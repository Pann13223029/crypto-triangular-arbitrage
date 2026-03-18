"""Tests for MultiExchangeSimulator."""

import pytest

from config.settings import MultiSimConfig
from core.models import Ticker, TradingPair
from exchange.multi_sim import MultiExchangeSimulator


@pytest.fixture
def multi_sim():
    config = MultiSimConfig(
        exchange_ids=["sim_binance", "sim_bybit"],
        initial_balances_per_exchange={"USDT": 10000.0, "BTC": 0.1},
        ou_theta=0.1,
        ou_sigma=0.002,
    )
    sim = MultiExchangeSimulator(config)
    sim.load_pairs([
        TradingPair(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT"),
        TradingPair(symbol="ETHUSDT", base_asset="ETH", quote_asset="USDT"),
    ])
    return sim


class TestMultiExchangeSimulator:

    def test_creates_exchanges(self, multi_sim):
        assert len(multi_sim.exchanges) == 2
        assert "sim_binance" in multi_sim.exchanges
        assert "sim_bybit" in multi_sim.exchanges

    def test_exchange_ids(self, multi_sim):
        for ex_id, ex in multi_sim.exchanges.items():
            assert ex.exchange_id == ex_id

    def test_inject_base_ticker(self, multi_sim):
        ticker = Ticker("BTCUSDT", 67000.0, 67010.0, 1000)
        result = multi_sim.inject_base_ticker(ticker)

        assert len(result) == 2
        # Base exchange gets exact price
        base = result["sim_binance"]
        assert base.bid == 67000.0
        assert base.ask == 67010.0

        # Other exchange gets divergent price
        other = result["sim_bybit"]
        assert other.symbol == "BTCUSDT"
        assert other.bid != 0
        assert other.ask != 0

    def test_prices_diverge(self, multi_sim):
        """After many ticks, prices should differ between exchanges."""
        divergences = []

        for _ in range(100):
            ticker = Ticker("BTCUSDT", 67000.0, 67010.0, 1000)
            result = multi_sim.inject_base_ticker(ticker)

            base_mid = (result["sim_binance"].bid + result["sim_binance"].ask) / 2
            other_mid = (result["sim_bybit"].bid + result["sim_bybit"].ask) / 2
            divergences.append(abs(other_mid - base_mid) / base_mid)

        # Should see some non-zero divergence
        avg_divergence = sum(divergences) / len(divergences)
        assert avg_divergence > 0

    def test_ou_mean_reverts(self, multi_sim):
        """O-U process should mean-revert — offsets shouldn't drift to infinity."""
        offsets = []

        for _ in range(1000):
            ticker = Ticker("BTCUSDT", 67000.0, 67010.0, 1000)
            result = multi_sim.inject_base_ticker(ticker)

            base = result["sim_binance"].bid
            other = result["sim_bybit"].bid
            offsets.append((other - base) / base)

        # Mean should be near 0 (O-U reverts to mu=0)
        mean_offset = sum(offsets) / len(offsets)
        assert abs(mean_offset) < 0.01  # Within 1%

        # Max offset should be bounded (not drifting)
        max_offset = max(abs(o) for o in offsets)
        assert max_offset < 0.05  # Within 5%

    def test_independent_balances(self, multi_sim):
        """Each exchange should have independent balances."""
        ex1 = multi_sim.get_exchange("sim_binance")
        ex2 = multi_sim.get_exchange("sim_bybit")

        assert ex1.balances["USDT"] == 10000.0
        assert ex2.balances["USDT"] == 10000.0

        # Modify one — shouldn't affect the other
        ex1.balances["USDT"] = 5000.0
        assert ex2.balances["USDT"] == 10000.0

    def test_load_pairs(self, multi_sim):
        """Pairs should be loaded on all exchanges."""
        for ex in multi_sim.exchanges.values():
            assert "BTCUSDT" in ex.pairs
            assert "ETHUSDT" in ex.pairs

    def test_fee_schedules(self, multi_sim):
        schedules = multi_sim.get_fee_schedules()
        assert len(schedules) == 2
        assert "sim_binance" in schedules
        assert "sim_bybit" in schedules
        assert schedules["sim_binance"].exchange_id == "sim_binance"

    def test_stats(self, multi_sim):
        stats = multi_sim.stats()
        assert stats["exchanges"] == 2
        assert "sim_binance" in stats["exchange_ids"]
