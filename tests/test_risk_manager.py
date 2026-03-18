"""Tests for risk manager (additional edge cases)."""

import pytest

from config.settings import TradingConfig
from core.models import Direction, Opportunity, TradingPair
from core.triangle import TriangleGraph
from execution.risk_manager import RiskManager


def make_triangle():
    graph = TriangleGraph()
    graph.load_pairs([
        TradingPair(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT"),
        TradingPair(symbol="ETHUSDT", base_asset="ETH", quote_asset="USDT"),
        TradingPair(symbol="ETHBTC", base_asset="ETH", quote_asset="BTC"),
    ])
    return graph.discover_triangles()[0]


def make_opp(profit: float = 0.005):
    return Opportunity(
        triangle=make_triangle(),
        direction=Direction.FORWARD,
        theoretical_profit=profit,
    )


class TestRiskManagerEdgeCases:

    def test_kill_is_persistent(self):
        """Once killed, all subsequent checks fail."""
        rm = RiskManager()
        rm.kill("test reason")

        opp = make_opp()
        for _ in range(5):
            approved, _ = rm.check(opp)
            assert not approved

    def test_stats_tracking(self):
        rm = RiskManager()
        opp = make_opp()

        rm.check(opp)  # Approved
        rm.check(opp)  # Approved

        assert rm.total_approved == 2
        assert rm.total_rejected == 0

    def test_on_trade_start_end(self):
        rm = RiskManager(TradingConfig(max_open_triangles=2))

        rm.on_trade_start()
        assert rm.open_triangles == 1
        rm.on_trade_start()
        assert rm.open_triangles == 2

        rm.on_trade_end()
        assert rm.open_triangles == 1
        rm.on_trade_end()
        assert rm.open_triangles == 0

        # Doesn't go negative
        rm.on_trade_end()
        assert rm.open_triangles == 0

    def test_daily_pnl_accumulates(self):
        rm = RiskManager(TradingConfig(daily_loss_limit_usd=100.0))

        rm.record_trade_result(5.0)
        rm.record_trade_result(-2.0)
        rm.record_trade_result(3.0)

        assert rm.daily_pnl == 6.0
        assert not rm.killed
