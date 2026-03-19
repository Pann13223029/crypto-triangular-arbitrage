"""Tests for funding rate arb models and position manager."""

import pytest
from time import time_ns

from funding_arb.models import (
    FundingDirection,
    FundingOpportunity,
    FundingPosition,
    PositionStatus,
)
from funding_arb.position_manager import FundingPositionManager


def make_opp(symbol="VANRYUSDTM", rate=0.003, direction=FundingDirection.LONGS_PAY):
    return FundingOpportunity(
        symbol=symbol,
        base_asset=symbol.replace("USDTM", ""),
        funding_rate=rate,
        predicted_rate=rate * 0.8,
        direction=direction,
        daily_rate=rate * 3,
        annualized=rate * 3 * 365,
    )


class TestFundingModels:

    def test_opportunity_abs_rate(self):
        opp = make_opp(rate=-0.005)
        assert opp.abs_rate == 0.005

    def test_opportunity_is_longs_pay(self):
        opp_pos = make_opp(rate=0.003)
        opp_neg = make_opp(rate=-0.003)
        assert opp_pos.is_longs_pay
        assert not opp_neg.is_longs_pay

    def test_position_net_pnl(self):
        pos = FundingPosition(
            symbol="VANRYUSDTM",
            base_asset="VANRY",
            spot_symbol="VANRY-USDT",
            direction=FundingDirection.LONGS_PAY,
        )
        pos.funding_collected = 0.50
        pos.total_fees = 0.12
        assert pos.net_pnl == pytest.approx(0.38)

    def test_position_holding_hours(self):
        pos = FundingPosition(
            symbol="VANRYUSDTM",
            base_asset="VANRY",
            spot_symbol="VANRY-USDT",
            direction=FundingDirection.LONGS_PAY,
            entry_time_ms=time_ns() // 1_000_000 - 3_600_000,  # 1 hour ago
        )
        assert abs(pos.holding_hours - 1.0) < 0.1


class TestPositionManager:

    def test_should_enter_approved(self):
        pm = FundingPositionManager(kucoin_exchange=None)
        opp = make_opp(rate=0.002)
        approved, _ = pm.should_enter(opp)
        assert approved

    def test_should_enter_rejected_low_rate(self):
        pm = FundingPositionManager(kucoin_exchange=None, min_funding_rate=0.005)
        opp = make_opp(rate=0.001)
        approved, reason = pm.should_enter(opp)
        assert not approved
        assert "below threshold" in reason

    def test_should_enter_rejected_shorts_pay(self):
        pm = FundingPositionManager(kucoin_exchange=None)
        opp = make_opp(rate=-0.005, direction=FundingDirection.SHORTS_PAY)
        approved, reason = pm.should_enter(opp)
        assert not approved
        assert "SHORTS_PAY" in reason

    def test_should_enter_rejected_active_position(self):
        pm = FundingPositionManager(kucoin_exchange=None)
        pm.active_position = FundingPosition(
            symbol="X", base_asset="X", spot_symbol="X",
            direction=FundingDirection.LONGS_PAY,
        )
        opp = make_opp()
        approved, _ = pm.should_enter(opp)
        assert not approved

    def test_should_exit_funding_normalized(self):
        pm = FundingPositionManager(kucoin_exchange=None, exit_funding_rate=0.0005)
        pm.active_position = FundingPosition(
            symbol="VANRYUSDTM", base_asset="VANRY", spot_symbol="VANRY-USDT",
            direction=FundingDirection.LONGS_PAY,
        )
        should_exit, reason = pm.should_exit(0.0001)
        assert should_exit
        assert "normalized" in reason

    def test_should_exit_max_hold(self):
        pm = FundingPositionManager(kucoin_exchange=None, max_holding_days=7)
        pm.active_position = FundingPosition(
            symbol="VANRYUSDTM", base_asset="VANRY", spot_symbol="VANRY-USDT",
            direction=FundingDirection.LONGS_PAY,
            entry_time_ms=time_ns() // 1_000_000 - 8 * 24 * 3_600_000,  # 8 days ago
        )
        should_exit, reason = pm.should_exit(0.002)
        assert should_exit
        assert "holding period" in reason

    def test_should_exit_basis_divergence(self):
        pm = FundingPositionManager(kucoin_exchange=None, basis_stop_loss=0.015)
        pm.active_position = FundingPosition(
            symbol="VANRYUSDTM", base_asset="VANRY", spot_symbol="VANRY-USDT",
            direction=FundingDirection.LONGS_PAY,
            entry_time_ms=time_ns() // 1_000_000,
        )
        pm.active_position.current_basis = 0.02  # 2% divergence
        should_exit, reason = pm.should_exit(0.002)
        assert should_exit
        assert "Basis" in reason

    def test_record_funding(self):
        pm = FundingPositionManager(kucoin_exchange=None)
        pm.active_position = FundingPosition(
            symbol="VANRYUSDTM", base_asset="VANRY", spot_symbol="VANRY-USDT",
            direction=FundingDirection.LONGS_PAY,
        )
        pm.record_funding_payment(0.05)
        pm.record_funding_payment(0.04)
        assert pm.active_position.funding_collected == pytest.approx(0.09)
        assert pm.active_position.funding_periods == 2

    def test_create_and_close_position(self):
        pm = FundingPositionManager(kucoin_exchange=None, total_capital=30)
        opp = make_opp()

        pos = pm.create_position(opp)
        assert pos.status == PositionStatus.ENTERING
        assert pos.position_usd == 12.0  # 30 * 0.8 / 2
        assert pm.active_position is not None

        pm.close_position()
        assert pm.active_position.status == PositionStatus.EXITING

        pm.finalize_close()
        assert pm.active_position is None
        assert len(pm.closed_positions) == 1

    def test_stats(self):
        pm = FundingPositionManager(kucoin_exchange=None)
        stats = pm.stats()
        assert stats["active_position"] is None
        assert stats["total_entries"] == 0
