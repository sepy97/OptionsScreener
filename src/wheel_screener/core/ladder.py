"""The covered-call ladder: one holding's call stepped up a fixed set of strikes ("rungs") until
the shares can leave at the price they were assigned at.

The rules are docs/CALL_LADDER.md's. A call is open at a rung. Two orders rest on it at the
broker, linked so that one cancels the other: a take-profit that buys it back at 25% of what it
was sold for, and a roll that waits for the share price to reach 97% of the strike, then buys it
back and sells the next rung four to six weeks later, for a net credit. Fixed rungs are what let
the roll be written down in advance: with a strike chosen by delta at roll time, there is nothing
to place until that moment comes.

No option is held over an earnings report. The last expiry before a report is a wall: a roll
must land on or before it, so the call that expires there cannot roll at all, and after the
report the ladder starts again.

Pure: everything here comes from what the person entered and one chain pull (``Market``). The
model prices are Black-Scholes at one volatility — the open call's — and are what the limit and
the roll table are judged against, because the trigger price is in the future and no chain
quotes the future. The roll that would be placed today is priced from the chain as well.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

from wheel_screener.core.assignment import DEFAULT_CARRY_RATE
from wheel_screener.core.assignment import assess as assess_assignment
from wheel_screener.core.models import (
    AssignmentRisk,
    Dividend,
    EarlyAssignment,
    OptionContract,
    OptionType,
)

ROLL_TRIGGER = 0.97  # a share price at 97% of the strike counts as close to it
TAKE_PROFIT = 0.25  # buy the call back once it costs 25% of what it was sold for
# The TER plan's minimums were $3.00 for a new call with the shares near $300, and $0.50 for a
# roll of the $340: as shares of the price, they hold for any stock, not only a $300 one.
MIN_PREMIUM_SHARE = 0.01  # a new call has to pay 1% of the share price to be worth selling
MIN_ROLL_CREDIT_SHARE = 0.0015  # a roll at least 0.15% of the strike; never for a debit
ROLL_DAYS = (28, 45)  # a roll's new expiry, counted from the old one
OPEN_DAYS = (28, 45)  # a new call's expiry, counted from the day it is sold
MAX_DAYS = 90  # no call or roll further out than this
LIMIT_SHARE = 0.85  # the roll's limit: this share of the model's worst credit at the trigger...
LIMIT_IV_DROP = 0.10  # ...with volatility this much lower, since a rally usually lowers it
GAP_DAYS_LEFT = 10  # the gap price is judged with this many days left on the open call
CHAIN_DAYS = 200  # how far out the chain is pulled: past one roll, and the report after it
MAX_RUNGS = 12
SHARES_PER_CONTRACT = 100


def min_premium(price: float) -> float:
    """What a new call has to pay, per share, at this share price."""
    return round(max(0.05, MIN_PREMIUM_SHARE * price), 2)


def min_roll_credit(strike: float) -> float:
    """The least a roll of a call at this strike may pay, per share."""
    return round(max(0.05, MIN_ROLL_CREDIT_SHARE * strike), 2)


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def call_value(spot: float, strike: float, days: float, iv: float,
               rate: float = DEFAULT_CARRY_RATE) -> float:
    """Black-Scholes value of a call, per share. At or past expiry, what it is in the money."""
    if days <= 0 or iv <= 0:
        return max(spot - strike, 0.0)
    t = days / 365.0
    v = iv * math.sqrt(t)
    d1 = (math.log(spot / strike) + (rate + iv * iv / 2) * t) / v
    return spot * _cdf(d1) - strike * math.exp(-rate * t) * _cdf(d1 - v)


def roll_credit(spot: float, strike: float, to_strike: float, days_left: float, added_days: int,
                iv: float, rate: float = DEFAULT_CARRY_RATE) -> float:
    """What rolling pays per share at the model's mid prices: the new call, less the old."""
    return (call_value(spot, to_strike, days_left + added_days, iv, rate)
            - call_value(spot, strike, days_left, iv, rate))


def leg(action: str, contracts: int, symbol: str, expiry: date, strike: float) -> str:
    """One order line as an order ticket reads it: BUY TO CLOSE 1 TER 18 DEC 26 340 CALL."""
    return (f"{action} {contracts} {symbol} {expiry.day:02d} "
            f"{expiry.strftime('%b').upper()} {expiry.strftime('%y')} {strike:g} CALL")


