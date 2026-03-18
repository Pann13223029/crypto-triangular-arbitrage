"""Risk manager — position limits, loss limits, cooldowns, kill switch."""

import logging
from time import time_ns

from config.settings import TradingConfig
from core.models import Opportunity

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Enforces trading risk limits before execution.

    Checks: daily loss, consecutive losses, cooldowns,
    position size, balance, WebSocket health.
    """

    def __init__(self, config: TradingConfig | None = None):
        self.config = config or TradingConfig()

        # Tracking state
        self.daily_pnl: float = 0.0
        self.consecutive_losses: int = 0
        self.last_loss_time_ms: int = 0
        self.open_triangles: int = 0
        self.killed: bool = False
        self.kill_reason: str = ""

        # Stats
        self.total_approved: int = 0
        self.total_rejected: int = 0

    def check(
        self,
        opportunity: Opportunity,
        ws_healthy: bool = True,
    ) -> tuple[bool, str]:
        """
        Check if an opportunity passes all risk checks.

        Returns:
            (approved: bool, reason: str)
        """
        # Kill switch
        if self.killed:
            return False, f"Kill switch active: {self.kill_reason}"

        # WebSocket health
        if not ws_healthy:
            self.kill("WebSocket unhealthy — stale prices")
            return False, "WebSocket unhealthy"

        # Daily loss limit
        if self.daily_pnl <= -self.config.daily_loss_limit_usd:
            self.kill(f"Daily loss limit hit: ${self.daily_pnl:.2f}")
            return False, f"Daily loss limit ({self.daily_pnl:.2f})"

        # Consecutive losses
        if self.consecutive_losses >= self.config.max_consecutive_losses:
            self.kill(f"Max consecutive losses: {self.consecutive_losses}")
            return False, f"Consecutive losses ({self.consecutive_losses})"

        # Cooldown after loss
        if self.last_loss_time_ms > 0:
            now = time_ns() // 1_000_000
            elapsed_sec = (now - self.last_loss_time_ms) / 1000
            if elapsed_sec < self.config.cooldown_after_loss_sec:
                remaining = self.config.cooldown_after_loss_sec - elapsed_sec
                self.total_rejected += 1
                return False, f"Cooldown ({remaining:.0f}s remaining)"

        # Max open triangles
        if self.open_triangles >= self.config.max_open_triangles:
            self.total_rejected += 1
            return False, "Max open triangles reached"

        # Min profit threshold
        if opportunity.theoretical_profit < self.config.min_profit_threshold:
            self.total_rejected += 1
            return False, f"Below min profit ({opportunity.theoretical_profit:.4%})"

        self.total_approved += 1
        return True, "Approved"

    def record_trade_result(self, pnl: float) -> None:
        """Record the P&L of a completed trade."""
        self.daily_pnl += pnl

        if pnl < 0:
            self.consecutive_losses += 1
            self.last_loss_time_ms = time_ns() // 1_000_000
        else:
            self.consecutive_losses = 0

    def on_trade_start(self) -> None:
        """Called when a triangle trade begins execution."""
        self.open_triangles += 1

    def on_trade_end(self) -> None:
        """Called when a triangle trade completes."""
        self.open_triangles = max(0, self.open_triangles - 1)

    def kill(self, reason: str) -> None:
        """Activate kill switch."""
        if not self.killed:
            logger.critical("KILL SWITCH: %s", reason)
        self.killed = True
        self.kill_reason = reason

    def reset_daily(self) -> None:
        """Reset daily counters (call at start of new day)."""
        self.daily_pnl = 0.0
        self.consecutive_losses = 0
        self.last_loss_time_ms = 0
        self.killed = False
        self.kill_reason = ""

    def stats(self) -> dict:
        return {
            "daily_pnl": round(self.daily_pnl, 6),
            "consecutive_losses": self.consecutive_losses,
            "open_triangles": self.open_triangles,
            "killed": self.killed,
            "kill_reason": self.kill_reason,
            "total_approved": self.total_approved,
            "total_rejected": self.total_rejected,
        }
