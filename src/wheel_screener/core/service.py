"""The single application service that both the CLI and the future FastAPI call."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from wheel_screener.core.models import (
    CandidateResult,
    ChainFilter,
    OptionType,
    ScreenCriteria,
    Underlying,
)
from wheel_screener.core.pipeline.pull_chains import pull_chains
from wheel_screener.core.pipeline.rank import rank
from wheel_screener.core.pipeline.rate_fundamentals import rate_and_rank
from wheel_screener.core.pipeline.select_strike import credited_premium, put_yield, select_put
from wheel_screener.core.pipeline.universe import build_universe
from wheel_screener.core.ports import ChainProvider, FundamentalsProvider


@dataclass
class ScreenerService:
    """Use-case entry point. Wires the pipeline over injected ports.

    Both delivery layers (CLI now, FastAPI later) call these methods — no pipeline
    logic is duplicated anywhere else.
    """

    fundamentals: FundamentalsProvider
    chains: ChainProvider

    def screen_fundamentals(self, criteria: ScreenCriteria, today: date) -> list[Underlying]:
        """Universe -> fundamental gate + cross-sectional rank -> ranked names."""
        universe = build_universe(self.fundamentals, criteria)
        return rate_and_rank(self.fundamentals, universe, criteria, today)

    def run_screen(self, criteria: ScreenCriteria, today: date) -> list[CandidateResult]:
        """Full pipeline: fundamentals -> chain pull -> ~target-delta put -> yield rank."""
        survivors = self.screen_fundamentals(criteria, today)
        filt = ChainFilter(
            option_type=OptionType.PUT,
            # pull a padded window so monthly-only names (no expiry exactly in [min,max])
            # still surface their nearest monthly; select_put prefers in-band expiries.
            min_dte=max(criteria.min_dte - criteria.dte_tolerance, 1),
            max_dte=criteria.max_dte + criteria.dte_tolerance,
            min_open_interest=criteria.min_open_interest,
            target_delta=criteria.target_delta,
        )
        chains = pull_chains(self.chains, survivors, filt)

        candidates: list[CandidateResult] = []
        for u in survivors:
            snapshot = chains.get(u.symbol)
            if snapshot is None:
                continue
            put = select_put(snapshot, criteria)
            if put is None:
                continue
            candidates.append(
                CandidateResult(
                    symbol=u.symbol,
                    contract=put,
                    fundamental_score=u.fundamental_score,
                    annualized_yield=put_yield(put),
                    premium=credited_premium(put),  # conservative: the bid
                    collateral=put.strike * 100,
                    next_earnings=u.next_earnings,
                    has_weeklys=u.has_weeklys,
                )
            )

        if criteria.min_annualized_yield is not None:
            floor = criteria.min_annualized_yield
            candidates = [c for c in candidates if (c.annualized_yield or 0.0) >= floor]
        return rank(candidates, criteria.fundamental_weight)
