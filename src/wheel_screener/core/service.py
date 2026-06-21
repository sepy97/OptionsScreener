"""The single application service that both the CLI and the future FastAPI call."""

from __future__ import annotations

from dataclasses import dataclass

from wheel_screener.core.models import CandidateResult, ScreenCriteria
from wheel_screener.core.ports import ChainProvider, FundamentalsProvider


@dataclass
class ScreenerService:
    """Use-case entry point. Wires the pipeline over injected ports.

    Both delivery layers (CLI now, FastAPI later) call ``run_screen`` — no
    pipeline logic is duplicated anywhere else.
    """

    fundamentals: FundamentalsProvider
    chains: ChainProvider

    def run_screen(self, criteria: ScreenCriteria) -> list[CandidateResult]:
        """Run universe -> fundamental rating -> chain pull -> strike select -> rank.

        Wiring completes incrementally as the stages land (M1 universe+fundamentals
        -> M2 chains+contract selection -> M3 ranking/output).
        """
        raise NotImplementedError("Pipeline wiring completes across M1-M3")