@dataclass(frozen=True)
class Plan:
    """What a person enters for one holding. Prices are per share."""

    symbol: str
    shares: int
    cost: float  # what the shares cost: the strike of the put they were assigned on
    rungs: tuple[float, ...]  # ascending; the last is the target
    put_premium: float = 0.0
    # every call premium so far, the open call's included, less whatever buying calls back cost
    call_premiums: float = 0.0
    call_strike: float | None = None
    call_expiry: date | None = None
    call_sold_for: float | None = None  # what the open call was sold for

    @property
    def contracts(self) -> int:
        return self.shares // SHARES_PER_CONTRACT

    @property
    def real_cost(self) -> float:
        """What the shares stand at after every premium — what being called away is judged by."""
        return self.cost - self.put_premium - self.call_premiums

    @property
    def has_call(self) -> bool:
        return self.call_strike is not None and self.call_expiry is not None

    @property
    def target(self) -> float:
        return self.rungs[-1]


@dataclass(frozen=True)
class Market:
    """One chain pull's worth of what the ladder is priced from."""

    spot: float | None
    calls: list[OptionContract]
    next_earnings: date | None = None
    dividends: list[Dividend] = field(default_factory=list)  # ex-dates ahead, earliest first
    dividends_known: bool = False


@dataclass(frozen=True)
class Rung:
    strike: float
    trigger: float | None  # None on the target: nothing rolls from there
    to_strike: float | None
    credit: float | None  # model credit at the trigger, GAP_DAYS_LEFT days left, 4 weeks added
    if_called: float  # strike less real cost, per share
    state: str  # "passed", "open", "ahead" or "target"


@dataclass(frozen=True)
class Roll:
    """The roll order that rests on the open call, or why there is none."""

    trigger: float
    to_strike: float | None = None
    to_expiry: date | None = None
    added_days: int = 0
    limit: float | None = None
    gap: float | None = None  # above this, with GAP_DAYS_LEFT days left, the limit can't fill
    credit_mid: float | None = None  # rolling right now, at the chain's mid prices
    credit_natural: float | None = None  # ...and at its bid and ask
    blocked: str | None = None  # in words, when it can't be placed
    lines: tuple[str, ...] = ()
    minimum: float = 0.0  # the least it may pay (min_roll_credit)


@dataclass(frozen=True)
class NewCall:
    """A call to sell: when none is open, or the one after the open call expires."""

    strike: float
    expiry: date
    premium: float | None  # the chain's mid, or the model's estimate (``estimated``)
    estimated: bool
    sells: bool  # pays ``minimum`` or more
    line: str
    minimum: float = 0.0  # what it has to pay (min_premium)


@dataclass(frozen=True)
class Event:
    day: date
    kind: str  # now, dividend, expiry, wall, report, resume
    dividend: Dividend | None = None
    wall: bool = False  # this day is also the last expiry before the report


@dataclass(frozen=True)
class CreditGrid:
    prices: list[float]
    days_left: list[int]
    cells: list[list[float]]  # [price][days_left]
    trigger: float
    limit: float | None


@dataclass(frozen=True)
class RailMark:
    value: float
    at: float  # 0..1 along the rail
    kind: str  # spot, rung, cost


@dataclass(frozen=True)
class Ladder:
    plan: Plan
    spot: float | None
    iv: float | None
    rungs: list[Rung]
    status: str  # one of the STATUS keys the page words
    report: date | None
    wall: date | None
    quote: OptionContract | None = None  # the open call, from the chain
    days_left: int | None = None
    take_profit: float | None = None
    kept: float | None = None  # share of the premium kept so far, at the mid
    roll: Roll | None = None
    new_call: NewCall | None = None  # nothing open: the call to sell now
    next_call: NewCall | None = None  # what follows the open call if it expires below its strike
    events: list[Event] = field(default_factory=list)
    grid: CreditGrid | None = None
    early: EarlyAssignment | None = None
    rail: list[RailMark] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _quote(calls: list[OptionContract], strike: float, expiry: date) -> OptionContract | None:
    return next((c for c in calls if c.expiration == expiry and abs(c.strike - strike) < 1e-6),
                None)


def _mid(c: OptionContract | None) -> float | None:
    if c is None:
        return None
    if c.mid:
        return c.mid
    if c.bid is not None and c.ask:
        return (c.bid + c.ask) / 2
    return c.last or None


