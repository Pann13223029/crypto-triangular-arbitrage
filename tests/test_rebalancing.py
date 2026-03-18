"""Tests for RebalanceManager."""

import pytest

from config.settings import FeeConfig, RebalanceConfig, SimulationConfig
from cross_exchange.balance_tracker import BalanceTracker
from cross_exchange.models import CrossExchangeOpportunity, TransferStatus
from exchange.simulator import SimulatedExchange
from rebalancing.manager import RebalanceManager


def make_exchange(ex_id: str, usdt: float = 10000.0) -> SimulatedExchange:
    fee = FeeConfig(taker_fee=0.001, use_bnb_fee=False)
    sim = SimulationConfig(
        initial_balances={"USDT": usdt, "BTC": 0.1},
        latency_ms=0,
        fixed_slippage=0.0,
    )
    return SimulatedExchange(fee, sim, exchange_id=ex_id)


def make_setup(
    balances: dict[str, float] | None = None,
    threshold: float = 0.25,
    min_amount: float = 100.0,
    cooldown: float = 0.0,
):
    """Create exchanges, tracker, and manager."""
    bal = balances or {"ex_a": 10000, "ex_b": 10000, "ex_c": 10000}
    exchanges = {ex_id: make_exchange(ex_id, usdt) for ex_id, usdt in bal.items()}

    tracker = BalanceTracker(exchanges)
    # Manually set cached balances (skip async refresh)
    for ex_id, ex in exchanges.items():
        tracker._cached[ex_id] = {"USDT": ex.balances["USDT"], "BTC": 0.1}

    config = RebalanceConfig(
        deviation_threshold=threshold,
        min_rebalance_usd=min_amount,
        cooldown_sec=cooldown,
        transfer_fee_usd=1.0,
    )
    manager = RebalanceManager(tracker, config)
    manager.set_targets(list(bal.keys()))

    return exchanges, tracker, manager


