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

A holding needs a ladder when the call open on it is below what the shares cost, or, with none
open, the shares are (``for_holding``, from what the broker reports). The rungs are planned from
the chain (``plan_rungs``): from the call sold now up to the strike at the cost, in the fewest even
steps a roll still pays for. The plan (``Projection``) lays the rungs out in time: the share price
taken to grow a steady amount a month, the ladder's rules are run on it a day at a time —
take-profits, rolls, calls sold again after they expire, the reports — from the call open now
until the shares are called.

Pure: everything here comes from the holding, what the person adjusts, and one chain pull
(``Market``). The model prices are Black-Scholes at one volatility — the open call's — and are
what the roll's limit is judged against, because the trigger price is in the future and no chain
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
    Position,
    PositionKind,
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
PLAN_STEP_SHARE = 0.05  # with no volatility quoted, planned rungs are about 5% of the price apart
# With no call open, a planned ladder starts at the strike of this delta, by how far the price is
# below the real cost: far below, the recovery is what's needed, so it isn't sold cheap
START_DELTAS = ((0.15, 0.15), (0.05, 0.25), (-math.inf, 0.40))  # (at least this far below, delta)
DEFAULT_GROWTH = 0.01  # a month: the steady climb the plan takes when the person sets none
DAYS_PER_MONTH = 365.25 / 12
PLAN_DAYS = 4 * 365  # the plan runs this far ahead, or until the shares are called
REPORT_EVERY = 91  # reports past the next one are taken to be a quarter apart
MAX_CALLS = 80
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


def call_delta(spot: float, strike: float, days: float, iv: float,
               rate: float = DEFAULT_CARRY_RATE) -> float:
    """Black-Scholes delta of a call."""
    if days <= 0 or iv <= 0:
        return 1.0 if spot > strike else 0.0
    t = days / 365.0
    v = iv * math.sqrt(t)
    return _cdf((math.log(spot / strike) + (rate + iv * iv / 2) * t) / v)


def start_delta(spot: float, real_cost: float) -> float:
    """The delta a planned ladder starts at with no call open: 0.15 with the price 15% or more
    below the real cost, 0.25 within 5-15% of it, 0.40 closer."""
    below = 1 - spot / real_cost if real_cost > 0 else 0.0
    return next(delta for gap, delta in START_DELTAS if below >= gap)


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
    """One holding's ladder: what the broker reports, and what the person adjusts. Prices are
    per share."""

    symbol: str
    shares: int
    cost: float  # what the shares cost: the broker's average cost, or the put's strike
    rungs: tuple[float, ...]  # ascending; the last is the target
    premiums: float = 0.0  # what the cost doesn't count: the put's premium, earlier calls'
    call_premiums: float = 0.0  # the open call's premium
    call_strike: float | None = None
    call_expiry: date | None = None
    call_sold_for: float | None = None  # what the open call was sold for
    growth: float | None = None  # a month, what the plan takes the share price to grow by

    @property
    def contracts(self) -> int:
        return self.shares // SHARES_PER_CONTRACT

    @property
    def real_cost(self) -> float:
        """What the shares stand at after every premium — what being called away is judged by."""
        return self.cost - self.premiums - self.call_premiums

    @property
    def has_call(self) -> bool:
        return self.call_strike is not None and self.call_expiry is not None

    @property
    def target(self) -> float:
        return self.rungs[-1]

    @property
    def monthly_growth(self) -> float:
        return DEFAULT_GROWTH if self.growth is None else self.growth


def for_holding(holding: Position, positions: list[Position]) -> Plan | None:
    """The ladder a holding needs, from what its broker reports: the shares and their average
    cost, and the call open on them (with several, the lowest strike). None when it needs none:
    under 100 shares, no cost reported, a call open at or above the cost already, or none open
    with the shares at or above it. Its rungs are empty, for the chain to plan."""
    if holding.kind is not PositionKind.SHARES or not holding.covered_call_lots:
        return None
    cost = holding.average_price
    if not cost or cost <= 0:
        return None
    shares = int(holding.quantity)
    calls = [p for p in positions
             if p.kind is PositionKind.SHORT_CALL and p.underlying == holding.underlying
             and p.strike and p.expiration and not p.expired]
    if calls:
        call = min(calls, key=lambda p: (p.strike, p.expiration))
        if call.strike >= cost - 1e-6:
            return None
        sold = call.opening_price or call.average_price
        return Plan(holding.underlying, shares, cost, (), call_premiums=sold or 0.0,
                    call_strike=call.strike, call_expiry=call.expiration, call_sold_for=sold)
    if not holding.market_value or holding.market_value / holding.quantity >= cost - 1e-6:
        return None
    return Plan(holding.underlying, shares, cost, ())


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
    pays: bool | None = None  # the step to ``to_strike`` pays the minimum at its worst (``_pays``)