def _volatility(plan: Plan, market: Market, quote: OptionContract | None) -> float | None:
    """The one volatility the model uses: the open call's, else the nearest-to-the-money call's
    a month or more out."""
    if quote is not None and quote.implied_volatility and quote.implied_volatility > 0:
        return quote.implied_volatility
    if market.spot is None:
        return None
    usable = [c for c in market.calls if c.implied_volatility and c.implied_volatility > 0
              and c.dte >= 21]
    if not usable:
        return None
    near = min(usable, key=lambda c: (abs(c.strike - market.spot), c.dte))
    return near.implied_volatility


def _wall(expiries: list[date], report: date | None, today: date) -> date | None:
    """The last listed expiry before the report: no call may expire after it."""
    if report is None or report < today:
        return None
    before = [e for e in expiries if e < report]
    return max(before) if before else None


def _strike_at_or_above(calls: list[OptionContract], expiry: date, wanted: float) -> float | None:
    listed = sorted({c.strike for c in calls
                     if c.expiration == expiry and c.strike >= wanted - 1e-6})
    return listed[0] if listed else None


def _step(price: float) -> float:
    """A round price step about 3% of the price: 10 for a $340 strike."""
    raw = price * 0.03
    for s in (0.5, 1, 2.5, 5, 10, 25, 50, 100):
        if raw <= s * 1.5:
            return float(s)
    return 100.0


def _allowed(e: date, today: date, report: date | None) -> bool:
    """An expiry a call may have: no more than 90 days out, and before the report."""
    return (e - today).days <= MAX_DAYS and (report is None or e < report)


def _new_call(plan: Plan, calls: list[OptionContract], expiries: list[date], strike: float,
              start: date, today: date, report: date | None, spot: float | None,
              iv: float | None, rate: float) -> NewCall | None:
    """The latest listed expiry four to six weeks after ``start``. Failing that, with a report
    ahead, the last expiry before it at least three weeks out; with none, the nearest one."""
    lo, hi = start + timedelta(days=OPEN_DAYS[0]), start + timedelta(days=OPEN_DAYS[1])
    window = [e for e in expiries if lo <= e <= hi and _allowed(e, today, report)]
    if window:
        expiry = max(window)
    else:
        later = [e for e in expiries
                 if e >= start + timedelta(days=21) and _allowed(e, today, report)]
        if not later:
            return None
        expiry = max(later) if report is not None else min(later)
    listed = _strike_at_or_above(calls, expiry, strike)
    k = listed if listed is not None else strike
    quoted = _mid(_quote(calls, k, expiry)) if start == today else None
    if quoted is not None:
        premium, estimated = quoted, False
    elif spot is not None and iv is not None:
        premium, estimated = call_value(spot, k, (expiry - start).days, iv, rate), True
    else:
        premium, estimated = None, True
    floor = min_premium(spot or k)
    return NewCall(k, expiry, premium, estimated,
                   sells=premium is not None and premium >= floor,
                   line=leg("SELL TO OPEN", plan.contracts, plan.symbol, expiry, k), minimum=floor)


