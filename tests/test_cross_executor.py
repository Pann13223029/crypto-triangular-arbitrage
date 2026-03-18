"""Tests for CrossExchangeExecutor and CrossExchangeRiskManager."""

import pytest

from config.settings import CrossExchangeConfig, FeeConfig, SimulationConfig, TradingConfig
from core.models import OrderStatus, Ticker, TradingPair
from cross_exchange.executor import CrossExchangeExecutor
from cross_exchange.models import CrossExchangeOpportunity, CrossTradeStatus
from cross_exchange.risk_manager import CrossExchangeRiskManager
from exchange.simulator import SimulatedExchange


def make_exchange(ex_id: str, usdt: float = 10000.0, btc: float = 0.15) -> SimulatedExchange:
    fee = FeeConfig(taker_fee=0.001, use_bnb_fee=False)
    sim = SimulationConfig(
        initial_balances={"USDT": usdt, "BTC": btc, "ETH": 4.0},
        latency_ms=0,
        fixed_slippage=0.0,
    )
    ex = SimulatedExchange(fee, sim, exchange_id=ex_id)
    ex.load_pairs([
        TradingPair(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT"),
        TradingPair(symbol="ETHUSDT", base_asset="ETH", quote_asset="USDT"),
    ])
    return ex


def make_opp(symbol="BTCUSDT", buy_ex="ex_a", sell_ex="ex_b",
             buy_price=67000.0, sell_price=67200.0) -> CrossExchangeOpportunity:
    gross = (sell_price - buy_price) / buy_price
    return CrossExchangeOpportunity(
        symbol=symbol,
        buy_exchange=buy_ex,
        sell_exchange=sell_ex,
        buy_price=buy_price,
        sell_price=sell_price,
        gross_spread=gross,
        net_spread=gross - 0.002,
    )


class TestCrossExchangeExecutor:

    @pytest.mark.asyncio
    async def test_both_filled(self):
        ex_a = make_exchange("ex_a")
        ex_b = make_exchange("ex_b")

        # Inject prices
        ex_a.inject_ticker(Ticker("BTCUSDT", 67000, 67000, 0))
        ex_b.inject_ticker(Ticker("BTCUSDT", 67200, 67200, 0))

        executor = CrossExchangeExecutor(
            exchanges={"ex_a": ex_a, "ex_b": ex_b},
            cx_config=CrossExchangeConfig(max_position_size_usd=500),
        )

        opp = make_opp()
        result = await executor.execute(opp)

        assert result.status == CrossTradeStatus.COMPLETED
        assert result.buy_order is not None
        assert result.sell_order is not None
        assert result.buy_order.status == OrderStatus.FILLED
        assert result.sell_order.status == OrderStatus.FILLED
        assert executor.total_both_filled == 1

    @pytest.mark.asyncio
    async def test_pnl_positive_on_spread(self):
        ex_a = make_exchange("ex_a")
        ex_b = make_exchange("ex_b")

        ex_a.inject_ticker(Ticker("BTCUSDT", 67000, 67000, 0))
        ex_b.inject_ticker(Ticker("BTCUSDT", 67200, 67200, 0))

        executor = CrossExchangeExecutor(
            exchanges={"ex_a": ex_a, "ex_b": ex_b},
            cx_config=CrossExchangeConfig(max_position_size_usd=500),
        )

        result = await executor.execute(make_opp())

        # Spread is ~0.3%, fees ~0.2%, net should be positive
        assert result.net_pnl > 0

    @pytest.mark.asyncio
    async def test_position_size_capped(self):
        ex_a = make_exchange("ex_a", usdt=100000)
        ex_b = make_exchange("ex_b", btc=10)

        ex_a.inject_ticker(Ticker("BTCUSDT", 67000, 67000, 0))
        ex_b.inject_ticker(Ticker("BTCUSDT", 67200, 67200, 0))

        executor = CrossExchangeExecutor(
            exchanges={"ex_a": ex_a, "ex_b": ex_b},
            cx_config=CrossExchangeConfig(max_position_size_usd=100),
        )

        result = await executor.execute(make_opp())
        assert result.buy_order is not None
        # Should trade ~$100 worth, not $100K
        trade_value = result.buy_order.quantity * result.buy_order.actual_price
        assert trade_value < 200  # Well under $200

    @pytest.mark.asyncio
    async def test_insufficient_balance_aborts(self):
        ex_a = make_exchange("ex_a", usdt=0.0)
        ex_b = make_exchange("ex_b", btc=0.0)

        ex_a.inject_ticker(Ticker("BTCUSDT", 67000, 67000, 0))
        ex_b.inject_ticker(Ticker("BTCUSDT", 67200, 67200, 0))

        executor = CrossExchangeExecutor(
            exchanges={"ex_a": ex_a, "ex_b": ex_b},
        )

        result = await executor.execute(make_opp())
        assert result.status == CrossTradeStatus.FAILED
        assert executor.total_aborts == 1

    @pytest.mark.asyncio
    async def test_stats_tracking(self):
        ex_a = make_exchange("ex_a")
        ex_b = make_exchange("ex_b")

        ex_a.inject_ticker(Ticker("BTCUSDT", 67000, 67000, 0))
        ex_b.inject_ticker(Ticker("BTCUSDT", 67200, 67200, 0))

        executor = CrossExchangeExecutor(
            exchanges={"ex_a": ex_a, "ex_b": ex_b},
            cx_config=CrossExchangeConfig(max_position_size_usd=500),
        )

        await executor.execute(make_opp())
        stats = executor.stats()
        assert stats["total_executions"] == 1
        assert stats["both_filled"] == 1


class TestCrossExchangeRiskManager:

    def test_approve_valid(self):
        rm = CrossExchangeRiskManager()
        opp = make_opp()
        approved, reason = rm.check(opp)
        assert approved

    def test_reject_killed(self):
        rm = CrossExchangeRiskManager()
        rm.kill("test")
        approved, _ = rm.check(make_opp())
        assert not approved

    def test_reject_daily_loss(self):
        rm = CrossExchangeRiskManager(
            trading_config=TradingConfig(daily_loss_limit_usd=10)
        )
        rm.daily_pnl = -15.0
        approved, _ = rm.check(make_opp())
        assert not approved
        assert rm.killed

    def test_reject_emergency_hedge_limit(self):
        rm = CrossExchangeRiskManager()
        rm.emergency_hedge_count = 3
        approved, _ = rm.check(make_opp())
        assert not approved
        assert rm.killed

    def test_reject_concurrent_limit(self):
        rm = CrossExchangeRiskManager(
            cx_config=CrossExchangeConfig(max_concurrent_arbs=1)
        )
        rm.active_arbs = 1
        approved, _ = rm.check(make_opp())
        assert not approved

    def test_reject_below_min_spread(self):
        rm = CrossExchangeRiskManager(
            cx_config=CrossExchangeConfig(min_net_spread=0.01)
        )
        opp = make_opp()
        opp.net_spread = 0.001  # Below threshold
        approved, _ = rm.check(opp)
        assert not approved

    def test_reject_unhealthy_exchange(self):
        rm = CrossExchangeRiskManager()
        rm.set_exchange_health("ex_a", False)
        approved, _ = rm.check(make_opp())
        assert not approved

    def test_record_profit_resets_losses(self):
        rm = CrossExchangeRiskManager()
        rm.consecutive_losses = 2
        rm.record_trade_result(1.0)
        assert rm.consecutive_losses == 0

    def test_record_loss_with_hedge(self):
        rm = CrossExchangeRiskManager()
        rm.record_trade_result(-0.5, had_emergency_hedge=True)
        assert rm.consecutive_losses == 1
        assert rm.emergency_hedge_count == 1

    def test_reset_daily(self):
        rm = CrossExchangeRiskManager()
        rm.daily_pnl = -50
        rm.killed = True
        rm.emergency_hedge_count = 5
        rm.reset_daily()
        assert rm.daily_pnl == 0
        assert not rm.killed
        assert rm.emergency_hedge_count == 0
