"""Tests for executor and risk manager."""

import pytest
import asyncio

from config.settings import TradingConfig, FeeConfig, SimulationConfig
from core.models import (
    Direction,
    Opportunity,
    OrderSide,
    OrderStatus,
    Ticker,
    TradingPair,
)
from core.triangle import TriangleGraph
from exchange.simulator import SimulatedExchange
from execution.executor import Executor
from execution.risk_manager import RiskManager


# --- Helpers ---

def make_ticker(symbol: str, bid: float, ask: float) -> Ticker:
    return Ticker(symbol=symbol, bid=bid, ask=ask, timestamp_ms=0)


def build_setup():
    """Build a triangle + exchange + executor for testing."""
    pairs = [
        TradingPair(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT"),
        TradingPair(symbol="ETHUSDT", base_asset="ETH", quote_asset="USDT"),
        TradingPair(symbol="ETHBTC", base_asset="ETH", quote_asset="BTC"),
    ]

    graph = TriangleGraph()
    graph.load_pairs(pairs)
    triangles = graph.discover_triangles()
    tri = triangles[0]

    fee_config = FeeConfig(taker_fee=0.001, use_bnb_fee=False)
    sim_config = SimulationConfig(
        initial_balances={"USDT": 10000.0, "BTC": 1.0, "ETH": 10.0},
        latency_ms=0,  # No delay for tests
        fixed_slippage=0.0,  # No slippage for predictable tests
    )

    exchange = SimulatedExchange(fee_config, sim_config)
    exchange.load_pairs(pairs)

    # Inject prices
    exchange.inject_ticker(make_ticker("BTCUSDT", 67000.0, 67000.0))
    exchange.inject_ticker(make_ticker("ETHUSDT", 3600.0, 3600.0))
    exchange.inject_ticker(make_ticker("ETHBTC", 0.050, 0.050))

    risk_manager = RiskManager(TradingConfig(
        max_position_size_usd=500.0,
        slippage_tolerance=0.01,  # 1% — generous for tests
    ))
    executor = Executor(exchange, risk_manager, risk_manager.config, fee_config)

    return tri, exchange, executor, risk_manager


# --- Risk Manager Tests ---

class TestRiskManager:

    def test_approve_valid_opportunity(self):
        tri, _, _, rm = build_setup()
        opp = Opportunity(
            triangle=tri,
            direction=Direction.FORWARD,
            theoretical_profit=0.005,
        )
        approved, reason = rm.check(opp)
        assert approved
        assert reason == "Approved"

    def test_reject_below_threshold(self):
        rm = RiskManager(TradingConfig(min_profit_threshold=0.01))
        tri, _, _, _ = build_setup()
        opp = Opportunity(
            triangle=tri,
            direction=Direction.FORWARD,
            theoretical_profit=0.001,
        )
        approved, _ = rm.check(opp)
        assert not approved

    def test_daily_loss_kills(self):
        rm = RiskManager(TradingConfig(daily_loss_limit_usd=10.0))
        rm.daily_pnl = -15.0

        tri, _, _, _ = build_setup()
        opp = Opportunity(triangle=tri, direction=Direction.FORWARD, theoretical_profit=0.01)
        approved, reason = rm.check(opp)
        assert not approved
        assert rm.killed

    def test_consecutive_losses_kills(self):
        rm = RiskManager(TradingConfig(max_consecutive_losses=3))
        rm.consecutive_losses = 3

        tri, _, _, _ = build_setup()
        opp = Opportunity(triangle=tri, direction=Direction.FORWARD, theoretical_profit=0.01)
        approved, _ = rm.check(opp)
        assert not approved
        assert rm.killed

    def test_cooldown_rejects(self):
        from time import time_ns
        rm = RiskManager(TradingConfig(cooldown_after_loss_sec=60.0))
        rm.last_loss_time_ms = time_ns() // 1_000_000  # Just now

        tri, _, _, _ = build_setup()
        opp = Opportunity(triangle=tri, direction=Direction.FORWARD, theoretical_profit=0.01)
        approved, reason = rm.check(opp)
        assert not approved
        assert "Cooldown" in reason

    def test_ws_unhealthy_kills(self):
        rm = RiskManager()
        tri, _, _, _ = build_setup()
        opp = Opportunity(triangle=tri, direction=Direction.FORWARD, theoretical_profit=0.01)
        approved, _ = rm.check(opp, ws_healthy=False)
        assert not approved
        assert rm.killed

    def test_max_open_triangles(self):
        rm = RiskManager(TradingConfig(max_open_triangles=1))
        rm.open_triangles = 1

        tri, _, _, _ = build_setup()
        opp = Opportunity(triangle=tri, direction=Direction.FORWARD, theoretical_profit=0.01)
        approved, _ = rm.check(opp)
        assert not approved

    def test_record_profit_resets_losses(self):
        rm = RiskManager()
        rm.consecutive_losses = 2
        rm.record_trade_result(0.5)
        assert rm.consecutive_losses == 0

    def test_record_loss_increments(self):
        rm = RiskManager()
        rm.record_trade_result(-0.1)
        assert rm.consecutive_losses == 1
        rm.record_trade_result(-0.2)
        assert rm.consecutive_losses == 2

    def test_reset_daily(self):
        rm = RiskManager()
        rm.daily_pnl = -20.0
        rm.killed = True
        rm.reset_daily()
        assert rm.daily_pnl == 0.0
        assert not rm.killed


# --- Executor Tests ---

class TestExecutor:

    @pytest.mark.asyncio
    async def test_successful_execution(self):
        tri, exchange, executor, _ = build_setup()

        opp = Opportunity(
            triangle=tri,
            direction=Direction.FORWARD,
            theoretical_profit=0.05,
        )

        result = await executor.execute(opp)
        assert not result.aborted
        assert len(result.orders) == 3
        assert all(o.status == OrderStatus.FILLED for o in result.orders)

    @pytest.mark.asyncio
    async def test_execution_updates_balances(self):
        tri, exchange, executor, _ = build_setup()

        initial_usdt = await exchange.get_balance("USDT")

        opp = Opportunity(
            triangle=tri,
            direction=Direction.FORWARD,
            theoretical_profit=0.05,
        )

        result = await executor.execute(opp)
        final_usdt = await exchange.get_balance("USDT")

        # Balance should have changed
        assert final_usdt != initial_usdt

    @pytest.mark.asyncio
    async def test_insufficient_balance_aborts(self):
        tri, exchange, executor, _ = build_setup()

        # Drain balance
        exchange.balances["USDT"] = 0.0

        opp = Opportunity(
            triangle=tri,
            direction=Direction.FORWARD,
            theoretical_profit=0.05,
        )

        result = await executor.execute(opp)
        assert result.aborted


# --- Simulated Exchange Tests ---

class TestSimulatedExchange:

    @pytest.mark.asyncio
    async def test_buy_order(self):
        _, exchange, _, _ = build_setup()

        order = await exchange.place_order(
            "BTCUSDT", OrderSide.BUY, 0.001, None
        )
        assert order.status == OrderStatus.FILLED
        assert order.actual_price > 0
        assert order.fee > 0

    @pytest.mark.asyncio
    async def test_sell_order(self):
        _, exchange, _, _ = build_setup()

        # First buy some BTC
        await exchange.place_order("BTCUSDT", OrderSide.BUY, 0.01)
        # Then sell
        order = await exchange.place_order("BTCUSDT", OrderSide.SELL, 0.005)
        assert order.status == OrderStatus.FILLED

    @pytest.mark.asyncio
    async def test_insufficient_balance_fails(self):
        _, exchange, _, _ = build_setup()

        order = await exchange.place_order("BTCUSDT", OrderSide.SELL, 100.0)
        assert order.status == OrderStatus.FAILED

    @pytest.mark.asyncio
    async def test_unknown_symbol_fails(self):
        _, exchange, _, _ = build_setup()

        order = await exchange.place_order("FOOBAR", OrderSide.BUY, 1.0)
        assert order.status == OrderStatus.FAILED

    @pytest.mark.asyncio
    async def test_balance_tracking(self):
        _, exchange, _, _ = build_setup()

        initial = await exchange.get_balance("USDT")
        await exchange.place_order("BTCUSDT", OrderSide.BUY, 0.001)
        after = await exchange.get_balance("USDT")

        assert after < initial  # Spent USDT
        btc = await exchange.get_balance("BTC")
        assert btc > 0  # Got BTC

    @pytest.mark.asyncio
    async def test_reset_balances(self):
        _, exchange, _, _ = build_setup()

        await exchange.place_order("BTCUSDT", OrderSide.BUY, 0.01)
        exchange.reset_balances()

        usdt = await exchange.get_balance("USDT")
        assert usdt == exchange.sim_config.initial_balances["USDT"]