def _roll(plan: Plan, calls: list[OptionContract], expiries: list[date], today: date,
          report: date | None, iv: float | None, quote: OptionContract | None,
          rate: float) -> Roll:
    strike, expiry = plan.call_strike, plan.call_expiry
    trigger = round(ROLL_TRIGGER * strike, 2)
    higher = [r for r in plan.rungs if r > strike + 1e-6]
    if not higher:
        return Roll(trigger, blocked="target")
    lo, hi = expiry + timedelta(days=ROLL_DAYS[0]), expiry + timedelta(days=ROLL_DAYS[1])
    listed = [e for e in expiries if lo <= e <= hi and (e - today).days <= MAX_DAYS]
    fits = [e for e in listed if _allowed(e, today, report)]
    if not fits:
        # every expiry far enough out is past the report, or none is listed that far out yet
        blocked_by_report = report is not None and (bool(listed) or lo >= report)
        return Roll(trigger, blocked="report" if blocked_by_report else "unlisted")
    to_expiry = min(fits)
    to_strike = _strike_at_or_above(calls, to_expiry, higher[0])
    if to_strike is None:
        return Roll(trigger, to_expiry=to_expiry, blocked="no-strike")
    added = (to_expiry - expiry).days
    days_left = max(1, (expiry - today).days)
    floor = min_roll_credit(strike)

    limit = gap = None
    if iv is not None:
        low = max(0.05, iv - LIMIT_IV_DROP)
        worst = min(roll_credit(trigger, strike, to_strike, d, added, low, rate)
                    for d in range(1, days_left + 1))
        limit = max(floor, math.floor(worst * LIMIT_SHARE * 20) / 20)
        at = min(GAP_DAYS_LEFT, days_left)
        s, step = trigger, max(0.01, strike / 400)
        while s < 3 * to_strike:
            if roll_credit(s, strike, to_strike, at, added, iv, rate) < limit:
                gap = round(s, 2)
                break
            s += step
    else:
        limit = floor

    new = _quote(calls, to_strike, to_expiry)
    mid_new, mid_old = _mid(new), _mid(quote)
    credit_mid = (mid_new - mid_old) if mid_new is not None and mid_old is not None else None
    credit_natural = (new.bid - quote.ask
                      if new is not None and quote is not None and new.bid is not None
                      and quote.ask is not None else None)
    return Roll(
        trigger, to_strike, to_expiry, added, limit, gap,
        None if credit_mid is None else round(credit_mid, 2),
        None if credit_natural is None else round(credit_natural, 2),
        lines=(leg("BUY TO CLOSE", plan.contracts, plan.symbol, expiry, strike),
               leg("SELL TO OPEN", plan.contracts, plan.symbol, to_expiry, to_strike)),
        minimum=floor,
    )


def _grid(plan: Plan, roll: Roll, days_left: int, iv: float, rate: float) -> CreditGrid:
    strike, step = plan.call_strike, _step(plan.call_strike)
    base = math.floor(roll.trigger / step) * step
    below = [base - step, base] if base < roll.trigger else [base - 2 * step, base - step]
    prices = sorted({*below, roll.trigger, strike, *(strike + step * k for k in range(1, 6))})
    lefts = [days_left] + [d for d in (30, 20, 10, 5, 1) if d < days_left]
    cells = [[round(roll_credit(s, strike, roll.to_strike, d, roll.added_days, iv, rate), 2)
              for d in lefts] for s in prices]
    return CreditGrid(prices, lefts, cells, roll.trigger, roll.limit)


def _rail(plan: Plan, spot: float | None, trigger: float | None) -> list[RailMark]:
    """The rungs, the real cost, the share price and the roll trigger along one price scale."""
    values = [*plan.rungs, plan.real_cost] + ([spot] if spot else [])
    lo, hi = min(values) * 0.95, max(values) * 1.03
    span = hi - lo or 1.0

    def mark(value: float, kind: str) -> RailMark:
        return RailMark(value, min(1.0, max(0.0, (value - lo) / span)), kind)

    marks = [mark(r, "rung") for r in plan.rungs]
    marks.append(mark(plan.real_cost, "cost"))
    if trigger is not None:
        marks.append(mark(trigger, "trigger"))
    if spot:
        marks.append(mark(spot, "spot"))
    return marks


