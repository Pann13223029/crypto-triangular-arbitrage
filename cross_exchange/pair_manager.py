"""Adaptive pair manager — discovers, ranks, promotes, and demotes trading pairs."""

import asyncio
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from time import time_ns

from config.settings import FeeSchedule
from core.models import Ticker

logger = logging.getLogger(__name__)


class PairStatus(str, Enum):
    ACTIVE = "ACTIVE"
    ON_DECK = "ON_DECK"
    DEMOTED = "DEMOTED"
    PENDING_APPROVAL = "PENDING_APPROVAL"


@dataclass
class PairCandidate:
    """A candidate trading pair with spread metrics."""

    symbol: str
    best_route: str  # e.g., "kucoin→binance"
    buy_exchange: str
    sell_exchange: str
    gross_spread: float
    net_spread: float
    price: float
    status: PairStatus = PairStatus.ON_DECK
    last_updated_ms: int = field(default_factory=lambda: time_ns() // 1_000_000)

    # Demotion tracking (for active pair)
    low_spread_since_ms: int = 0  # When spread first dropped below threshold
    consecutive_losses: int = 0


class PairManager:
    """
    Manages adaptive pair selection for cross-exchange arbitrage.

    - Scans all common pairs every 30 minutes (or on emergency trigger)
    - Maintains 1 active + 4 on-deck pairs ranked by spread
    - Demotes active pair when spread < 0.3% for 10 min OR 3 consecutive losses
    - Notifies user (terminal + sound) for approval before switching
    - Auto-pauses trading during switch
    """

    def __init__(
        self,
        fee_schedules: dict[str, FeeSchedule],
        scan_interval_sec: float = 1800,  # 30 minutes
        demotion_spread_threshold: float = 0.003,  # 0.3% net
        demotion_time_sec: float = 600,  # 10 minutes below threshold
        demotion_max_losses: int = 3,
        on_deck_count: int = 4,
    ):
        self.fee_schedules = fee_schedules
        self.scan_interval_sec = scan_interval_sec
        self.demotion_spread_threshold = demotion_spread_threshold
        self.demotion_time_sec = demotion_time_sec
        self.demotion_max_losses = demotion_max_losses
        self.on_deck_count = on_deck_count

        # State
        self.active_pair: PairCandidate | None = None
        self.on_deck: list[PairCandidate] = []
        self.all_candidates: list[PairCandidate] = []
        self.paused: bool = False
        self.pending_promotion: PairCandidate | None = None

        # Scan history
        self.last_scan_ms: int = 0
        self.total_scans: int = 0
        self.total_promotions: int = 0
        self.total_demotions: int = 0

    def set_active(self, symbol: str) -> bool:
        """Manually set the active trading pair."""
        for c in self.all_candidates:
            if c.symbol == symbol:
                if self.active_pair:
                    self.active_pair.status = PairStatus.DEMOTED
                c.status = PairStatus.ACTIVE
                c.consecutive_losses = 0
                c.low_spread_since_ms = 0
                self.active_pair = c
                self.paused = False
                logger.info("Active pair set: %s (%s, net: %.4f%%)",
                            symbol, c.best_route, c.net_spread * 100)
                return True
        logger.warning("Symbol %s not found in candidates", symbol)
        return False

    def update_candidates(self, candidates: list[PairCandidate]) -> None:
        """Update the full candidate list from a scan."""
        self.all_candidates = candidates
        self.last_scan_ms = time_ns() // 1_000_000
        self.total_scans += 1

        # Update on-deck (exclude active pair and demoted)
        active_symbol = self.active_pair.symbol if self.active_pair else ""
        ranked = [
            c for c in candidates
            if c.symbol != active_symbol
            and c.status != PairStatus.DEMOTED
            and c.net_spread > 0
        ]
        ranked.sort(key=lambda c: -c.net_spread)
        self.on_deck = ranked[:self.on_deck_count]

        for c in self.on_deck:
            c.status = PairStatus.ON_DECK

        logger.info(
            "Pair scan: %d candidates, %d profitable, on-deck: %s",
            len(candidates),
            len([c for c in candidates if c.net_spread > 0]),
            ", ".join(f"{c.symbol}({c.net_spread:.2%})" for c in self.on_deck[:3]),
        )

    def check_demotion(self, current_net_spread: float) -> bool:
        """
        Check if the active pair should be demoted.

        Returns True if demotion triggered (pauses trading + alerts user).
        """
        if self.active_pair is None or self.paused:
            return False

        now_ms = time_ns() // 1_000_000

        # Check spread threshold
        if current_net_spread < self.demotion_spread_threshold:
            if self.active_pair.low_spread_since_ms == 0:
                self.active_pair.low_spread_since_ms = now_ms
                logger.warning(
                    "Active pair %s spread dropped to %.4f%% (threshold: %.4f%%)",
                    self.active_pair.symbol,
                    current_net_spread * 100,
                    self.demotion_spread_threshold * 100,
                )

            elapsed_sec = (now_ms - self.active_pair.low_spread_since_ms) / 1000
            if elapsed_sec >= self.demotion_time_sec:
                return self._trigger_demotion(
                    f"Spread below {self.demotion_spread_threshold:.1%} for {elapsed_sec:.0f}s"
                )
        else:
            # Spread recovered — reset timer
            self.active_pair.low_spread_since_ms = 0

        # Check consecutive losses
        if self.active_pair.consecutive_losses >= self.demotion_max_losses:
            return self._trigger_demotion(
                f"{self.active_pair.consecutive_losses} consecutive losses"
            )

        return False

    def record_trade_result(self, profitable: bool) -> None:
        """Record a trade result on the active pair."""
        if self.active_pair is None:
            return

        if profitable:
            self.active_pair.consecutive_losses = 0
        else:
            self.active_pair.consecutive_losses += 1

    def _trigger_demotion(self, reason: str) -> bool:
        """Demote active pair and suggest promotion."""
        if self.active_pair is None:
            return False

        old_symbol = self.active_pair.symbol
        self.active_pair.status = PairStatus.DEMOTED
        self.total_demotions += 1
        self.paused = True

        logger.critical(
            "DEMOTION: %s — %s", old_symbol, reason,
        )

        # Find best on-deck pair
        if self.on_deck:
            best = self.on_deck[0]
            self.pending_promotion = best
            best.status = PairStatus.PENDING_APPROVAL

            self._alert(
                f"PAIR SWITCH: {old_symbol} demoted ({reason}). "
                f"Recommend: {best.symbol} ({best.best_route}, net: {best.net_spread:.2%}). "
                f"Trading PAUSED."
            )
        else:
            self.pending_promotion = None
            self._alert(
                f"PAIR SWITCH: {old_symbol} demoted ({reason}). "
                f"No on-deck pairs available. Trading PAUSED."
            )

        return True

    def approve_promotion(self) -> PairCandidate | None:
        """User approves the pending promotion. Returns the new active pair."""
        if self.pending_promotion is None:
            logger.warning("No pending promotion to approve")
            return None

        new_pair = self.pending_promotion
        if self.active_pair:
            self.active_pair.status = PairStatus.DEMOTED

        new_pair.status = PairStatus.ACTIVE
        new_pair.consecutive_losses = 0
        new_pair.low_spread_since_ms = 0
        self.active_pair = new_pair
        self.pending_promotion = None
        self.total_promotions += 1

        # Don't unpause — user needs to buy tokens first
        logger.info(
            "PROMOTED: %s (%s). Buy tokens on %s, then resume.",
            new_pair.symbol, new_pair.best_route, new_pair.sell_exchange,
        )

        self._alert(
            f"PROMOTED: {new_pair.symbol}. "
            f"Buy {new_pair.symbol.replace('USDT', '')} on {new_pair.sell_exchange}, "
            f"then type 'resume' to start trading."
        )

        return new_pair

    def decline_promotion(self) -> None:
        """User declines the promotion."""
        if self.pending_promotion:
            self.pending_promotion.status = PairStatus.ON_DECK
            self.pending_promotion = None
        logger.info("Promotion declined. Trading remains paused.")

    def resume(self) -> bool:
        """Resume trading after a pair switch."""
        if self.active_pair is None:
            logger.warning("No active pair to resume")
            return False
        self.paused = False
        logger.info("Trading RESUMED on %s", self.active_pair.symbol)
        return True

    def needs_scan(self) -> bool:
        """Check if it's time for a discovery scan."""
        if self.last_scan_ms == 0:
            return True
        now_ms = time_ns() // 1_000_000
        return (now_ms - self.last_scan_ms) / 1000 >= self.scan_interval_sec

    def needs_emergency_scan(self) -> bool:
        """Check if active pair has been weak long enough to trigger emergency scan."""
        if self.active_pair is None or self.active_pair.low_spread_since_ms == 0:
            return False
        now_ms = time_ns() // 1_000_000
        elapsed = (now_ms - self.active_pair.low_spread_since_ms) / 1000
        # Emergency scan at half the demotion time
        return elapsed >= (self.demotion_time_sec / 2)

    def get_active_symbols(self) -> list[str]:
        """Get symbols that should be on the WebSocket (active + on-deck)."""
        symbols = set()
        if self.active_pair:
            symbols.add(self.active_pair.symbol)
        for c in self.on_deck:
            symbols.add(c.symbol)
        return sorted(symbols)

    def _alert(self, message: str) -> None:
        """Terminal alert with sound."""
        # Print prominent alert
        print("\n" + "=" * 60)
        print(f"  🔔  {message}")
        print("=" * 60 + "\n")

        # System sound
        try:
            if sys.platform == "darwin":
                subprocess.Popen(
                    ["afplay", "/System/Library/Sounds/Glass.aiff"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                print("\a", end="", flush=True)  # Terminal bell
        except Exception:
            print("\a", end="", flush=True)

    def status_summary(self) -> dict:
        return {
            "active": (
                f"{self.active_pair.symbol} ({self.active_pair.net_spread:.2%})"
                if self.active_pair else "None"
            ),
            "paused": self.paused,
            "on_deck": [
                f"{c.symbol}({c.net_spread:.2%})" for c in self.on_deck
            ],
            "pending_promotion": (
                self.pending_promotion.symbol if self.pending_promotion else None
            ),
            "total_scans": self.total_scans,
            "total_promotions": self.total_promotions,
            "total_demotions": self.total_demotions,
        }

    def stats(self) -> dict:
        return self.status_summary()
