"""The single application service that both the CLI and the future FastAPI call."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from wheel_screener.core.models import CandidateResult, ScreenCriteria, Underlying
from wheel_screener.core.pipeline.rate_fundamentals import rate_and_rank
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

    def screen_fundamentals(
        self, criteria: ScreenCriteria, today: date
    ) -> list[Underlying]:
        """M1: universe -> fundamental gate + cross-sectional rank -> ranked names."""
        universe = build_universe(self.fundamentals, criteria)
        return rate_and_rank(self.fundamentals, universe, criteria, today)

    def run_screen(self, criteria: ScreenCriteria) -> list[CandidateResult]:
        """Full pipeline (adds chain pull -> strike select -> yield rank).

        TODO(M2): build on ``screen_fundamentals`` with the Schwab chain stages.
        """
        raise NotImplementedError("Full candidate pipeline completes in M2")
