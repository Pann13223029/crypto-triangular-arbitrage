"""Replay recorded data through the scanner for backtesting."""

import csv
import logging
from pathlib import Path

from core.calculator import ProfitCalculator
from core.models import Ticker, TradingPair
from core.scanner import TriangleScanner
from core.triangle import TriangleGraph

logger = logging.getLogger(__name__)


class Replayer:
    """
    Replays recorded ticker CSV data through the scanner.

    Useful for backtesting strategy parameters (thresholds,
    fee rates) against historical market data.
    """

    def __init__(
        self,
        pairs: list[TradingPair],
        fee_rate: float = 0.00075,
        min_profit: float = 0.001,
        max_triangles: int = 5000,
    ):
        self.graph = TriangleGraph()
        self.graph.load_pairs(pairs)
        self.graph.discover_triangles(max_triangles=max_triangles)

        calculator = ProfitCalculator(fee_rate=fee_rate)
        self.scanner = TriangleScanner(self.graph, calculator, min_profit=min_profit)

        self.opportunities_found: list[dict] = []
        self.total_rows = 0

    def replay_file(self, ticker_csv_path: str) -> list[dict]:
        """
        Replay a ticker CSV file through the scanner.

        Args:
            ticker_csv_path: Path to a recorded tickers CSV.

        Returns:
            List of opportunity dicts found during replay.
        """
        path = Path(ticker_csv_path)
        if not path.exists():
            raise FileNotFoundError(f"Ticker file not found: {path}")

        logger.info("Replaying %s...", path)
        opportunities = []

        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                self.total_rows += 1

                ticker = Ticker(
                    symbol=row["symbol"],
                    bid=float(row["bid"]),
                    ask=float(row["ask"]),
                    timestamp_ms=int(row["timestamp_ms"]),
                )

                opps = self.scanner.update_ticker(ticker)
                for opp in opps:
                    entry = {
                        "timestamp_ms": ticker.timestamp_ms,
                        "triangle": " → ".join(opp.triangle.assets),
                        "direction": opp.direction.value,
                        "profit": opp.theoretical_profit,
                    }
                    opportunities.append(entry)

        self.opportunities_found.extend(opportunities)
        logger.info(
            "Replay complete — %d rows, %d opportunities",
            self.total_rows, len(opportunities),
        )
        return opportunities

    def summary(self) -> dict:
        """Summary statistics from replay."""
        profits = [o["profit"] for o in self.opportunities_found]

        if not profits:
            return {
                "total_rows": self.total_rows,
                "total_opportunities": 0,
                "scanner_stats": self.scanner.stats(),
            }

        return {
            "total_rows": self.total_rows,
            "total_opportunities": len(profits),
            "avg_profit": sum(profits) / len(profits),
            "max_profit": max(profits),
            "min_profit": min(profits),
            "total_theoretical_profit": sum(profits),
            "scanner_stats": self.scanner.stats(),
        }
