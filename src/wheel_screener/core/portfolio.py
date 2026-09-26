"""One person's brokerage account: reading it, and the opinions that are about their positions.

Split out of :class:`~wheel_screener.core.service.ScreenerService` for one reason — a screen is
shared and an account is not. The same screen serves everybody, so the service that runs it is
built once and reused for the life of the process; an account belongs to exactly one person, and
the credential that reads it has to arrive *with the request* rather than sit on a process-wide
object where a later caller can pick it up by accident.

That is the whole point of the split, and it is worth stating as an invariant: after it,
``ScreenerService`` has no field that belongs to a user, so "did this request use the right
person's data?" has exactly one answer — whatever this object was constructed with. It cannot be
got wrong by forgetting to pass an argument.

The algorithms stay on the screener. Pricing a position, judging early assignment and deciding
keep-or-swap are all functions of a position plus market data, not of who holds it, so this class
delegates rather than duplicating them. The only thing it owns is the credential.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from wheel_screener.core.errors import ProviderUnavailableError
from wheel_screener.core.models import (
    BrokerageAccount,
    CandidateResult,
    Position,
    ScreenCriteria,
)
from wheel_screener.core.ports import BrokerageAccountProvider
from wheel_screener.core.service import ScreenerService


@dataclass
class PortfolioService:
    """The account-facing use cases, bound to ONE person's broker credential.

    ``accounts`` is None when this deployment (or, once there are users, this person) has no
    broker linked. Built per request — see ``api.deps.get_portfolio`` — so the credential's
    lifetime is the request's.
    """

    accounts: BrokerageAccountProvider | None
    screener: ScreenerService

    def brokerage_accounts(self) -> list[BrokerageAccount]:
        """Balances and positions for every linked brokerage account, priced.

        Raises ``ProviderUnavailableError`` when no broker is linked, rather than returning an
        empty list: "nothing connected" and "connected but you hold nothing" are different
        answers and the caller must be able to tell them apart.
        """
        if self.accounts is None:
            raise ProviderUnavailableError("no brokerage account is linked to this deployment")
        accounts = self.accounts.accounts()
        self.screener.price_positions(accounts)
        return accounts

    def swap_reviews(
        self,
        positions: list[Position],
        candidates: list[CandidateResult] | None,
        today: date,
        criteria: ScreenCriteria | None = None,
    ) -> None:
        """Stamp each open short option with its keep/swap verdict. See ``core.swap``."""
        self.screener.swap_reviews(positions, candidates, today, criteria)
