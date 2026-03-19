"""Depeg detector — threshold + confirmation window detection."""

import logging
from statistics import median
from time import time_ns

from stable_arb.models import (
    STABLE_WHITELIST,
    DepegEvent,
    DepegSeverity,
    SafetyTier,
    StablePrice,
)

logger = logging.getLogger(__name__)


class DepegDetector:
    """
    Detects stablecoin depeg events using threshold + confirmation.

    Detection logic:
    1. Collect prices from multiple sources
    2. Compute median price
    3. Calculate deviation from $1.00
    4. Require 3 ticks below threshold within 60s to confirm
    5. Classify severity (NORMAL → CRISIS)
    """

    def __init__(
        self,
        alert_threshold: float = 0.003,  # 0.3%
        execute_threshold: float = 0.005,  # 0.5%
        severe_threshold: float = 0.02,  # 2.0%
        crisis_threshold: float = 0.05,  # 5.0%
        confirmation_ticks: int = 3,
        confirmation_window_ms: int = 60_000,  # 60 seconds
        min_sources: int = 1,
    ):
        self.alert_threshold = alert_threshold
        self.execute_threshold = execute_threshold
        self.severe_threshold = severe_threshold
        self.crisis_threshold = crisis_threshold
        self.confirmation_ticks = confirmation_ticks
        self.confirmation_window_ms = confirmation_window_ms
        self.min_sources = min_sources

        # Per-stable tracking
        self._prices: dict[str, dict[str, StablePrice]] = {}  # stable -> source -> price
        self._confirmations: dict[str, list[int]] = {}  # stable -> list of timestamps
        self._last_severity: dict[str, DepegSeverity] = {}

        # Stats
        self.total_updates: int = 0
        self.total_alerts: int = 0

    def update(self, price: StablePrice) -> DepegEvent | None:
        """
        Update a price and check for depeg.

        Returns DepegEvent if confirmed depeg detected, else None.
        """
        self.total_updates += 1

        stable = price.stable
        if stable not in self._prices:
            self._prices[stable] = {}
        self._prices[stable][price.source] = price

        # Need minimum sources
        sources = self._prices.get(stable, {})
        if len(sources) < self.min_sources:
            return None

        # Compute median price
        prices = [p.price for p in sources.values() if p.price > 0]
        if not prices:
            return None

        med_price = median(prices)
        deviation = abs(1.0 - med_price)

        # Classify severity
        severity = self._classify(deviation)

        # Below alert threshold — reset confirmations
        if severity == DepegSeverity.NORMAL or severity == DepegSeverity.WATCHING:
            self._confirmations[stable] = []
            self._last_severity[stable] = severity
            return None

        # Above threshold — add confirmation tick
        now_ms = time_ns() // 1_000_000
        if stable not in self._confirmations:
            self._confirmations[stable] = []

        self._confirmations[stable].append(now_ms)

        # Clean old ticks outside window
        cutoff = now_ms - self.confirmation_window_ms
        self._confirmations[stable] = [
            t for t in self._confirmations[stable] if t >= cutoff
        ]

        # Not enough confirmations yet
        if len(self._confirmations[stable]) < self.confirmation_ticks:
            if self._last_severity.get(stable) != severity:
                logger.info(
                    "Depeg signal: %s %.4f%% (%s) — confirming (%d/%d)",
                    stable, deviation * 100, severity.value,
                    len(self._confirmations[stable]), self.confirmation_ticks,
                )
            self._last_severity[stable] = severity
            return None

        # CONFIRMED depeg
        self.total_alerts += 1
        safety_tier = STABLE_WHITELIST.get(stable, SafetyTier.ALERT_ONLY)

        event = DepegEvent(
            stable=stable,
            severity=severity,
            safety_tier=safety_tier,
            deviation=deviation,
            median_price=med_price,
            sources=list(sources.values()),
            confirmation_count=len(self._confirmations[stable]),
            first_detected_ms=self._confirmations[stable][0],
        )

        logger.warning(
            "DEPEG CONFIRMED: %s at $%.4f (%.4f%% deviation, %s, %s)",
            stable, med_price, deviation * 100,
            severity.value, safety_tier.value,
        )

        # Reset confirmations after alert (cooldown)
        self._confirmations[stable] = []

        return event

    def _classify(self, deviation: float) -> DepegSeverity:
        if deviation >= self.crisis_threshold:
            return DepegSeverity.CRISIS
        elif deviation >= self.severe_threshold:
            return DepegSeverity.SEVERE
        elif deviation >= self.execute_threshold:
            return DepegSeverity.MODERATE
        elif deviation >= self.alert_threshold:
            return DepegSeverity.MILD
        elif deviation >= 0.001:
            return DepegSeverity.WATCHING
        return DepegSeverity.NORMAL

    def get_status(self) -> dict[str, dict]:
        """Current status of all monitored stables."""
        status = {}
        for stable, sources in self._prices.items():
            prices = [p.price for p in sources.values() if p.price > 0]
            if prices:
                med = median(prices)
                dev = abs(1.0 - med)
                status[stable] = {
                    "price": round(med, 6),
                    "deviation": round(dev * 100, 4),
                    "severity": self._classify(dev).value,
                    "sources": len(sources),
                }
        return status

    def stats(self) -> dict:
        return {
            "total_updates": self.total_updates,
            "total_alerts": self.total_alerts,
            "monitored_stables": list(self._prices.keys()),
        }
