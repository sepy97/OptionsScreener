"""Scoring the ways out of an open short put, in one comparable unit.

Every action a holder can take — keep it, roll it, take assignment and sell a call against the
shares — is the same question wearing different clothes: *what does this capital earn from
here?* So each is reduced to one number, an annualised return on the collateral it commits, and
the highest wins.

Two things make that comparison honest, and both were found by getting them wrong first:

* **A roll earns over the days it ADDS, not its whole life.** The days before the current expiry
  belong to you either way. Scoring a seven-day extension across its full 33-day span made it
  look four times better than it was.
* **Rolling UP a put sells intrinsic, not time.** The credit balloons because you are being paid
  for an obligation you expect to hand back, and the committed collateral quietly grows with it.
  Ranked on credit alone, the worst available action sorts to the top. ``extrinsic`` and
  ``collateral_delta`` are carried on every row so that trade cannot hide.

For an in-the-money put, keeping and assign-then-covered-call score identically — put-call parity
guarantees it, since the put's extrinsic over the strike equals the call's premium over the share
value. That is a property of the maths, not a coincidence, and it is asserted in the tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from wheel_screener.core.models import OptionContract, OptionType

CONTRACT_MULTIPLIER = 100


@dataclass(frozen=True)
class ExitOption:
    """One way out, priced. ``credit`` is cash received; ``days`` is the time it commits."""

    kind: str  # "keep" | "roll" | "assign_cc"
    label: str
    credit: float
    days: int
    collateral: float
    # The part of ``credit`` that is time value rather than intrinsic — i.e. the part that can
    # actually be earned. Equal to credit for a same-strike roll; smaller when the strike moves.
    extrinsic: float | None = None
    strike: float | None = None
    expiration: date | None = None
    # How much more (or less) collateral this action commits than the position does today.
    collateral_delta: float = 0.0
    # Days from TODAY to this action's expiry. For a roll that is not ``days``: the credit is
    # earned over the days the roll ADDS, but the capital is committed for the whole run. "56
    # added" alone left the reader unable to tell whether 56 was the total or an increment.
    total_days: int | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def rate(self) -> float | None:
        """Annualised return on the collateral committed — scored on TIME VALUE, not credit.

        The distinction only bites on a roll to a different strike, and there it decides the
        whole ranking. Rolling AVGO's $390 put up to $435 pays a $3,476 credit, which on gross
        cash is 417%/yr and sorts straight to the top of the table. Net of the intrinsic being
        sold it is *minus* $1,024: the trade destroys time value, commits $4,500 more collateral,
        and the credit is money handed back at expiry. Ranking on cash received would recommend
        it, and would recommend it hardest exactly when it is worst.

        For keeping, for assign-and-write, and for a same-strike roll the two are identical, so
        this changes nothing there.
        """
        earnable = self.extrinsic if self.extrinsic is not None else self.credit
        if self.collateral <= 0 or self.days <= 0:
            return None
        return (earnable / self.collateral) * (365.0 / self.days)


def intrinsic(
    strike: float, spot: float | None, contracts: float,
    option_type: OptionType = OptionType.PUT,
) -> float:
    """What the contract is worth on exercise at today's price. Never negative."""
    if spot is None:
        return 0.0
    gap = strike - spot if option_type is OptionType.PUT else spot - strike
    return max(0.0, gap) * CONTRACT_MULTIPLIER * contracts


def committed(
    strike: float, spot: float | None, contracts: float,
    option_type: OptionType, is_short: bool, own_value: float,
) -> float:
    """The capital this position ties up — the denominator every rate is built on.

    It is a different quantity for each kind of position, and using the wrong one is the
    quietest way to make two rows uncomparable:

    * a SHORT PUT reserves cash equal to the strike, because that is what assignment costs;
    * a SHORT CALL encumbers shares, so the capital is what those shares are worth today, not
      what they cost — a covered call against a position bought at $200 and now worth $400 ties
      up $400 of value, and pretending otherwise doubles its apparent return;
    * a LONG option ties up only what it would fetch if sold. Nothing else is at stake, and the
      premium originally paid is gone whatever happens next.
    """
    if not is_short:
        return own_value
    if option_type is OptionType.PUT:
        return strike * CONTRACT_MULTIPLIER * contracts
    return (spot or strike) * CONTRACT_MULTIPLIER * contracts


def _match(chain: list[OptionContract], strike: float, expiration: date) -> OptionContract | None:
    return next(
        (c for c in chain if c.strike == strike and c.expiration == expiration), None
    )


