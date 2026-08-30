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
    return ExitOption(
        kind="keep", label="Keep to expiry",
        credit=round(earnable, 2), days=(expiration - today).days,
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
    for c in puts:
        if c.strike != target or c.expiration <= expiration or c.bid is None or c.bid <= 0:
            continue
        added = (c.expiration - expiration).days
        collateral = target * CONTRACT_MULTIPLIER * contracts
        credit = c.bid * CONTRACT_MULTIPLIER * contracts - cost_to_close
        time_value = (
            c.bid * CONTRACT_MULTIPLIER * contracts - intrinsic(target, spot, contracts)
        ) - (cost_to_close - intrinsic(strike, spot, contracts))
        warns: list[str] = []
        if target > strike:
            warns.append("sells intrinsic, not time — the credit is an obligation you expect to "
                         "hand back")
        if collateral > here:
            warns.append(f"commits ${collateral - here:,.0f} more collateral")
        out.append(ExitOption(
            kind="roll", label=f"Roll to {c.expiration:%d %b}",
            credit=round(credit, 2), days=added, collateral=collateral,
            extrinsic=round(time_value, 2), strike=target, expiration=c.expiration,
            collateral_delta=round(collateral - here, 2), warnings=tuple(warns),
        ))
    return out


def covered_calls(
    calls: list[OptionContract], strike: float, contracts: float,
    spot: float | None, today: date,
) -> list[ExitOption]:
    """Take assignment, then sell a call against the shares.

    Only meaningful once the put is in the money — otherwise assignment is not the likely
    outcome and this compares against a position the holder would not have. Collateral becomes
    the shares' market value, which is what the capital is now worth rather than what it cost.
    """
    if spot is None or spot <= 0 or spot >= strike:
        return []
    shares_value = spot * CONTRACT_MULTIPLIER * contracts
    here = strike * CONTRACT_MULTIPLIER * contracts
    out: list[ExitOption] = []
    for c in calls:
        if c.bid is None or c.bid <= 0 or c.dte <= 0:
            continue
        premium = c.bid * CONTRACT_MULTIPLIER * contracts
        warns: list[str] = []
        if c.strike < strike:
            warns.append("strike below your cost — being called away locks in a loss on the "
                         "shares")
        out.append(ExitOption(
            kind="assign_cc", label=f"Assign, sell ${c.strike:g} call {c.expiration:%d %b}",
            credit=round(premium, 2), days=c.dte, collateral=shares_value,
            extrinsic=round(premium, 2), strike=c.strike, expiration=c.expiration,
            collateral_delta=round(shares_value - here, 2), warnings=tuple(warns),
        ))
    return out


def compare(
    puts: list[OptionContract], calls: list[OptionContract], *,
    strike: float, expiration: date, contracts: float, spot: float | None, today: date,
    roll_strike: float | None = None, call_strike: float | None = None,
) -> list[ExitOption]:
    """Every action, best rate first. ``keep`` is always included as the baseline to beat."""
    rows: list[ExitOption] = []
    base = keep(puts, strike, expiration, contracts, spot, today)
    if base is not None:
        rows.append(base)
    rows.extend(rolls(puts, strike, expiration, contracts, spot, today,
                      roll_strike=roll_strike))
    cc = covered_calls(calls, strike, contracts, spot, today)
    if call_strike is not None:
        cc = [r for r in cc if r.strike == call_strike]
    rows.extend(cc)
    return sorted(rows, key=lambda r: (r.rate is None, -(r.rate or 0.0)))


def is_in_the_money(strike: float, spot: float | None) -> bool:
    return spot is not None and spot < strike


def option_type_for(kind: str) -> OptionType:
    return OptionType.CALL if kind == "assign_cc" else OptionType.PUT
