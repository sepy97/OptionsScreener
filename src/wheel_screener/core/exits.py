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


def intrinsic(strike: float, spot: float | None, contracts: float) -> float:
    """What a short put owes at today's price, if it were settled now."""
    if spot is None:
        return 0.0
    return max(0.0, strike - spot) * CONTRACT_MULTIPLIER * contracts


def _match(chain: list[OptionContract], strike: float, expiration: date) -> OptionContract | None:
    return next(
        (c for c in chain if c.strike == strike and c.expiration == expiration), None
    )


def keep(
    puts: list[OptionContract], strike: float, expiration: date, contracts: float,
    spot: float | None, today: date,
) -> ExitOption | None:
    """Hold to expiry: you earn the remaining extrinsic, and the collateral stays committed.

    Priced off the ASK, because closing means buying the contract back and that is what it would
    cost. Using the mid would flatter every hold.
    """
    cur = _match(puts, strike, expiration)
    if cur is None or cur.ask is None:
        return None
    collateral = strike * CONTRACT_MULTIPLIER * contracts
    cost_to_close = cur.ask * CONTRACT_MULTIPLIER * contracts
    earnable = cost_to_close - intrinsic(strike, spot, contracts)
    remaining = (expiration - today).days
    return ExitOption(
        kind="keep", label="Keep to expiry",
        credit=round(earnable, 2), days=remaining, total_days=remaining,
        collateral=collateral, extrinsic=round(earnable, 2),
        strike=strike, expiration=expiration,
    )


def rolls(
    puts: list[OptionContract], strike: float, expiration: date, contracts: float,
    spot: float | None, today: date, *, roll_strike: float | None = None,
) -> list[ExitOption]:
    """Close the current put and sell a later one, for every expiry on the board.

    ``roll_strike`` defaults to the position's own strike — the only comparison that is purely
    an extension of time. A different strike is offered because traders do it, and is annotated
    rather than hidden.
    """
    cur = _match(puts, strike, expiration)
    if cur is None or cur.ask is None:
        return []
    target = strike if roll_strike is None else roll_strike
    cost_to_close = cur.ask * CONTRACT_MULTIPLIER * contracts
    here = strike * CONTRACT_MULTIPLIER * contracts

    out: list[ExitOption] = []
    seen: set[date] = set()
    for c in sorted(puts, key=lambda c: (c.expiration, -(c.bid or 0.0))):
        if c.strike != target or c.expiration <= expiration or c.bid is None or c.bid <= 0:
            continue
        if c.expiration in seen:
            continue  # an adjusted contract shares strike and expiry with the standard one
        seen.add(c.expiration)
        added = (c.expiration - expiration).days
        collateral = target * CONTRACT_MULTIPLIER * contracts
        credit = c.bid * CONTRACT_MULTIPLIER * contracts - cost_to_close
        time_value = (
            c.bid * CONTRACT_MULTIPLIER * contracts - intrinsic(target, spot, contracts)
        ) - (cost_to_close - intrinsic(strike, spot, contracts))
        # ONE warning, and only the one the numbers cannot show. That the collateral grew is
        # already in the collateral column; saying it again in prose on every row of a ladder
        # buried the figures. That part of the credit is intrinsic is the thing a reader cannot
        # see, because it looks exactly like income until expiry.
        #
        # A HIGHER strike is not the same as an in-the-money one. Rolling $150 -> $155 while the
        # stock trades at $164 sells no intrinsic whatever, and warning there teaches the reader
        # to ignore the warning that matters. Compare the obligations, not the strikes.
        warns: list[str] = []
        if intrinsic(target, spot, contracts) > intrinsic(strike, spot, contracts):
            warns.append(
                f"${target:g} is in the money at ${spot:,.2f} — part of this credit is intrinsic, "
                "money that arrives now and is handed back at expiry"
            )
        out.append(ExitOption(
            kind="roll", label=f"Roll to {c.expiration:%d %b}",
            credit=round(credit, 2), days=added, total_days=(c.expiration - today).days,
            collateral=collateral,
            extrinsic=round(time_value, 2), strike=target, expiration=c.expiration,
            collateral_delta=round(collateral - here, 2), warnings=tuple(warns),
        ))
    return out


