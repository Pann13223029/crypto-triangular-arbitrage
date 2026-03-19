"""Data models for funding rate arbitrage."""

from dataclasses import dataclass, field
from enum import Enum
from time import time_ns


class FundingDirection(str, Enum):
    LONGS_PAY = "LONGS_PAY"  # Positive rate: we go long spot + short perp
    SHORTS_PAY = "SHORTS_PAY"  # Negative rate: we go short spot + long perp (harder)


class PositionStatus(str, Enum):
    SCANNING = "SCANNING"
    ENTERING = "ENTERING"
    ACTIVE = "ACTIVE"
    EXITING = "EXITING"
    CLOSED = "CLOSED"
    LIQUIDATION_RISK = "LIQUIDATION_RISK"


@dataclass
class FundingOpportunity:
    """A detected funding rate spike."""

    symbol: str  # KuCoin futures symbol e.g. LRCUSDTM
    base_asset: str  # e.g. LRC
    funding_rate: float  # Per 8h period
    predicted_rate: float  # Next period predicted
    direction: FundingDirection
    daily_rate: float = 0.0  # rate * 3
    annualized: float = 0.0  # rate * 3 * 365
    timestamp_ms: int = field(default_factory=lambda: time_ns() // 1_000_000)

    @property
    def abs_rate(self) -> float:
        return abs(self.funding_rate)

    @property
    def is_longs_pay(self) -> bool:
        """Positive funding = longs pay shorts. We want to be short perp."""
        return self.funding_rate > 0


@dataclass
class FundingPosition:
    """An active funding rate arb position."""

    symbol: str
    base_asset: str
    spot_symbol: str  # e.g. LRC-USDT (KuCoin spot)
    direction: FundingDirection

    # Position details
    spot_quantity: float = 0.0
    spot_entry_price: float = 0.0
    futures_quantity: float = 0.0
    futures_entry_price: float = 0.0
    position_usd: float = 0.0

    # Tracking
    status: PositionStatus = PositionStatus.SCANNING
    entry_funding_rate: float = 0.0
    funding_collected: float = 0.0
    funding_periods: int = 0
    total_fees: float = 0.0
    entry_time_ms: int = 0
    exit_time_ms: int = 0

    # Risk
    current_basis: float = 0.0  # Spot-futures price difference
    margin_ratio: float = 0.0  # Futures margin health

    @property
    def net_pnl(self) -> float:
        return self.funding_collected - self.total_fees

    @property
    def holding_hours(self) -> float:
        if self.entry_time_ms == 0:
            return 0
        now = time_ns() // 1_000_000
        end = self.exit_time_ms if self.exit_time_ms > 0 else now
        return (end - self.entry_time_ms) / 3_600_000
