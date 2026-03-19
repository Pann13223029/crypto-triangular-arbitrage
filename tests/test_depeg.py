"""Tests for stablecoin depeg detector."""

import pytest
from time import time_ns

from stable_arb.models import (
    STABLE_WHITELIST,
    DepegSeverity,
    SafetyTier,
    StablePrice,
)
from stable_arb.detector import DepegDetector
from stable_arb.price_aggregator import StablePriceAggregator


def now_ms():
    return time_ns() // 1_000_000


class TestDepegDetector:

    def test_normal_price_no_alert(self):
        d = DepegDetector()
        event = d.update(StablePrice("USDT", "kucoin", 0.9998))
        assert event is None

    def test_mild_depeg_needs_confirmation(self):
        d = DepegDetector(confirmation_ticks=3)
        # First tick — not confirmed yet
        event = d.update(StablePrice("USDT", "kucoin", 0.996, now_ms()))
        assert event is None

    def test_confirmed_mild_depeg(self):
        d = DepegDetector(confirmation_ticks=3, confirmation_window_ms=5000)
        ts = now_ms()
        d.update(StablePrice("USDT", "kucoin", 0.996, ts))
        d.update(StablePrice("USDT", "kucoin", 0.995, ts + 100))
        event = d.update(StablePrice("USDT", "kucoin", 0.996, ts + 200))
        assert event is not None
        assert event.severity == DepegSeverity.MILD
        assert event.stable == "USDT"

    def test_moderate_depeg(self):
        d = DepegDetector(confirmation_ticks=1)
        event = d.update(StablePrice("USDC", "kucoin", 0.992))
        assert event is not None
        assert event.severity == DepegSeverity.MODERATE
        assert event.is_auto_executable  # USDC is whitelisted

    def test_severe_depeg(self):
        d = DepegDetector(confirmation_ticks=1)
        event = d.update(StablePrice("USDT", "kucoin", 0.970))
        assert event is not None
        assert event.severity == DepegSeverity.SEVERE

    def test_crisis_depeg(self):
        d = DepegDetector(confirmation_ticks=1)
        event = d.update(StablePrice("USDT", "kucoin", 0.940))
        assert event is not None
        assert event.severity == DepegSeverity.CRISIS

    def test_dai_needs_human_approval(self):
        d = DepegDetector(confirmation_ticks=1)
        event = d.update(StablePrice("DAI", "kucoin", 0.990))
        assert event is not None
        assert event.safety_tier == SafetyTier.HUMAN_APPROVE
        assert event.needs_human

    def test_unknown_stable_alert_only(self):
        d = DepegDetector(confirmation_ticks=1)
        event = d.update(StablePrice("TUSD", "kucoin", 0.990))
        assert event is not None
        assert event.safety_tier == SafetyTier.ALERT_ONLY

    def test_recovery_resets_confirmation(self):
        d = DepegDetector(confirmation_ticks=3, confirmation_window_ms=5000)
        ts = now_ms()
        d.update(StablePrice("USDT", "kucoin", 0.996, ts))
        d.update(StablePrice("USDT", "kucoin", 0.996, ts + 100))
        # Price recovers
        d.update(StablePrice("USDT", "kucoin", 0.9999, ts + 200))
        # Depeg again — should need fresh 3 confirmations
        event = d.update(StablePrice("USDT", "kucoin", 0.996, ts + 300))
        assert event is None  # Only 1 confirmation

    def test_multiple_sources_median(self):
        d = DepegDetector(confirmation_ticks=1, min_sources=2)
        d.update(StablePrice("USDT", "kucoin", 0.995))
        # Only 1 source — no alert
        event = d.update(StablePrice("USDT", "kucoin", 0.995))
        assert event is None

        # Add second source
        event = d.update(StablePrice("USDT", "binance", 0.994))
        # Now median of 0.995 and 0.994 = 0.9945 → 0.55% depeg
        assert event is not None

    def test_get_status(self):
        d = DepegDetector()
        d.update(StablePrice("USDT", "kucoin", 0.9995))
        d.update(StablePrice("USDC", "kucoin", 1.0002))

        status = d.get_status()
        assert "USDT" in status
        assert "USDC" in status
        assert status["USDT"]["severity"] == "NORMAL"


class TestPriceAggregator:

    def test_usdc_usdt_pair(self):
        prices = []
        agg = StablePriceAggregator(on_price=lambda p: prices.append(p))

        from core.models import Ticker
        agg.handle_ticker("kucoin", Ticker("USDCUSDT", 1.0005, 1.0006, 0))

        # Should emit USDC and USDT prices
        assert len(prices) == 2
        usdc = [p for p in prices if p.stable == "USDC"][0]
        usdt = [p for p in prices if p.stable == "USDT"][0]
        assert usdc.price > 1.0
        assert usdt.price < 1.0

    def test_dai_pair(self):
        prices = []
        agg = StablePriceAggregator(on_price=lambda p: prices.append(p))

        from core.models import Ticker
        agg.handle_ticker("binance", Ticker("DAIUSDT", 0.9998, 0.9999, 0))

        assert len(prices) == 1
        assert prices[0].stable == "DAI"

    def test_ws_symbols(self):
        symbols = StablePriceAggregator.get_ws_symbols()
        assert "USDCUSDT" in symbols
        assert "DAIUSDT" in symbols


class TestWhitelist:

    def test_usdt_auto_execute(self):
        assert STABLE_WHITELIST["USDT"] == SafetyTier.AUTO_EXECUTE

    def test_usdc_auto_execute(self):
        assert STABLE_WHITELIST["USDC"] == SafetyTier.AUTO_EXECUTE

    def test_dai_human_approve(self):
        assert STABLE_WHITELIST["DAI"] == SafetyTier.HUMAN_APPROVE

    def test_tusd_alert_only(self):
        assert STABLE_WHITELIST["TUSD"] == SafetyTier.ALERT_ONLY
