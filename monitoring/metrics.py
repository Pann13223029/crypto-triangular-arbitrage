"""Pipeline metrics — lightweight timing instrumentation."""

import logging
from collections import deque
from dataclasses import dataclass, field
from time import time_ns

logger = logging.getLogger(__name__)


@dataclass
class TradeMetric:
    """Timing for a single trade pipeline execution."""

    opportunity_detected_ms: int = 0
    risk_check_ms: int = 0
    execution_start_ms: int = 0
    execution_end_ms: int = 0
    net_pnl: float = 0.0
    symbol: str = ""
    aborted: bool = False

    @property
    def opportunity_age_ms(self) -> int:
        """How stale was the opportunity when execution started."""
        if self.execution_start_ms and self.opportunity_detected_ms:
            return self.execution_start_ms - self.opportunity_detected_ms
        return 0

    @property
    def execution_ms(self) -> int:
        """How long did execution take."""
        if self.execution_end_ms and self.execution_start_ms:
            return self.execution_end_ms - self.execution_start_ms
        return 0

    @property
    def total_pipeline_ms(self) -> int:
        """End-to-end from detection to fill."""
        if self.execution_end_ms and self.opportunity_detected_ms:
            return self.execution_end_ms - self.opportunity_detected_ms
        return 0


class PipelineMetrics:
    """
    Tracks timing metrics across the trading pipeline.

    Maintains a rolling window of recent trade metrics for
    performance monitoring and optimization.
    """

    def __init__(self, window_size: int = 100):
        self._trades: deque[TradeMetric] = deque(maxlen=window_size)
        self._total_trades: int = 0
        self._total_aborts: int = 0

        # Per-symbol P&L tracking
        self.pnl_by_symbol: dict[str, float] = {}
        self.trades_by_symbol: dict[str, int] = {}

    def record(self, metric: TradeMetric) -> None:
        """Record a trade metric."""
        self._trades.append(metric)
        self._total_trades += 1

        if metric.aborted:
            self._total_aborts += 1

        # Per-symbol tracking
        if metric.symbol:
            self.pnl_by_symbol[metric.symbol] = (
                self.pnl_by_symbol.get(metric.symbol, 0.0) + metric.net_pnl
            )
            self.trades_by_symbol[metric.symbol] = (
                self.trades_by_symbol.get(metric.symbol, 0) + 1
            )

    def stats(self) -> dict:
        """Aggregate timing statistics."""
        if not self._trades:
            return {
                "total_trades": 0,
                "avg_pipeline_ms": 0,
                "avg_execution_ms": 0,
                "avg_opportunity_age_ms": 0,
            }

        recent = list(self._trades)
        executed = [t for t in recent if not t.aborted]

        ages = [t.opportunity_age_ms for t in executed if t.opportunity_age_ms > 0]
        exec_times = [t.execution_ms for t in executed if t.execution_ms > 0]
        pipelines = [t.total_pipeline_ms for t in executed if t.total_pipeline_ms > 0]

        return {
            "total_trades": self._total_trades,
            "total_aborts": self._total_aborts,
            "abort_rate": (
                f"{self._total_aborts / max(self._total_trades, 1) * 100:.1f}%"
            ),
            "avg_pipeline_ms": (
                round(sum(pipelines) / len(pipelines)) if pipelines else 0
            ),
            "avg_execution_ms": (
                round(sum(exec_times) / len(exec_times)) if exec_times else 0
            ),
            "avg_opportunity_age_ms": (
                round(sum(ages) / len(ages)) if ages else 0
            ),
            "max_opportunity_age_ms": max(ages) if ages else 0,
        }

    def symbol_report(self) -> list[dict]:
        """P&L breakdown by symbol, sorted by profit."""
        report = []
        for symbol in self.pnl_by_symbol:
            report.append({
                "symbol": symbol,
                "pnl": round(self.pnl_by_symbol[symbol], 4),
                "trades": self.trades_by_symbol.get(symbol, 0),
                "avg_pnl": round(
                    self.pnl_by_symbol[symbol] /
                    max(self.trades_by_symbol.get(symbol, 1), 1), 4
                ),
            })
        report.sort(key=lambda x: -x["pnl"])
        return report
