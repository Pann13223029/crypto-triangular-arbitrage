"""Real-time triangle scanner — detects opportunities on price updates."""

import logging
from time import time_ns

from core.calculator import ProfitCalculator
from core.models import Opportunity, Ticker, Triangle
from core.triangle import TriangleGraph

logger = logging.getLogger(__name__)


class TriangleScanner:
    """
    Scans for arbitrage opportunities in real-time.

    On each price tick, only recalculates triangles affected by the
    updated symbol — not all triangles.
    """

    def __init__(
        self,
        graph: TriangleGraph,
        calculator: ProfitCalculator,
        min_profit: float = 0.001,
    ):
        self.graph = graph
        self.calculator = calculator
        self.min_profit = min_profit

        # Current price state
        self.tickers: dict[str, Ticker] = {}

        # Stats
        self.total_ticks: int = 0
        self.total_scans: int = 0
        self.total_opportunities: int = 0

    def update_ticker(self, ticker: Ticker) -> list[Opportunity]:
        """
        Process a price update and scan affected triangles.

        Args:
            ticker: Updated price data for a symbol.

        Returns:
            List of profitable opportunities found (may be empty).
        """
        self.total_ticks += 1
        self.tickers[ticker.symbol] = ticker

        # Get only triangles affected by this symbol
        affected = self.graph.get_affected_triangles(ticker.symbol)
        if not affected:
            return []

        self.total_scans += len(affected)

        # Batch calculate profits for affected triangles
        opportunities = self.calculator.batch_calculate(
            triangles=affected,
            tickers=self.tickers,
            min_profit=self.min_profit,
        )

        self.total_opportunities += len(opportunities)

        if opportunities:
            best = opportunities[0]
            logger.info(
                "Found %d opportunities (best: %.4f%% on %s)",
                len(opportunities),
                best.theoretical_profit * 100,
                " → ".join(best.triangle.assets),
            )

        return opportunities

    def bulk_update(self, tickers: list[Ticker]) -> list[Opportunity]:
        """
        Process multiple ticker updates at once.

        Deduplicates affected triangles across all updates
        and runs a single batch calculation.
        """
        # Update all prices first
        affected_set: set[int] = set()
        for ticker in tickers:
            self.total_ticks += 1
            self.tickers[ticker.symbol] = ticker

            for tri in self.graph.get_affected_triangles(ticker.symbol):
                affected_set.add(tri.id)

        if not affected_set:
            return []

        # Gather unique affected triangles
        affected_triangles = [
            tri for tri in self.graph.triangles if tri.id in affected_set
        ]
        self.total_scans += len(affected_triangles)

        opportunities = self.calculator.batch_calculate(
            triangles=affected_triangles,
            tickers=self.tickers,
            min_profit=self.min_profit,
        )

        self.total_opportunities += len(opportunities)
        return opportunities

    def stats(self) -> dict:
        """Scanner performance statistics."""
        return {
            "total_ticks": self.total_ticks,
            "total_triangle_scans": self.total_scans,
            "total_opportunities": self.total_opportunities,
            "tracked_symbols": len(self.tickers),
            "hit_rate": (
                f"{self.total_opportunities / max(self.total_scans, 1) * 100:.4f}%"
            ),
        }
