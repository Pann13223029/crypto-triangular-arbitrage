"""Tests for PairManager — adaptive pair selection."""

import pytest
from time import time_ns

from config.settings import FeeSchedule
from cross_exchange.pair_manager import PairCandidate, PairManager, PairStatus


def make_candidate(symbol: str, net_spread: float = 0.02, route: str = "kucoin→binance"):
    buy, sell = route.split("→")
    return PairCandidate(
        symbol=symbol,
        best_route=route,
        buy_exchange=buy,
        sell_exchange=sell,
        gross_spread=net_spread + 0.00175,
        net_spread=net_spread,
        price=1.0,
    )


def make_manager(**kwargs):
    fees = {
        "binance": FeeSchedule("binance", taker_fee=0.00075),
        "kucoin": FeeSchedule("kucoin", taker_fee=0.001),
    }
    return PairManager(fee_schedules=fees, **kwargs)


class TestPairManager:

    def test_update_candidates_ranks_by_spread(self):
        pm = make_manager()
        candidates = [
            make_candidate("AAUSDT", 0.01),
            make_candidate("BBUSDT", 0.03),
            make_candidate("CCUSDT", 0.02),
        ]
        pm.update_candidates(candidates)

        assert len(pm.on_deck) == 3
        assert pm.on_deck[0].symbol == "BBUSDT"  # Highest spread first
        assert pm.on_deck[1].symbol == "CCUSDT"

    def test_on_deck_limited_to_count(self):
        pm = make_manager(on_deck_count=2)
        candidates = [make_candidate(f"T{i}USDT", 0.01 * i) for i in range(1, 6)]
        pm.update_candidates(candidates)

        assert len(pm.on_deck) == 2

    def test_set_active(self):
        pm = make_manager()
        candidates = [make_candidate("BARDUSDT", 0.04)]
        pm.update_candidates(candidates)

        assert pm.set_active("BARDUSDT")
        assert pm.active_pair is not None
        assert pm.active_pair.symbol == "BARDUSDT"
        assert pm.active_pair.status == PairStatus.ACTIVE
        assert not pm.paused

    def test_set_active_unknown_symbol(self):
        pm = make_manager()
        pm.update_candidates([])
        assert not pm.set_active("FAKEUSDT")

    def test_active_excluded_from_on_deck(self):
        pm = make_manager()
        candidates = [
            make_candidate("BARDUSDT", 0.04),
            make_candidate("ATAUSDT", 0.02),
        ]
        pm.update_candidates(candidates)
        pm.set_active("BARDUSDT")
        pm.update_candidates(candidates)  # Re-scan

        on_deck_symbols = [c.symbol for c in pm.on_deck]
        assert "BARDUSDT" not in on_deck_symbols
        assert "ATAUSDT" in on_deck_symbols

    def test_demotion_by_consecutive_losses(self):
        pm = make_manager(demotion_max_losses=3)
        pm.update_candidates([make_candidate("BARDUSDT", 0.04)])
        pm.set_active("BARDUSDT")

        pm.record_trade_result(False)
        pm.record_trade_result(False)
        assert not pm.check_demotion(0.04)  # Only 2 losses

        pm.record_trade_result(False)
        assert pm.check_demotion(0.04)  # 3 losses → demotion
        assert pm.paused

    def test_demotion_resets_on_win(self):
        pm = make_manager(demotion_max_losses=3)
        pm.update_candidates([make_candidate("BARDUSDT", 0.04)])
        pm.set_active("BARDUSDT")

        pm.record_trade_result(False)
        pm.record_trade_result(False)
        pm.record_trade_result(True)  # Reset
        pm.record_trade_result(False)

        assert not pm.check_demotion(0.04)  # Only 1 loss after reset

    def test_demotion_by_low_spread(self):
        pm = make_manager(
            demotion_spread_threshold=0.003,
            demotion_time_sec=0.001,  # Instant for test
        )
        candidates = [
            make_candidate("BARDUSDT", 0.04),
            make_candidate("ATAUSDT", 0.02),
        ]
        pm.update_candidates(candidates)
        pm.set_active("BARDUSDT")
        pm.update_candidates(candidates)  # Refresh on-deck after set_active

        # First check sets the timer
        pm.check_demotion(0.001)
        # Second check triggers (time elapsed > 0.001s)
        import time
        time.sleep(0.01)
        assert pm.check_demotion(0.001)
        assert pm.paused
        assert pm.pending_promotion is not None
        assert pm.pending_promotion.symbol == "ATAUSDT"

    def test_spread_recovery_resets_timer(self):
        pm = make_manager(demotion_spread_threshold=0.003, demotion_time_sec=600)
        pm.update_candidates([make_candidate("BARDUSDT", 0.04)])
        pm.set_active("BARDUSDT")

        pm.check_demotion(0.001)  # Start timer
        assert pm.active_pair.low_spread_since_ms > 0

        pm.check_demotion(0.005)  # Spread recovered
        assert pm.active_pair.low_spread_since_ms == 0  # Timer reset

    def test_approve_promotion(self):
        pm = make_manager(demotion_max_losses=1, demotion_time_sec=0.001)
        candidates = [
            make_candidate("BARDUSDT", 0.04),
            make_candidate("ATAUSDT", 0.02),
        ]
        pm.update_candidates(candidates)
        pm.set_active("BARDUSDT")
        pm.update_candidates(candidates)  # Refresh on-deck

        pm.record_trade_result(False)
        pm.check_demotion(0.04)  # Triggers demotion

        new = pm.approve_promotion()
        assert new is not None
        assert new.symbol == "ATAUSDT"
        assert pm.active_pair.symbol == "ATAUSDT"
        assert pm.paused  # Still paused until resume

    def test_resume(self):
        pm = make_manager()
        pm.update_candidates([make_candidate("BARDUSDT", 0.04)])
        pm.set_active("BARDUSDT")
        pm.paused = True

        assert pm.resume()
        assert not pm.paused

    def test_decline_promotion(self):
        pm = make_manager(demotion_max_losses=1)
        candidates = [
            make_candidate("BARDUSDT", 0.04),
            make_candidate("ATAUSDT", 0.02),
        ]
        pm.update_candidates(candidates)
        pm.set_active("BARDUSDT")
        pm.record_trade_result(False)
        pm.check_demotion(0.04)

        pm.decline_promotion()
        assert pm.pending_promotion is None
        assert pm.paused  # Still paused

    def test_needs_scan(self):
        pm = make_manager(scan_interval_sec=0.01)
        assert pm.needs_scan()  # Never scanned

        pm.last_scan_ms = time_ns() // 1_000_000
        assert not pm.needs_scan()  # Just scanned

        import time
        time.sleep(0.02)
        assert pm.needs_scan()  # Interval passed

    def test_get_active_symbols(self):
        pm = make_manager()
        candidates = [
            make_candidate("BARDUSDT", 0.04),
            make_candidate("ATAUSDT", 0.02),
            make_candidate("CFGUSDT", 0.01),
        ]
        pm.update_candidates(candidates)
        pm.set_active("BARDUSDT")

        symbols = pm.get_active_symbols()
        assert "BARDUSDT" in symbols
        assert "ATAUSDT" in symbols
        assert "CFGUSDT" in symbols

    def test_stats(self):
        pm = make_manager()
        pm.update_candidates([make_candidate("BARDUSDT", 0.04)])
        pm.set_active("BARDUSDT")

        stats = pm.stats()
        assert "BARDUSDT" in stats["active"]
        assert stats["paused"] is False
        assert stats["total_scans"] == 1