@dataclass(frozen=True)
class RungPlan:
    """Rungs planned from the chain (``plan_rungs``), and what the first one came from."""

    rungs: tuple[float, ...]
    # the first rung is "call": the open call's strike; "delta": the strike at start_delta;
    # "premium": below that, the highest strike a new call still pays the minimum at; "price":
    # the lowest above the share price, when none pays yet; "target": the shares are at or past
    # the target already
    start: str
    iv: float | None  # the volatility the steps were judged at; None: none quoted, even steps
    tight: bool = False  # even MAX_RUNGS rungs leave steps that pay under the minimum
    delta: float | None = None  # with "delta" or "premium": the delta it was aimed at


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
class Stretch:
    """A stretch of the plan: the call open now, calls sold at one rung cycle after cycle, a roll
    up a rung, or how it ends."""

    # "open", "sell", "roll"; and the end: "called" (the shares go at the strike), "above" (the
    # price is past the last rung), "end" (PLAN_DAYS on, still climbing)
    how: str
    strike: float
    start: date  # sold, rolled into or called on; "open": today
    end: date  # its last call's expiry
    spot: float  # the share price on the path at ``start``
    spot_end: float  # ...and at ``end``
    calls: int = 1
    amount: float = 0.0  # what it brings in, per share: premiums less buybacks, or a roll's credit
    profits: int = 0  # calls closed early at the take-profit
    reports: int = 0  # reports waited out before its calls, with none open
    skipped: int = 0  # weeks with no call, as none paid the minimum
    short_days: int = 0  # its calls' days to expiry when sold: the fewest and the most
    long_days: int = 0
    from_strike: float | None = None  # a roll's: the strike it leaves
    trigger: float | None = None  # a roll's: the share price that sets it off
    credit: float | None = None  # a roll's net credit, before any buyback of the new call
    walls: tuple[date, ...] = ()  # days the trigger was reached on a call that couldn't roll
    outcome: str = ""  # its last call's: "expired", "profit", "rolled", "called" or "open"
    outcome_on: date | None = None
    real_cost: float = 0.0  # after it


@dataclass(frozen=True)
class Projection:
    """The plan, on a path where the share price grows by ``growth`` a month."""

    growth: float
    stretches: list[Stretch]
    real_cost: float  # at the end
    premiums: float  # brought in along the way, per share, net of buybacks
    called: Stretch | None = None  # the end, if the shares are called


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
    projection: Projection | None = None
    dividends: list[Dividend] = field(default_factory=list)  # ex-dates while the open call is
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


def _volatility(market: Market, quote: OptionContract | None) -> float | None:
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


def _next_rung(plan: Plan, price: float, reached: float = 0.0) -> float | None:
    """The rung a new call is sold at: the lowest above the price whose roll trigger is too (a
    call sold in its roll zone would roll at once), never below the rung already reached. The
    target has no roll, so it only has to be above the price."""
    return next((r for r in plan.rungs if r > price + 1e-6 and r >= reached - 1e-6
                 and (r == plan.target or ROLL_TRIGGER * r > price)), None)


def _allowed(e: date, today: date, report: date | None) -> bool:
    """An expiry a call may have: no more than 90 days out, and before the report."""
    return (e - today).days <= MAX_DAYS and (report is None or e < report)


