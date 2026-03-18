"""Cross-exchange risk manager — extends base risk controls."""

import logging
from time import time_ns

from config.settings import CrossExchangeConfig, TradingConfig
from cross_exchange.models import CrossExchangeOpportunity

logger = logging.getLogger(__name__)


class CrossExchangeRiskManager:
    """
    Risk management for cross-exchange arbitrage.

    Controls: kill switch, daily loss limit, cooldowns,
    concurrent arb limit, exchange health, emergency hedge tracking.
    """

    def __init__(
        self,
        trading_config: TradingConfig | None = None,
        cx_config: CrossExchangeConfig | None = None,
    ):
        self.trading_config = trading_config or TradingConfig()
        self.cx_config = cx_config or CrossExchangeConfig()

        # State
        self.daily_pnl: float = 0.0
        self.consecutive_losses: int = 0
        self.last_loss_time_ms: int = 0
        self.active_arbs: int = 0
        self.emergency_hedge_count: int = 0
        self.killed: bool = False
        self.kill_reason: str = ""

        # Exchange health: exchange_id -> healthy
        self.exchange_healthy: dict[str, bool] = {}

        # Stats
        self.total_approved: int = 0
        self.total_rejected: int = 0

        # Limits
        self.emergency_hedge_limit: int = 3  # Per session
        self.max_concurrent = self.cx_config.max_concurrent_arbs

    def check(
        self,
        opp: CrossExchangeOpportunity,
    ) -> tuple[bool, str]:
        """
        Check if a cross-exchange opportunity passes all risk checks.

        Returns (approved, reason).
        """
        # Kill switch
        if self.killed:
            return False, f"Kill switch: {self.kill_reason}"

        # Daily loss limit
        if self.daily_pnl <= -self.trading_config.daily_loss_limit_usd:
            self.kill(f"Daily loss limit: ${self.daily_pnl:.2f}")
            return False, "Daily loss limit"

        # Emergency hedge limit
        if self.emergency_hedge_count >= self.emergency_hedge_limit:
            self.kill(f"Emergency hedge limit: {self.emergency_hedge_count}")
            return False, "Too many emergency hedges"

        # Consecutive losses
        if self.consecutive_losses >= self.trading_config.max_consecutive_losses:
            self.kill(f"Consecutive losses: {self.consecutive_losses}")
            return False, "Max consecutive losses"

        # Cooldown after loss
        if self.last_loss_time_ms > 0:
            now = time_ns() // 1_000_000
            elapsed = (now - self.last_loss_time_ms) / 1000
            if elapsed < self.trading_config.cooldown_after_loss_sec:
                self.total_rejected += 1
                return False, f"Cooldown ({self.trading_config.cooldown_after_loss_sec - elapsed:.0f}s)"

        # Concurrent arb limit
        if self.active_arbs >= self.max_concurrent:
            self.total_rejected += 1
            return False, f"Max concurrent arbs ({self.active_arbs})"

        # Min spread
        if opp.net_spread < self.cx_config.min_net_spread:
            self.total_rejected += 1
            return False, f"Below min spread ({opp.net_spread:.4%})"

        # Exchange health
        for ex_id in [opp.buy_exchange, opp.sell_exchange]:
            if not self.exchange_healthy.get(ex_id, True):
                self.total_rejected += 1
                return False, f"Exchange unhealthy: {ex_id}"

        # Anomaly check: spread > 0.5% on liquid pairs is suspicious
        if opp.net_spread > 0.005:
            logger.warning(
                "Large spread %.4f%% on %s — possible stale price",
                opp.net_spread * 100, opp.symbol,
            )

        self.total_approved += 1
        return True, "Approved"

    def record_trade_result(self, pnl: float, had_emergency_hedge: bool = False) -> None:
        """Record the result of a completed trade."""
        self.daily_pnl += pnl

        if pnl < 0:
            self.consecutive_losses += 1
            self.last_loss_time_ms = time_ns() // 1_000_000
        else:
            self.consecutive_losses = 0

        if had_emergency_hedge:
            self.emergency_hedge_count += 1

    def on_arb_start(self) -> None:
        self.active_arbs += 1

    def on_arb_end(self) -> None:
        self.active_arbs = max(0, self.active_arbs - 1)

    def set_exchange_health(self, exchange_id: str, healthy: bool) -> None:
        self.exchange_healthy[exchange_id] = healthy
        if not healthy:
            logger.warning("Exchange marked unhealthy: %s", exchange_id)

    def kill(self, reason: str) -> None:
        if not self.killed:
            logger.critical("KILL SWITCH: %s", reason)
        self.killed = True
        self.kill_reason = reason

    def reset_daily(self) -> None:
        self.daily_pnl = 0.0
        self.consecutive_losses = 0
        self.last_loss_time_ms = 0
        self.emergency_hedge_count = 0
        self.killed = False
        self.kill_reason = ""

    def stats(self) -> dict:
        return {
            "daily_pnl": round(self.daily_pnl, 4),
            "consecutive_losses": self.consecutive_losses,
            "active_arbs": self.active_arbs,
            "emergency_hedges": self.emergency_hedge_count,
            "killed": self.killed,
            "kill_reason": self.kill_reason,
            "approved": self.total_approved,
            "rejected": self.total_rejected,
        }
