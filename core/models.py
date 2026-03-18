"""Data models for the arbitrage system."""

from dataclasses import dataclass, field
from enum import Enum
from time import time_ns


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Direction(str, Enum):
    FORWARD = "FORWARD"
    REVERSE = "REVERSE"


@dataclass
class TradingPair:
    """A tradeable pair on the exchange."""

    symbol: str  # e.g. "BTCUSDT"
    base_asset: str  # e.g. "BTC"
    quote_asset: str  # e.g. "USDT"
    min_qty: float = 0.0
    step_size: float = 0.0
    min_notional: float = 0.0  # Minimum order value


@dataclass
class Ticker:
    """Current price snapshot for a pair."""

    symbol: str
    bid: float  # Best bid (sell price)
    ask: float  # Best ask (buy price)
    timestamp_ms: int = 0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        if self.ask == 0:
            return 0.0
        return (self.ask - self.bid) / self.ask


@dataclass
class OrderBookLevel:
    """Single price level in the order book."""

    price: float
    quantity: float


@dataclass
class OrderBook:
    """Order book snapshot."""

    symbol: str
    bids: list[OrderBookLevel] = field(default_factory=list)  # Descending
    asks: list[OrderBookLevel] = field(default_factory=list)  # Ascending
    timestamp_ms: int = 0

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 0.0

    def executable_buy_price(self, quantity: float) -> float | None:
        """Average price to buy `quantity` walking up the ask book."""
        remaining = quantity
        total_cost = 0.0
        for level in self.asks:
            fill = min(remaining, level.quantity)
            total_cost += fill * level.price
            remaining -= fill
            if remaining <= 0:
                return total_cost / quantity
        return None  # Insufficient liquidity

    def executable_sell_price(self, quantity: float) -> float | None:
        """Average price to sell `quantity` walking down the bid book."""
        remaining = quantity
        total_revenue = 0.0
        for level in self.bids:
            fill = min(remaining, level.quantity)
            total_revenue += fill * level.price
            remaining -= fill
            if remaining <= 0:
                return total_revenue / quantity
        return None  # Insufficient liquidity


@dataclass
class TriangleLeg:
    """One leg of a triangle trade."""

    symbol: str  # Trading pair symbol
    side: OrderSide  # BUY or SELL
    base_asset: str
    quote_asset: str


@dataclass
class Triangle:
    """A 3-pair arbitrage triangle."""

    id: int
    assets: tuple[str, str, str]  # e.g. ("USDT", "BTC", "ETH")
    forward_legs: tuple[TriangleLeg, TriangleLeg, TriangleLeg]
    reverse_legs: tuple[TriangleLeg, TriangleLeg, TriangleLeg]
    symbols: frozenset[str] = field(init=False)  # Set of pair symbols

    def __post_init__(self):
        self.symbols = frozenset(
            leg.symbol for leg in self.forward_legs
        )

    def __hash__(self):
        return hash(self.symbols)

    def __eq__(self, other):
        if not isinstance(other, Triangle):
            return False
        return self.symbols == other.symbols


@dataclass
class Opportunity:
    """A detected arbitrage opportunity."""

    triangle: Triangle
    direction: Direction
    theoretical_profit: float  # Before slippage
    executable_profit: float | None = None  # After order book simulation
    prices: dict[str, Ticker] = field(default_factory=dict)
    timestamp_ms: int = field(default_factory=lambda: time_ns() // 1_000_000)
    executed: bool = False
    skip_reason: str = ""


@dataclass
class Order:
    """An executed or pending order."""

    id: str = ""
    symbol: str = ""
    side: OrderSide = OrderSide.BUY
    quantity: float = 0.0
    expected_price: float = 0.0
    actual_price: float = 0.0
    fee: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    timestamp_ms: int = field(default_factory=lambda: time_ns() // 1_000_000)

    @property
    def slippage(self) -> float:
        """Actual slippage vs expected price."""
        if self.expected_price == 0:
            return 0.0
        return abs(self.actual_price - self.expected_price) / self.expected_price


@dataclass
class TradeResult:
    """Result of executing a full triangle."""

    opportunity: Opportunity
    orders: list[Order] = field(default_factory=list)
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    total_fees: float = 0.0
    aborted: bool = False
    abort_reason: str = ""