def _open_expiry(expiries: list[date], start: date, today: date,
                 report: date | None) -> date | None:
    """A new call's expiry: the latest listed four to six weeks after ``start``. Failing that, the
    one nearest those weeks, three weeks out or more, before the report."""
    lo, hi = start + timedelta(days=OPEN_DAYS[0]), start + timedelta(days=OPEN_DAYS[1])
    window = [e for e in expiries if lo <= e <= hi and _allowed(e, today, report)]
    if window:
        return max(window)
    later = [e for e in expiries if e >= start + timedelta(days=21) and _allowed(e, today, report)]
    if not later:
        return None
    return min(later, key=lambda e: ((lo - e).days if e < lo else (e - hi).days, e))


def _new_call(plan: Plan, calls: list[OptionContract], expiries: list[date], strike: float,
              start: date, today: date, report: date | None, spot: float | None,
              iv: float | None, rate: float) -> NewCall | None:
    expiry = _open_expiry(expiries, start, today, report)
    if expiry is None:
        return None
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
        limit = max(floor, _model_limit(strike, to_strike, days_left, added, iv, rate))
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


def _model_limit(strike: float, to_strike: float, days_left: int, added: int, iv: float,
                 rate: float) -> float:
    """LIMIT_SHARE of the model's worst credit for the roll at the trigger, over the days left on
    the call, at volatility LIMIT_IV_DROP lower, to the $0.05 below. The roll order's limit,
    before the minimum."""
    low = max(0.05, iv - LIMIT_IV_DROP)
    trigger = round(ROLL_TRIGGER * strike, 2)
    worst = min(roll_credit(trigger, strike, to_strike, d, added, low, rate)
                for d in range(1, max(1, days_left) + 1))
    return math.floor(worst * LIMIT_SHARE * 20) / 20


def _pays(strike: float, to_strike: float, iv: float, rate: float) -> bool:
    """A step up the ladder that a roll pays for even at its worst: the trigger reached with a
    whole cycle (OPEN_DAYS) left on the call, four weeks added — the roll order's limit, from the
    model, is still the minimum or more."""
    return (_model_limit(strike, to_strike, OPEN_DAYS[1], ROLL_DAYS[0], iv, rate)
            >= min_roll_credit(strike))


def _strikes(calls: list[OptionContract], today: date, top: float) -> list[float]:
    """The strikes a rung can be: those listed a new call's four weeks or more out, carried on
    past the highest at its spacing until they reach ``top`` (a long way above the price, the
    chain may not list them yet)."""
    out_there = [c for c in calls if (c.expiration - today).days >= OPEN_DAYS[0]] or calls
    listed = sorted({c.strike for c in out_there})
    if not listed:
        return []
    gap = listed[-1] - listed[-2] if len(listed) > 1 else _step(top)
    while listed[-1] < top - 1e-6:
        listed.append(round(listed[-1] + gap, 2))
    return listed


def _even(start: float, target: float, steps: int, strikes: list[float]) -> tuple[float, ...]:
    """``steps`` steps from ``start`` to ``target``, each rung the strike nearest an even split
    (the lower one on a tie)."""
    rungs = [start]
    for i in range(1, steps):
        want = start + (target - start) * i / steps
        between = [s for s in strikes if rungs[-1] + 1e-6 < s < target - 1e-6]
        if between:
            rungs.append(min(between, key=lambda s: (abs(s - want), s)))
    return (*rungs, target)