def build(plan: Plan, market: Market, today: date, rate: float = DEFAULT_CARRY_RATE) -> Ladder:
    """The ladder page's numbers for one plan against one chain pull."""
    calls = [c for c in market.calls if c.option_type is OptionType.CALL]
    expiries = sorted({c.expiration for c in calls if c.expiration >= today})
    report = (market.next_earnings
              if market.next_earnings and market.next_earnings >= today else None)
    wall = _wall(expiries, report, today)
    spot = market.spot
    quote = _quote(calls, plan.call_strike, plan.call_expiry) if plan.has_call else None
    iv = _volatility(plan, market, quote)
    notes: list[str] = []
    if report is None:
        notes.append("no-report")

    rc = plan.real_cost
    rungs = []
    for i, r in enumerate(plan.rungs):
        last = i == len(plan.rungs) - 1
        trigger = None if last else round(ROLL_TRIGGER * r, 2)
        to = None if last else plan.rungs[i + 1]
        credit = (roll_credit(trigger, r, to, GAP_DAYS_LEFT, ROLL_DAYS[0], iv, rate)
                  if trigger is not None and iv is not None else None)
        if last:
            state = "target"
        elif plan.has_call and abs(r - plan.call_strike) < 1e-6:
            state = "open"
        elif plan.has_call and r < plan.call_strike:
            state = "passed"
        else:
            state = "ahead"
        rungs.append(Rung(r, trigger, to, None if credit is None else round(credit, 2),
                          round(r - rc, 2), state))

    events = [Event(today, "now")]
    trigger = round(ROLL_TRIGGER * plan.call_strike, 2) if plan.has_call else None
    base = dict(plan=plan, spot=spot, iv=iv, rungs=rungs, report=report, wall=wall,
                rail=_rail(plan, spot, trigger))

    if not plan.has_call:
        # the lowest rung above the share price: never a call in the money, never below the
        # first rung, and never a step down the ladder
        strike = next((r for r in plan.rungs if spot is None or r > spot), plan.rungs[-1])
        new = _new_call(plan, calls, expiries, strike, today, today, report, spot, iv, rate)
        if new is not None:
            events.append(Event(new.expiry, "expiry", wall=new.expiry == wall))
        events += [e for e in _report_events(wall, report)
                   if not (e.kind == "wall" and new is not None and e.day == new.expiry)]
        status = "wait" if new is None else ("sell" if new.sells else "thin")
        return Ladder(**base, status=status, new_call=new, events=_sorted(events), notes=notes)

    expiry = plan.call_expiry
    days_left = (expiry - today).days
    take_profit = (round(plan.call_sold_for * TAKE_PROFIT, 2)
                   if plan.call_sold_for and plan.call_sold_for > 0 else None)
    mid = _mid(quote)
    kept = (1 - mid / plan.call_sold_for
            if mid is not None and plan.call_sold_for and plan.call_sold_for > 0 else None)
    if quote is None and days_left >= 0:
        notes.append("no-quote")

    if days_left < 0:
        events += _report_events(wall, report)
        return Ladder(**base, status="expired", days_left=days_left, events=_sorted(events),
                      notes=notes)

    roll = _roll(plan, calls, expiries, today, report, iv, quote, rate)
    early = None
    if spot is not None:
        price = None if quote is None else (quote.bid if quote.bid else mid)
        early = assess_assignment(OptionType.CALL, plan.call_strike, spot, price, expiry, today,
                                  [d for d in market.dividends if today < d.ex_date <= expiry],
                                  rate)
    next_call = None
    if roll.blocked != "target":
        next_call = _new_call(plan, calls, expiries, plan.call_strike, expiry, today, report,
                              spot, iv, rate)
    for d in market.dividends:
        if today < d.ex_date <= expiry:
            events.append(Event(d.ex_date, "dividend", dividend=d))
    events.append(Event(expiry, "expiry", wall=expiry == wall))
    events += [e for e in _report_events(wall, report)
               if not (e.kind == "wall" and e.day == expiry)]
    grid = (_grid(plan, roll, days_left, iv, rate)
            if iv is not None and roll.blocked is None and days_left > 0 else None)

    spans = report is not None and expiry >= report
    if spans:
        status = "spans"
    elif roll.blocked == "target":
        status = "target"
    elif take_profit is not None and quote is not None and quote.ask is not None \
            and quote.ask <= take_profit:
        status = "take-profit"
    elif early is not None and early.risk is AssignmentRisk.LIKELY:
        status = "assignment"
    elif spot is not None and spot >= roll.trigger:
        status = "roll-zone"
    else:
        status = "hold"
    if spot is not None and spot >= rc:
        notes.append("breakeven")
    return Ladder(**base, status=status, quote=quote, days_left=days_left,
                  take_profit=take_profit, kept=kept, roll=roll, next_call=next_call,
                  events=_sorted(events), grid=grid, early=early, notes=notes)


def _report_events(wall: date | None, report: date | None) -> list[Event]:
    out = []
    if wall is not None:
        out.append(Event(wall, "wall", wall=True))
    if report is not None:
        out += [Event(report, "report"), Event(report + timedelta(days=1), "resume")]
    return out


def _sorted(events: list[Event]) -> list[Event]:
    order = {"now": 0, "dividend": 1, "expiry": 2, "wall": 3, "report": 4, "resume": 5}
    seen, out = set(), []
    for e in sorted(events, key=lambda e: (e.day, order[e.kind])):
        if (e.day, e.kind) not in seen:
            seen.add((e.day, e.kind))
            out.append(e)
    return out