def keep(
    chain: list[OptionContract], strike: float, expiration: date, contracts: float,
    spot: float | None, today: date, *,
    option_type: OptionType = OptionType.PUT, is_short: bool = True,
) -> ExitOption | None:
    """Hold to expiry. The extrinsic changes hands either way — the SIGN is the whole story.

    Short, you are paid to wait: the time value decays in your favour and you keep it. Long, you
    are paying to wait, and the identical number is a cost. Reporting both as positive would put
    a bleeding long call alongside a working short put as though they were the same thing.

    Priced off the side you would actually trade out at — the ask to buy a short back, the bid to
    sell a long. The mid flatters both.
    """
    cur = _match(chain, strike, expiration)
    if cur is None:
        return None
    price = cur.ask if is_short else cur.bid
    if price is None:
        return None
    own_value = price * CONTRACT_MULTIPLIER * contracts
    time_value = own_value - intrinsic(strike, spot, contracts, option_type)
    earnable = time_value if is_short else -time_value
    remaining = (expiration - today).days
    return ExitOption(
        kind="keep", label="Keep to expiry",
        credit=round(earnable, 2), days=remaining, total_days=remaining,
        collateral=committed(strike, spot, contracts, option_type, is_short, own_value),
        extrinsic=round(earnable, 2), strike=strike, expiration=expiration,
    )


def rolls(
    chain: list[OptionContract], strike: float, expiration: date, contracts: float,
    spot: float | None, today: date, *, roll_strike: float | None = None,
    option_type: OptionType = OptionType.PUT, is_short: bool = True,
) -> list[ExitOption]:
    """Close the current contract and reopen it later, for every expiry on the board.

    Short, a roll is a credit: sell the later contract, buy back the near one. Long, it is a
    debit — sell what you hold, pay for more time — so the same arithmetic runs with the two
    sides swapped, and the result is negative because extending a long costs money.

    ``roll_strike`` defaults to the position's own, the only comparison that is purely an
    extension of time. A different strike is offered because traders do it, and is annotated.
    """
    cur = _match(chain, strike, expiration)
    if cur is None:
        return []
    close_at = cur.ask if is_short else cur.bid          # what unwinding costs / returns
    if close_at is None:
        return []
    target = strike if roll_strike is None else roll_strike
    unwind = close_at * CONTRACT_MULTIPLIER * contracts
    here_intrinsic = intrinsic(strike, spot, contracts, option_type)

    out: list[ExitOption] = []
    seen: set[date] = set()
    for c in sorted(chain, key=lambda c: (c.expiration, -(c.bid or 0.0))):
        if c.strike != target or c.expiration <= expiration:
            continue
        open_at = c.bid if is_short else c.ask           # what the new leg pays / costs
        if open_at is None or open_at <= 0:
            continue
        if c.expiration in seen:
            continue  # an adjusted contract shares strike and expiry with the standard one
        seen.add(c.expiration)
        added = (c.expiration - expiration).days
        new_value = open_at * CONTRACT_MULTIPLIER * contracts
        new_intrinsic = intrinsic(target, spot, contracts, option_type)
        cash = new_value - unwind
        time_value = (new_value - new_intrinsic) - (unwind - here_intrinsic)
        if not is_short:
            cash, time_value = -cash, -time_value        # a long pays to extend

        warns: list[str] = []
        if new_intrinsic > here_intrinsic and is_short:
            warns.append(
                f"${target:g} is in the money at ${spot:,.2f} — part of this credit is intrinsic, "
                "money that arrives now and is handed back at expiry"
            )
        out.append(ExitOption(
            kind="roll", label=f"Roll to {c.expiration:%d %b}",
            credit=round(cash, 2), days=added, total_days=(c.expiration - today).days,
            collateral=committed(target, spot, contracts, option_type, is_short, abs(new_value)),
            extrinsic=round(time_value, 2), strike=target, expiration=c.expiration,
            collateral_delta=round(
                committed(target, spot, contracts, option_type, is_short, abs(new_value))
                - committed(strike, spot, contracts, option_type, is_short, unwind), 2),
            warnings=tuple(warns),
        ))
    return out