def plan_rungs(plan: Plan, market: Market, today: date,
               rate: float = DEFAULT_CARRY_RATE) -> RungPlan | None:
    """The rungs for a plan typed without them. They start at the open call's strike — with none
    open, at the strike of ``start_delta``, or below it, the highest that still pays the minimum
    — and end at the first strike at or above the cost, in the fewest even steps each of which a
    roll pays for at its worst (``_pays``). None when there is nothing to plan from: no chain,
    or, with no call open, no share price."""
    calls = [c for c in market.calls if c.option_type is OptionType.CALL]
    spot = market.spot
    strikes = _strikes(calls, today, plan.cost)
    if not strikes or (spot is None and not plan.has_call):
        return None
    target = next(s for s in strikes if s >= plan.cost - 1e-6)
    quote = _quote(calls, plan.call_strike, plan.call_expiry) if plan.has_call else None
    iv = _volatility(market, quote)

    wanted = None
    if plan.has_call:
        start, how = plan.call_strike, "call"
    else:
        above = [s for s in strikes if spot + 1e-6 < s <= target + 1e-6]
        if not above:
            # past the cost already: the one rung is the first strike above the price
            first = next((s for s in strikes if s > spot + 1e-6), target)
            return RungPlan((first,), "target", iv)
        expiries = sorted({c.expiration for c in calls if c.expiration >= today})
        report = (market.next_earnings
                  if market.next_earnings and market.next_earnings >= today else None)
        expiry = _open_expiry(expiries, today, today, report)
        days = (expiry - today).days if expiry is not None else OPEN_DAYS[1]

        def premium(k: float) -> float | None:
            quoted = _mid(_quote(calls, k, expiry)) if expiry is not None else None
            if quoted is not None or iv is None:
                return quoted
            return call_value(spot, k, days, iv, rate)

        aim = above[-1]
        if iv is not None:
            wanted = start_delta(spot, plan.real_cost)
            aim = min(above, key=lambda k: (abs(call_delta(spot, k, days, iv, rate) - wanted), k))
        floor = min_premium(spot)
        paying = [k for k in above if k <= aim + 1e-6 and (premium(k) or 0.0) >= floor]
        if not paying:
            start, how = above[0], "price"
        else:
            start = max(paying)
            how = "delta" if iv is not None and abs(start - aim) < 1e-6 else "premium"
    if start >= target - 1e-6:
        return RungPlan((start,), how, iv, delta=wanted)

    if iv is None:
        steps = max(1, round((target - start) / (PLAN_STEP_SHARE * start)))
        return RungPlan(_even(start, target, min(steps, MAX_RUNGS - 1), strikes), how, None)
    for steps in range(1, MAX_RUNGS):
        rungs = _even(start, target, steps, strikes)
        if all(_pays(a, b, iv, rate) for a, b in zip(rungs, rungs[1:], strict=False)):
            return RungPlan(rungs, how, iv, delta=wanted)
    return RungPlan(_even(start, target, MAX_RUNGS - 1, strikes), how, iv, tight=True,
                    delta=wanted)


def _third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


def _ahead(listed: list[date], today: date) -> list[date]:
    """The expiries a plan can use: those listed, and the ones to come that aren't yet — every
    Friday for a chain that lists weeklies, the third Friday of each month for one that doesn't."""
    weekly = any((b - a).days == 7 for a, b in zip(listed, listed[1:], strict=False))
    end = today + timedelta(days=PLAN_DAYS + OPEN_DAYS[1])
    out = set(listed)
    if weekly:
        day = today + timedelta(days=(4 - today.weekday()) % 7)
        while day <= end:
            out.add(day)
            day += timedelta(days=7)
    else:
        y, m = today.year, today.month
        while (f := _third_friday(y, m)) <= end:
            if f >= today:
                out.add(f)
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return sorted(out)


def _reports(report: date | None, today: date) -> list[date]:
    """The next report, and the ones after it, a quarter apart, to the end of the plan."""
    if report is None:
        return []
    out = [report]
    while out[-1] < today + timedelta(days=PLAN_DAYS + REPORT_EVERY):
        out.append(out[-1] + timedelta(days=REPORT_EVERY))
    return out


@dataclass
class _Call:
    """One call on the plan's path, as it is lived."""

    how: str  # "open", "sell", "roll"
    strike: float
    on: date
    expiry: date
    spot: float
    amount: float  # what it brought in: its premium, or the roll's credit, less any buyback
    sold_for: float | None  # what it was sold for alone: the take-profit is a share of this
    from_strike: float | None = None
    trigger: float | None = None
    credit: float | None = None
    reports: int = 0
    skipped: int = 0
    walls: list[date] = field(default_factory=list)
    outcome: str = "open"
    outcome_on: date | None = None