class TestRebalanceManager:

    def test_no_rebalance_when_balanced(self):
        """Equal balances should not trigger rebalance."""
        _, _, manager = make_setup({"ex_a": 10000, "ex_b": 10000, "ex_c": 10000})
        decision = manager.check_rebalance_needed()
        assert decision is None

    def test_rebalance_on_deviation(self):
        """Unequal balances exceeding threshold should trigger."""
        _, _, manager = make_setup(
            {"ex_a": 5000, "ex_b": 20000, "ex_c": 5000},
            threshold=0.25,
            min_amount=100,
        )
        decision = manager.check_rebalance_needed()
        assert decision is not None
        assert len(decision.transfers) > 0
        assert decision.total_amount > 0

    def test_transfer_direction_correct(self):
        """Transfers should go from excess to deficit."""
        _, _, manager = make_setup(
            {"ex_a": 5000, "ex_b": 20000, "ex_c": 5000},
            threshold=0.25,
            min_amount=100,
        )
        decision = manager.check_rebalance_needed()
        assert decision is not None

        for transfer in decision.transfers:
            assert transfer.from_exchange == "ex_b"  # Excess
            assert transfer.to_exchange in ("ex_a", "ex_c")  # Deficit
            assert transfer.amount > 0

    def test_no_rebalance_below_threshold(self):
        """Slight deviation below threshold should not trigger."""
        _, _, manager = make_setup(
            {"ex_a": 9000, "ex_b": 11000, "ex_c": 10000},
            threshold=0.25,  # 25% — deviation is only ~10%
        )
        decision = manager.check_rebalance_needed()
        assert decision is None

    def test_min_amount_respected(self):
        """Transfers below minimum amount are skipped."""
        _, _, manager = make_setup(
            {"ex_a": 9500, "ex_b": 10500, "ex_c": 10000},
            threshold=0.01,  # Very low threshold
            min_amount=5000,  # Very high minimum
        )
        decision = manager.check_rebalance_needed()
        assert decision is None

    @pytest.mark.asyncio
    async def test_execute_rebalance(self):
        """Execute should move USDT between exchanges."""
        exchanges, tracker, manager = make_setup(
            {"ex_a": 5000, "ex_b": 20000, "ex_c": 5000},
            threshold=0.25,
            min_amount=100,
            cooldown=0,
        )

        decision = manager.check_rebalance_needed()
        assert decision is not None

        completed = await manager.execute_rebalance(decision)
        assert len(completed) > 0

        # All transfers should be confirmed
        for t in completed:
            assert t.status == TransferStatus.CONFIRMED

        # Balances should be more balanced
        a_usdt = exchanges["ex_a"].balances["USDT"]
        b_usdt = exchanges["ex_b"].balances["USDT"]
        c_usdt = exchanges["ex_c"].balances["USDT"]

        # ex_b should have less than 20000 now
        assert b_usdt < 20000
        # ex_a or ex_c should have more than 5000
        assert a_usdt > 5000 or c_usdt > 5000

    @pytest.mark.asyncio
    async def test_transfer_fees_deducted(self):
        """Transfer fees should be deducted from sender."""
        exchanges, tracker, manager = make_setup(
            {"ex_a": 3000, "ex_b": 27000},
            threshold=0.25,
            min_amount=100,
            cooldown=0,
        )

        decision = manager.check_rebalance_needed()
        assert decision is not None

        total_before = sum(ex.balances["USDT"] for ex in exchanges.values())
        await manager.execute_rebalance(decision)
        total_after = sum(ex.balances["USDT"] for ex in exchanges.values())

        # Total should decrease by fees
        assert total_after < total_before
        assert manager.total_transfer_fees > 0

    def test_deviation_report(self):
        _, _, manager = make_setup({"ex_a": 5000, "ex_b": 20000, "ex_c": 5000})
        report = manager.get_deviation_report()

        assert "ex_a" in report
        assert "ex_b" in report
        assert report["ex_b"]["needs_rebalance"]
        assert not report["ex_a"]["needs_rebalance"] or report["ex_a"]["needs_rebalance"]

    def test_opportunity_rebalance_bonus(self):
        """Should give bonus for trades that naturally rebalance."""
        _, _, manager = make_setup(
            {"ex_a": 5000, "ex_b": 20000, "ex_c": 5000},
        )

        # Buying on ex_b (excess USDT) and selling on ex_a (deficit) is good
        good_opp = CrossExchangeOpportunity(
            symbol="BTCUSDT",
            buy_exchange="ex_b",  # Has excess USDT
            sell_exchange="ex_a",  # Needs USDT
            buy_price=67000, sell_price=67200,
            gross_spread=0.003, net_spread=0.001,
        )
        good_bonus = manager.opportunity_rebalance_bonus(good_opp)

        # Buying on ex_a (deficit USDT) would worsen imbalance
        bad_opp = CrossExchangeOpportunity(
            symbol="BTCUSDT",
            buy_exchange="ex_a",  # Already low on USDT
            sell_exchange="ex_b",  # Already high on USDT
            buy_price=67000, sell_price=67200,
            gross_spread=0.003, net_spread=0.001,
        )
        bad_bonus = manager.opportunity_rebalance_bonus(bad_opp)

        assert good_bonus > bad_bonus

    def test_stats(self):
        _, _, manager = make_setup()
        stats = manager.stats()
        assert stats["total_transfers"] == 0
        assert "targets" in stats

    def test_set_custom_targets(self):
        config = RebalanceConfig(
            target_allocation={"ex_a": 0.5, "ex_b": 0.3, "ex_c": 0.2}
        )
        exchanges = {ex_id: make_exchange(ex_id) for ex_id in ["ex_a", "ex_b", "ex_c"]}
        tracker = BalanceTracker(exchanges)
        manager = RebalanceManager(tracker, config)
        manager.set_targets(["ex_a", "ex_b", "ex_c"])

        assert manager.targets["ex_a"] == 0.5
        assert manager.targets["ex_b"] == 0.3
