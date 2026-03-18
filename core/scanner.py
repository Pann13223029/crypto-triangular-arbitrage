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

    Deduplicates: won't emit the same triangle again within
    the cooldown window (default 5s).
    """

    def __init__(
        self,
        graph: TriangleGraph,
        calculator: ProfitCalculator,
        min_profit: float = 0.001,
        dedup_cooldown_ms: int = 5000,
    ):
        self.graph = graph
        self.calculator = calculator
        self.min_profit = min_profit
        self.dedup_cooldown_ms = dedup_cooldown_ms

        # Current price state
        self.tickers: dict[str, Ticker] = {}

        # Dedup: triangle_id → last emission timestamp (ms)
        self._last_emitted: dict[int, int] = {}

        # Stats
        self.total_ticks: int = 0
        self.total_scans: int = 0
        self.total_opportunities: int = 0
        self.total_deduped: int = 0

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

        # Deduplicate — don't re-emit the same triangle within cooldown
        now_ms = time_ns() // 1_000_000
        unique: list[Opportunity] = []
        for opp in opportunities:
            tri_id = opp.triangle.id
            last = self._last_emitted.get(tri_id, 0)
            if (now_ms - last) >= self.dedup_cooldown_ms:
                self._last_emitted[tri_id] = now_ms
                unique.append(opp)
            else:
                self.total_deduped += 1

        self.total_opportunities += len(unique)

        if unique:
            best = unique[0]
            logger.info(
                "Opportunity: %s %s (%.4f%%)",
                best.direction.value,
                " → ".join(best.triangle.assets),
                best.theoretical_profit * 100,
            )

        return unique

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
            "total_deduped": self.total_deduped,
            "tracked_symbols": len(self.tickers),
            "hit_rate": (
                f"{self.total_opportunities / max(self.total_scans, 1) * 100:.4f}%"
            ),
        }