def _project(plan: Plan, spot: float | None, iv: float | None, listed: list[date],
             reports: list[date], today: date, rate: float) -> Projection | None:
    """The ladder's rules, run a day at a time on a path where the share price grows a steady
    ``plan.monthly_growth`` a month at today's volatility, from the call open now (or the one to
    sell) until the shares are called, the price passes the last rung, or PLAN_DAYS go by.

    A call is bought back at the take-profit, or rolled up a rung once the price reaches its
    trigger and the roll pays the order's limit, or held to expiry: called if the price is past
    the strike then. Else a new one is sold (``_next_rung``, never below the rung reached), four
    to six weeks out, before the next report; one that pays under the minimum waits a week."""
    if spot is None or iv is None or not plan.rungs:
        return None
    growth, expiries = plan.monthly_growth, _ahead(listed, today)
    end = today + timedelta(days=PLAN_DAYS)

    def at(d: date) -> float:
        return spot * (1 + growth) ** ((d - today).days / DAYS_PER_MONTH)

    def value(d: date, strike: float, expiry: date) -> float:
        return call_value(at(d), strike, (expiry - d).days, iv, rate)

    lived: list[_Call] = []
    cur: _Call | None = None
    start, waited, skipped, stop, reached = today, 0, 0, "", 0.0
    if plan.has_call and plan.call_expiry >= today:
        cur = _Call("open", plan.call_strike, today, plan.call_expiry, spot, 0.0,
                    plan.call_sold_for)
    while len(lived) < MAX_CALLS:
        if cur is None:
            if start > end:
                stop = "end"
                break
            price = at(start)
            strike = _next_rung(plan, price, reached)
            if strike is None:
                stop = "above"
                break
            report = next((r for r in reports if r > start), None)
            expiry = _open_expiry(expiries, start, start, report)
            if expiry is None:
                if report is None:
                    stop = "end"
                    break
                start, waited = report + timedelta(days=1), waited + 1
                continue
            premium = value(start, strike, expiry)
            if premium < min_premium(price):
                start, skipped = start + timedelta(days=7), skipped + 1
                continue
            cur = _Call("sell", strike, start, expiry, price, premium, premium, reports=waited,
                        skipped=skipped)
            waited = skipped = 0
        lived.append(cur)
        reached = max(reached, cur.strike)

        higher = [r for r in plan.rungs if r > cur.strike + 1e-6]
        to_expiry = limit = None
        if higher:
            report = next((r for r in reports if r > cur.on), None)
            lo = cur.expiry + timedelta(days=ROLL_DAYS[0])
            hi = cur.expiry + timedelta(days=ROLL_DAYS[1])
            fits = [e for e in expiries if lo <= e <= hi and (report is None or e < report)]
            if fits:
                to_expiry = min(fits)
                limit = max(min_roll_credit(cur.strike),
                            _model_limit(cur.strike, higher[0], (cur.expiry - cur.on).days,
                                         (to_expiry - cur.expiry).days, iv, rate))
        trigger = round(ROLL_TRIGGER * cur.strike, 2)
        take = TAKE_PROFIT * cur.sold_for if cur.sold_for else None
        rolled: _Call | None = None
        day = cur.on + timedelta(days=1)
        while day < cur.expiry and cur.outcome == "open":
            if higher and at(day) >= trigger:
                if to_expiry is None:
                    if not cur.walls:
                        cur.walls.append(day)
                else:
                    new = value(day, higher[0], to_expiry)
                    credit = new - value(day, cur.strike, cur.expiry)
                    if credit >= limit:
                        cur.outcome, cur.outcome_on = "rolled", day
                        rolled = _Call("roll", higher[0], day, to_expiry, at(day), credit, new,
                                       from_strike=cur.strike, trigger=trigger, credit=credit)
                        break
            if take is not None and value(day, cur.strike, cur.expiry) <= take:
                cur.amount -= value(day, cur.strike, cur.expiry)
                cur.outcome, cur.outcome_on = "profit", day
                break
            day += timedelta(days=1)
        if rolled is not None:
            cur = rolled
            continue
        if cur.outcome == "profit":
            start, cur = cur.outcome_on, None
            continue
        if cur.expiry > end:
            stop = "end"
            break
        cur.outcome_on = cur.expiry
        if at(cur.expiry) >= cur.strike:
            cur.outcome, stop = "called", "called"
            break
        cur.outcome, start, cur = "expired", cur.expiry, None
    return _stretches(plan, lived, at, stop, start, growth)