def write_after_assignment(
    chain: list[OptionContract], strike: float, contracts: float,
    spot: float | None, today: date, *,
    position_type: OptionType = OptionType.PUT, write_strike: float | None = None,
) -> list[ExitOption]:
    """What the freed capital would earn once assignment happens — priced by TENOR.

    Assignment turns one position into another, and the wheel's two halves mirror exactly:

    * a short PUT is assigned, leaving SHARES worth spot, against which you write CALLS;
    * a short CALL is assigned, the shares go at the strike, leaving CASH, with which you
      write PUTS.

    Either way the default strike is the position's own — for the put that is the basis you were
    handed, for the call the price you sold at — and the opposite side of the chain is quoted.

    Rows are WRITING TENORS priced from today's market, never the contract you would eventually
    sell. That contract holds more time value today than it will when the shares arrive: AVGO's
    47-day call reads $1,648 now and would be written with 21 days left, worth about $1,090.
    Pairing today's price with the post-assignment period inflated every rate by half. What a
    21-day option fetches today is the honest estimate of what a 21-day option fetches in three
    weeks, and it needs no decay model. Cross-checked against root-time decay of the real
    contract: $1,090 against $1,102, agreeing to 1%.
    """
    if spot is None or spot <= 0:
        return []
    written = OptionType.CALL if position_type is OptionType.PUT else OptionType.PUT
    # Capital afterwards: shares at market for an assigned put, the sale proceeds for a call.
    capital = (
        spot * CONTRACT_MULTIPLIER * contracts if position_type is OptionType.PUT
        else strike * CONTRACT_MULTIPLIER * contracts
    )
    here = strike * CONTRACT_MULTIPLIER * contracts

    listed = {c.strike for c in chain if c.option_type is written}
    if not listed:
        return []
    wanted = write_strike if write_strike is not None else strike
    target = wanted if wanted in listed else min(listed, key=lambda k: abs(k - wanted))

    out: list[ExitOption] = []
    seen: set[int] = set()
    for c in sorted(chain, key=lambda c: c.dte):
        if c.option_type is not written or c.strike != target:
            continue
        if c.bid is None or c.bid <= 0 or c.dte <= 0 or c.dte in seen:
            continue
        seen.add(c.dte)
        premium = c.bid * CONTRACT_MULTIPLIER * contracts
        itm = target < spot if written is OptionType.CALL else target > spot
        warns = (
            (f"${target:g} is in the money at ${spot:,.2f} — part of this premium is intrinsic, "
             f"and it would be assigned straight back",)
            if itm else ()
        )
        out.append(ExitOption(
            kind="assign_cc", label=f"{c.dte}-day ${target:g} {written.value}",
            credit=round(premium, 2), days=c.dte, collateral=capital,
            extrinsic=round(premium, 2), strike=target,
            collateral_delta=round(capital - here, 2), warnings=warns,
        ))
    return out


def compare(
    own: list[OptionContract], opposite: list[OptionContract], *,
    strike: float, expiration: date, contracts: float, spot: float | None, today: date,
    option_type: OptionType = OptionType.PUT, is_short: bool = True,
    roll_strike: float | None = None, call_strike: float | None = None,
) -> tuple[list[ExitOption], list[ExitOption]]:
    """``(alternatives, after_assignment)`` — two lists, because they are two questions.

    The alternatives are mutually exclusive things that can be done TODAY: keep, or roll. They
    are ranked against each other, best rate first, with keep as the baseline to beat.

    The after-assignment ladder is not an alternative to any of them. It is what the capital
    would earn once the put expires and the shares arrive, so it follows keeping rather than
    competing with it. Ranking the two together put "sell a call three days out" above "keep
    the position" as though a choice existed between them.
    """
    rows: list[ExitOption] = []
    base = keep(own, strike, expiration, contracts, spot, today,
                option_type=option_type, is_short=is_short)
    if base is not None:
        rows.append(base)
    rows.extend(rolls(own, strike, expiration, contracts, spot, today, roll_strike=roll_strike,
                      option_type=option_type, is_short=is_short))
    # Only a SHORT position in the money has an assignment to plan past. A long option is
    # exercised by choice, and one out of the money simply expires.
    later = (
        write_after_assignment(opposite, strike, contracts, spot, today,
                               position_type=option_type, write_strike=call_strike)
        if is_short and is_in_the_money(strike, spot, option_type) else []
    )
    return (
        sorted(rows, key=lambda r: (r.rate is None, -(r.rate or 0.0))),
        sorted(later, key=lambda r: r.days),
    )


def is_in_the_money(
    strike: float, spot: float | None, option_type: OptionType = OptionType.PUT
) -> bool:
    if spot is None:
        return False
    return spot < strike if option_type is OptionType.PUT else spot > strike


def option_type_for(kind: str) -> OptionType:
    return OptionType.CALL if kind == "assign_cc" else OptionType.PUT
