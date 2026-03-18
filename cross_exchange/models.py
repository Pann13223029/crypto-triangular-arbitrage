"""Data models for cross-exchange arbitrage."""

from dataclasses import dataclass, field
from enum import Enum
from time import time_ns

from core.models import Order


class CrossTradeStatus(str, Enum):
    PENDING = "PENDING"
    ORDERS_SENT = "ORDERS_SENT"
    BOTH_FILLED = "BOTH_FILLED"
    BUY_ONLY = "BUY_ONLY"
    SELL_ONLY = "SELL_ONLY"
    NEITHER = "NEITHER"
    HEDGING = "HEDGING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass
class ExchangeQuote:
    """A price quote from a specific exchange."""

    exchange_id: str
    symbol: str
    bid: float
    ask: float
    bid_qty: float = 0.0
    ask_qty: float = 0.0
    timestamp_ms: int = field(default_factory=lambda: time_ns() // 1_000_000)


@dataclass
class CrossExchangeOpportunity:
    """A detected cross-exchange arbitrage opportunity."""

    symbol: str
    buy_exchange: str
    sell_exchange: str
    buy_price: float  # Best ask on buy exchange
    sell_price: float  # Best bid on sell exchange
    gross_spread: float  # (sell - buy) / buy
    net_spread: float  # After fees
    max_quantity: float = 0.0
    timestamp_ms: int = field(default_factory=lambda: time_ns() // 1_000_000)
    executed: bool = False
    skip_reason: str = ""


class TransferStatus(str, Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"


@dataclass
class Transfer:
    """A fund transfer between exchanges."""

    from_exchange: str
    to_exchange: str
    asset: str
    amount: float
    fee: float = 0.0
    chain: str = "TRC-20"
    status: TransferStatus = TransferStatus.PENDING
    tx_hash: str = ""
    initiated_ms: int = field(default_factory=lambda: time_ns() // 1_000_000)
    confirmed_ms: int = 0


@dataclass
class RebalanceDecision:
    """A rebalancing action plan."""

    transfers: list[Transfer] = field(default_factory=list)
    reason: str = ""
    total_amount: float = 0.0
    total_fees: float = 0.0


@dataclass
class CrossTradeResult:
    """Result of a cross-exchange trade execution."""

    opportunity: CrossExchangeOpportunity
    status: CrossTradeStatus = CrossTradeStatus.PENDING
    buy_order: Order | None = None
    sell_order: Order | None = None
    hedge_order: Order | None = None
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    total_fees: float = 0.0