def _stretches(plan: Plan, lived: list[_Call], at, stop: str, stopped: date,
               growth: float) -> Projection:
    """The calls lived, as the page tells them: those sold again and again at one rung, one
    line."""
    rows: list[dict] = []
    rc = plan.real_cost  # the open call's premium is in it already
    for c in lived:
        rc -= c.amount
        days = (c.expiry - c.on).days
        last = rows[-1] if rows else None
        if (c.how == "sell" and last is not None and last["how"] == "sell"
                and last["strike"] == c.strike):
            last.update(end=c.expiry, spot_end=at(c.expiry), calls=last["calls"] + 1,
                        amount=last["amount"] + c.amount, reports=last["reports"] + c.reports,
                        skipped=last["skipped"] + c.skipped,
                        short_days=min(last["short_days"], days),
                        long_days=max(last["long_days"], days),
                        walls=last["walls"] + tuple(c.walls))
        else:
            last = dict(how=c.how, strike=c.strike, start=c.on, end=c.expiry, spot=c.spot,
                        spot_end=at(c.expiry), amount=c.amount, reports=c.reports,
                        skipped=c.skipped, short_days=days, long_days=days,
                        from_strike=c.from_strike, trigger=c.trigger,
                        credit=None if c.credit is None else round(c.credit, 2),
                        walls=tuple(c.walls), calls=1, profits=0)
            rows.append(last)
        last.update(profits=last["profits"] + (c.outcome == "profit"), outcome=c.outcome,
                    outcome_on=c.outcome_on, real_cost=round(rc, 2))
    stretches = [Stretch(**{**r, "amount": round(r["amount"], 2)}) for r in rows]
    called = None
    if stop == "called":
        c = lived[-1]
        called = Stretch("called", c.strike, c.expiry, c.expiry, at(c.expiry), at(c.expiry),
                         calls=0, real_cost=round(rc, 2))
        stretches.append(called)
    elif stop in ("above", "end"):
        day = stopped if stop == "above" or not lived else lived[-1].expiry
        stretches.append(Stretch(stop, lived[-1].strike if lived else plan.rungs[0], day, day,
                                 at(day), at(day), calls=0, real_cost=round(rc, 2)))
    premiums = sum(c.amount for c in lived)
    return Projection(growth, stretches, round(rc, 2), round(premiums, 2), called)


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
    iv = _volatility(market, quote)
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
        pays = _pays(r, to, iv, rate) if to is not None and iv is not None else None
        if last:
            state = "target"
        elif plan.has_call and abs(r - plan.call_strike) < 1e-6:
            state = "open"
        elif plan.has_call and r < plan.call_strike:
            state = "passed"
        else:
            state = "ahead"
        rungs.append(Rung(r, trigger, to, None if credit is None else round(credit, 2),
                          round(r - rc, 2), state, pays))

    trigger = round(ROLL_TRIGGER * plan.call_strike, 2) if plan.has_call else None
    base = dict(plan=plan, spot=spot, iv=iv, rungs=rungs, report=report, wall=wall,
                rail=_rail(plan, spot, trigger))
    projection = _project(plan, spot, iv, expiries, _reports(report, today), today, rate)

    if not plan.has_call:
        # never a call in the money, nor one in its roll zone already
        strike = (plan.rungs[0] if spot is None else _next_rung(plan, spot)) or plan.rungs[-1]
        new = _new_call(plan, calls, expiries, strike, today, today, report, spot, iv, rate)
        status = "wait" if new is None else ("sell" if new.sells else "thin")
        return Ladder(**base, status=status, new_call=new, projection=projection, notes=notes)

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
        return Ladder(**base, status="expired", days_left=days_left, notes=notes)

    roll = _roll(plan, calls, expiries, today, report, iv, quote, rate)
    dividends = [d for d in market.dividends if today < d.ex_date <= expiry]
    early = None
    if spot is not None:
        price = None if quote is None else (quote.bid if quote.bid else mid)
        early = assess_assignment(OptionType.CALL, plan.call_strike, spot, price, expiry, today,
                                  dividends, rate)
    next_call = None
    if roll.blocked != "target":
        next_call = _new_call(plan, calls, expiries, plan.call_strike, expiry, today, report,
                              spot, iv, rate)

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
                  projection=projection, dividends=dividends, early=early,
                  notes=notes)

