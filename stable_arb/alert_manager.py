"""Alert manager — terminal + sound alerts for depeg events."""

import logging
import subprocess
import sys
from time import time_ns

from stable_arb.models import DepegEvent, DepegSeverity

logger = logging.getLogger(__name__)


class AlertManager:
    """
    Manages alerts for depeg events with cooldown.

    Tiers:
    - MILD: terminal log + sound
    - MODERATE: prominent banner + sound
    - SEVERE/CRISIS: urgent banner + repeated sound
    """

    def __init__(self, cooldown_sec: float = 300):
        self.cooldown_sec = cooldown_sec
        self._last_alert_ms: dict[str, int] = {}  # stable -> last alert timestamp
        self.total_alerts: int = 0

    def should_alert(self, event: DepegEvent) -> bool:
        """Check if alert should fire (respecting cooldown)."""
        now_ms = time_ns() // 1_000_000
        last = self._last_alert_ms.get(event.stable, 0)
        if (now_ms - last) < self.cooldown_sec * 1000:
            return False
        return True

    def alert(self, event: DepegEvent) -> None:
        """Fire an alert for a depeg event."""
        if not self.should_alert(event):
            return

        self._last_alert_ms[event.stable] = time_ns() // 1_000_000
        self.total_alerts += 1

        severity = event.severity
        action = self._action_label(event)

        # Terminal alert
        print(f"\n{'='*60}")
        if severity in (DepegSeverity.SEVERE, DepegSeverity.CRISIS):
            print(f"  {'!'*20} DEPEG ALERT {'!'*20}")
        else:
            print(f"  🔔  DEPEG DETECTED")
        print(f"{'='*60}")
        print(f"  Stablecoin: {event.stable}")
        print(f"  Price:      ${event.median_price:.4f}")
        print(f"  Deviation:  {event.deviation:.4%}")
        print(f"  Severity:   {severity.value}")
        print(f"  Safety:     {event.safety_tier.value}")
        print(f"  Action:     {action}")
        print(f"  Sources:    {len(event.sources)}")
        for s in event.sources:
            print(f"    {s.source:<12} ${s.price:.4f}")
        print(f"{'='*60}\n")

        # Sound
        self._play_sound(severity)

        logger.warning(
            "DEPEG ALERT: %s $%.4f (%.4f%%, %s) — %s",
            event.stable, event.median_price, event.deviation * 100,
            severity.value, action,
        )

    def _action_label(self, event: DepegEvent) -> str:
        if event.severity == DepegSeverity.CRISIS:
            return "ALERT ONLY — possible collapse, DO NOT auto-buy"
        if event.is_auto_executable:
            return "AUTO-EXECUTE eligible (whitelisted)"
        if event.needs_human:
            return "Waiting for human approval"
        return "Monitor"

    def _play_sound(self, severity: DepegSeverity) -> None:
        try:
            if sys.platform == "darwin":
                sound = "/System/Library/Sounds/Glass.aiff"
                if severity in (DepegSeverity.SEVERE, DepegSeverity.CRISIS):
                    sound = "/System/Library/Sounds/Sosumi.aiff"
                subprocess.Popen(
                    ["afplay", sound],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            else:
                print("\a", end="", flush=True)
        except Exception:
            print("\a", end="", flush=True)
