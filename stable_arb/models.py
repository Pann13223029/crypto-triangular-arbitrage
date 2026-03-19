"""Data models for stablecoin depeg monitoring."""

from dataclasses import dataclass, field
from enum import Enum
from time import time_ns


class DepegSeverity(str, Enum):
    NORMAL = "NORMAL"          # < 0.1%
    WATCHING = "WATCHING"      # 0.1-0.3%
    MILD = "MILD"              # 0.3-0.5% → ALERT
    MODERATE = "MODERATE"      # 0.5-2.0% → AUTO-EXECUTE (whitelisted)
    SEVERE = "SEVERE"          # 2.0-5.0% → HUMAN APPROVE
    CRISIS = "CRISIS"          # > 5.0% → ALERT ONLY


class SafetyTier(str, Enum):
    AUTO_EXECUTE = "AUTO_EXECUTE"    # USDT, USDC
    HUMAN_APPROVE = "HUMAN_APPROVE"  # DAI, FDUSD
    ALERT_ONLY = "ALERT_ONLY"        # Everything else


# Stablecoin safety classification
STABLE_WHITELIST = {
    "USDT": SafetyTier.AUTO_EXECUTE,
    "USDC": SafetyTier.AUTO_EXECUTE,
    "DAI": SafetyTier.HUMAN_APPROVE,
    "FDUSD": SafetyTier.HUMAN_APPROVE,
    "TUSD": SafetyTier.ALERT_ONLY,
}


@dataclass
class StablePrice:
    """Price of a stablecoin from a specific source."""

    stable: str  # e.g. "USDT"
    source: str  # e.g. "kucoin", "binance", "pancakeswap"
    price: float  # e.g. 0.9985
    timestamp_ms: int = field(default_factory=lambda: time_ns() // 1_000_000)


@dataclass
class DepegEvent:
    """A detected stablecoin depeg event."""

    stable: str
    severity: DepegSeverity
    safety_tier: SafetyTier
    deviation: float  # e.g. 0.015 = 1.5% below peg
    median_price: float  # e.g. 0.985
    sources: list[StablePrice] = field(default_factory=list)
    confirmation_count: int = 0
    first_detected_ms: int = field(default_factory=lambda: time_ns() // 1_000_000)
    timestamp_ms: int = field(default_factory=lambda: time_ns() // 1_000_000)

    @property
    def is_auto_executable(self) -> bool:
        return (
            self.safety_tier == SafetyTier.AUTO_EXECUTE
            and self.severity in (DepegSeverity.MODERATE, DepegSeverity.SEVERE)
        )

    @property
    def needs_human(self) -> bool:
        return (
            self.safety_tier == SafetyTier.HUMAN_APPROVE
            and self.severity in (DepegSeverity.MODERATE, DepegSeverity.SEVERE)
        ) or self.severity == DepegSeverity.SEVERE


@dataclass
class DepegPosition:
    """A position taken during a depeg event."""

    stable: str
    buy_price: float
    quantity: float
    buy_exchange: str
    entry_time_ms: int = field(default_factory=lambda: time_ns() // 1_000_000)
    exit_time_ms: int = 0
    exit_price: float = 0.0
    stop_loss_price: float = 0.0  # 2x the depeg at entry
    pnl: float = 0.0
