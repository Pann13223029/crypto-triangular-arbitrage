"""Position manager — enters and exits funding rate arb positions."""

import logging
import subprocess
import sys
from time import time_ns

from funding_arb.models import (
    FundingDirection,
    FundingOpportunity,
    FundingPosition,
    PositionStatus,
)

logger = logging.getLogger(__name__)


class FundingPositionManager:
    """
    Manages funding rate arb positions on KuCoin.

    Entry: Buy spot + short perp (when longs pay) — delta neutral
    Exit: Sell spot + close perp short (when funding normalizes)

    For SHORTS_PAY direction, we'd need to short spot (margin) which
    is more complex — for v1, only support LONGS_PAY direction.
    """

    def __init__(
        self,
        kucoin_exchange,
        total_capital: float = 30.0,
        reserve_pct: float = 0.20,  # 20% cash reserve
        min_funding_rate: float = 0.001,  # 0.10% per 8h entry
        exit_funding_rate: float = 0.0005,  # 0.05% per 8h exit
        max_holding_days: int = 7,
        basis_stop_loss: float = 0.015,  # 1.5% basis divergence
    ):
        self.exchange = kucoin_exchange
        self.total_capital = total_capital
        self.reserve_pct = reserve_pct
        self.deployable = total_capital * (1 - reserve_pct)
        self.min_funding_rate = min_funding_rate
        self.exit_funding_rate = exit_funding_rate
        self.max_holding_days = max_holding_days
        self.basis_stop_loss = basis_stop_loss

        # State
        self.active_position: FundingPosition | None = None
        self.closed_positions: list[FundingPosition] = []

        # Stats
        self.total_entries: int = 0
        self.total_exits: int = 0
        self.total_funding_collected: float = 0.0
        self.total_fees_paid: float = 0.0

    def should_enter(self, opp: FundingOpportunity) -> tuple[bool, str]:
        """Check if we should enter a position on this opportunity."""
        if self.active_position is not None:
            return False, "Already have active position"

        if opp.abs_rate < self.min_funding_rate:
            return False, f"Rate {opp.abs_rate:.4%} below threshold {self.min_funding_rate:.4%}"

        # v1: only support LONGS_PAY (positive funding)
        # Strategy: long spot + short perp = collect funding from longs
        if not opp.is_longs_pay:
            return False, "SHORTS_PAY not supported in v1 (need margin short)"

        return True, "Approved"

    def should_exit(self, current_funding_rate: float) -> tuple[bool, str]:
        """Check if we should exit the active position."""
        if self.active_position is None:
            return False, "No active position"

        pos = self.active_position

        # Funding normalized
        if abs(current_funding_rate) < self.exit_funding_rate:
            return True, f"Funding normalized to {current_funding_rate:.4%}"

        # Max holding period
        if pos.holding_hours > self.max_holding_days * 24:
            return True, f"Max holding period ({self.max_holding_days}d) exceeded"

        # Basis divergence
        if abs(pos.current_basis) > self.basis_stop_loss:
            return True, f"Basis divergence {pos.current_basis:.4%} > {self.basis_stop_loss:.4%}"

        return False, "Hold"

    def record_funding_payment(self, amount: float) -> None:
        """Record a funding payment received."""
        if self.active_position is None:
            return
        self.active_position.funding_collected += amount
        self.active_position.funding_periods += 1
        self.total_funding_collected += amount
        logger.info(
            "Funding received: $%.6f (total: $%.6f, %d periods)",
            amount, self.active_position.funding_collected,
            self.active_position.funding_periods,
        )

    def create_position(self, opp: FundingOpportunity) -> FundingPosition:
        """Create a new position (not yet executed)."""
        spot_symbol = f"{opp.base_asset}-USDT"
        pos = FundingPosition(
            symbol=opp.symbol,
            base_asset=opp.base_asset,
            spot_symbol=spot_symbol,
            direction=opp.direction,
            position_usd=self.deployable / 2,  # Half for spot, half for futures margin
            entry_funding_rate=opp.funding_rate,
            status=PositionStatus.ENTERING,
            entry_time_ms=time_ns() // 1_000_000,
        )
        self.active_position = pos
        self.total_entries += 1
        return pos

    def close_position(self) -> FundingPosition | None:
        """Mark active position for closing."""
        if self.active_position is None:
            return None
        self.active_position.status = PositionStatus.EXITING
        self.active_position.exit_time_ms = time_ns() // 1_000_000
        return self.active_position

    def finalize_close(self) -> None:
        """Move active position to closed list and sync totals."""
        if self.active_position is None:
            return
        pos = self.active_position
        pos.status = PositionStatus.CLOSED
        self.closed_positions.append(pos)
        self.total_exits += 1
        # Sync totals from position (handles both fresh and restored positions)
        self.total_funding_collected = sum(p.funding_collected for p in self.closed_positions)
        self.total_fees_paid = sum(p.total_fees for p in self.closed_positions)
        logger.info(
            "Position closed: %s | Funding: $%.4f | Fees: $%.4f | Net: $%.4f | Held: %.1fh",
            self.active_position.symbol,
            self.active_position.funding_collected,
            self.active_position.total_fees,
            self.active_position.net_pnl,
            self.active_position.holding_hours,
        )
        self.active_position = None

    def alert(self, message: str) -> None:
        """Terminal + sound alert."""
        print("\n" + "=" * 60)
        print(f"  🔔  {message}")
        print("=" * 60 + "\n")
        try:
            if sys.platform == "darwin":
                subprocess.Popen(
                    ["afplay", "/System/Library/Sounds/Glass.aiff"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            else:
                print("\a", end="", flush=True)
        except Exception:
            print("\a", end="", flush=True)

    def stats(self) -> dict:
        active = None
        if self.active_position:
            p = self.active_position
            active = {
                "symbol": p.symbol,
                "funding_collected": round(p.funding_collected, 6),
                "fees": round(p.total_fees, 6),
                "net_pnl": round(p.net_pnl, 6),
                "periods": p.funding_periods,
                "hours": round(p.holding_hours, 1),
            }

        return {
            "active_position": active,
            "total_entries": self.total_entries,
            "total_exits": self.total_exits,
            "total_funding": round(self.total_funding_collected, 6),
            "total_fees": round(self.total_fees_paid, 6),
            "net_pnl": round(self.total_funding_collected - self.total_fees_paid, 6),
            "closed_positions": len(self.closed_positions),
        }
