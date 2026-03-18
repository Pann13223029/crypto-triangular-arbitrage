"""Order manager — tracks active and historical orders."""

import logging
from core.models import Order, OrderStatus, TradeResult

logger = logging.getLogger(__name__)


class OrderManager:
    """Tracks all orders and trade results for the session."""

    def __init__(self):
        self.active_orders: list[Order] = []
        self.completed_orders: list[Order] = []
        self.trade_results: list[TradeResult] = []

    def record_result(self, result: TradeResult) -> None:
        """Record a completed triangle trade result."""
        self.trade_results.append(result)
        for order in result.orders:
            if order.status == OrderStatus.FILLED:
                self.completed_orders.append(order)

    @property
    def total_trades(self) -> int:
        return len(self.trade_results)

    @property
    def successful_trades(self) -> int:
        return sum(1 for r in self.trade_results if not r.aborted and r.net_pnl >= 0)

    @property
    def failed_trades(self) -> int:
        return sum(1 for r in self.trade_results if r.aborted)

    @property
    def total_pnl(self) -> float:
        return sum(r.net_pnl for r in self.trade_results if not r.aborted)

    @property
    def total_fees(self) -> float:
        return sum(r.total_fees for r in self.trade_results)

    @property
    def win_rate(self) -> float:
        completed = [r for r in self.trade_results if not r.aborted]
        if not completed:
            return 0.0
        wins = sum(1 for r in completed if r.net_pnl >= 0)
        return wins / len(completed)

    def stats(self) -> dict:
        return {
            "total_trades": self.total_trades,
            "successful": self.successful_trades,
            "failed": self.failed_trades,
            "win_rate": f"{self.win_rate:.1%}",
            "total_pnl": round(self.total_pnl, 6),
            "total_fees": round(self.total_fees, 6),
            "total_orders": len(self.completed_orders),
        }