def covered_calls(
    calls: list[OptionContract], strike: float, contracts: float,
    spot: float | None, today: date, *, call_strike: float | None = None,
) -> list[ExitOption]:
    """What writing calls would pay after assignment — estimated by TENOR, not by contract.

    The obvious implementation quotes the contract you would actually sell, and is wrong. A call
    expiring 16 Oct is worth $1,648 today with 47 days of life in it, but it would be written on
    25 Sep with only 21 days left, by which time it is worth about $1,090. Pairing today's price
    with the post-assignment holding period inflated every rate by half — 77%/yr where 51% was
    real. You cannot quote a trade you will make in a month.

    So each row is a WRITING TENOR priced from today's market: what a 21-day call fetches today
    is the honest estimate of what a 21-day call fetches in three weeks, holding price and
    volatility fixed. It uses only quoted bids — no decay model — and it keeps moneyness fixed,
    since the same strike against the same spot is the same distance out.

    Cross-checked against the alternative: decaying the 16 Oct contract's extrinsic by root-time
    gives $1,102 against the tenor proxy's $1,090, a 1.1% disagreement. Two independent methods
    agreeing is the reason to trust either.

    ``expiration`` is deliberately left unset. These are tenors, not tradeable contracts, and
    printing a date invites exactly the reading — "sell the 02 Sep call" — that started this.

    The same logic mirrors for a short call that gets assigned: the shares go, and the put you
    would write afterwards is estimated the same way.
    """
    if spot is None or spot <= 0 or spot >= strike:
        return []
    listed = {c.strike for c in calls}
    target = call_strike if call_strike is not None else (
        strike if strike in listed
        else min(listed, key=lambda k: abs(k - strike)) if listed else None
    )
    if target is None:
        return []

    shares_value = spot * CONTRACT_MULTIPLIER * contracts
    here = strike * CONTRACT_MULTIPLIER * contracts
    out: list[ExitOption] = []
    seen: set[int] = set()
    for c in sorted(calls, key=lambda c: c.dte):
        if c.strike != target or c.bid is None or c.bid <= 0 or c.dte <= 0:
            continue
        if c.dte in seen:
            continue
        seen.add(c.dte)
        premium = c.bid * CONTRACT_MULTIPLIER * contracts
        warns: list[str] = []
        if target < spot:
            warns.append(
                f"${target:g} is in the money at ${spot:,.2f} — part of this premium is "
                "intrinsic, and the shares would be called away"
            )
        out.append(ExitOption(
            kind="assign_cc", label=f"{c.dte}-day ${target:g} call",
            credit=round(premium, 2), days=c.dte, collateral=shares_value,
            extrinsic=round(premium, 2), strike=target,
            collateral_delta=round(shares_value - here, 2), warnings=tuple(warns),
        ))
    return out


def compare(
    puts: list[OptionContract], calls: list[OptionContract], *,
    strike: float, expiration: date, contracts: float, spot: float | None, today: date,
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
    base = keep(puts, strike, expiration, contracts, spot, today)
    if base is not None:
        rows.append(base)
    rows.extend(rolls(puts, strike, expiration, contracts, spot, today,
                      roll_strike=roll_strike))
    later = covered_calls(calls, strike, contracts, spot, today, call_strike=call_strike)
    return (
        sorted(rows, key=lambda r: (r.rate is None, -(r.rate or 0.0))),
        sorted(later, key=lambda r: r.days),
    )


def is_in_the_money(strike: float, spot: float | None) -> bool:
    return spot is not None and spot < strike


def option_type_for(kind: str) -> OptionType:
    return OptionType.CALL if kind == "assign_cc" else OptionType.PUT
