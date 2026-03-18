"""Configuration settings using dataclasses for type safety."""

from dataclasses import dataclass, field
from pathlib import Path

# Project root
BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass
class TradingConfig:
    """Risk and trading parameters."""

    min_profit_threshold: float = 0.0001  # 0.01% minimum net profit
    max_position_size_usd: float = 500.0  # Max USD per triangle
    daily_loss_limit_usd: float = 50.0  # Kill switch threshold
    max_open_triangles: int = 1  # One at a time (v1)
    slippage_tolerance: float = 0.0005  # 0.05%
    cooldown_after_loss_sec: float = 60.0  # Pause after loss
    max_consecutive_losses: int = 3  # Halt after N losses


@dataclass
class FeeConfig:
    """Binance fee structure."""

    maker_fee: float = 0.001  # 0.1%
    taker_fee: float = 0.001  # 0.1%
    bnb_discount: float = 0.25  # 25% discount with BNB
    use_bnb_fee: bool = True  # Pay fees in BNB

    @property
    def effective_fee(self) -> float:
        """Fee rate after BNB discount."""
        base = self.taker_fee  # Market orders = taker
        if self.use_bnb_fee:
            return base * (1 - self.bnb_discount)
        return base


@dataclass
class WebSocketConfig:
    """Binance WebSocket settings."""

    base_url: str = "wss://stream.binance.com:9443/ws"
    health_timeout_sec: float = 5.0  # Reconnect if no msg
    reconnect_max_retries: int = 10
    reconnect_base_delay_sec: float = 1.0  # Exponential backoff base
    order_book_depth: int = 5  # Top-N levels


@dataclass
class SimulationConfig:
    """Paper trading settings."""

    initial_balances: dict[str, float] = field(
        default_factory=lambda: {
            "USDT": 10000.0,
            "BTC": 0.15,     # ~$10K worth
            "ETH": 4.0,      # ~$10K worth
            "BNB": 15.0,     # ~$10K worth + fee payment
        }
    )
    slippage_model: str = "fixed"  # "fixed", "random", "depth"
    fixed_slippage: float = 0.0001  # 0.01%
    latency_ms: float = 50.0  # Simulated API latency


@dataclass
class DatabaseConfig:
    """SQLite settings."""

    db_path: str = str(BASE_DIR / "data" / "arbitrage.db")


@dataclass
class ScannerConfig:
    """Triangle scanner settings."""

    quote_assets: list[str] = field(
        default_factory=lambda: ["USDT", "BTC", "ETH", "BNB"]
    )
    min_volume_usd_24h: float = 100_000.0  # Filter low-liquidity pairs
    max_triangles: int = 5000  # Safety cap


@dataclass
class FeeSchedule:
    """Exchange-agnostic fee schedule."""

    exchange_id: str = ""
    taker_fee: float = 0.001
    maker_fee: float = 0.001
    withdrawal_fees: dict[str, dict[str, float]] = field(default_factory=dict)

    def round_trip_cost(self, other: "FeeSchedule") -> float:
        """Total taker fee cost for buy on self + sell on other."""
        return self.taker_fee + other.taker_fee


@dataclass
class CrossExchangeConfig:
    """Cross-exchange arbitrage settings."""

    enabled: bool = True
    min_net_spread: float = 0.0005  # 0.05% minimum after fees
    max_position_size_usd: float = 500.0
    staleness_threshold_ms: int = 1000  # 1 second
    max_concurrent_arbs: int = 3
    dedup_cooldown_ms: int = 5000  # 5 seconds (bookTicker fires much more frequently)
    symbols: list[str] = field(
        default_factory=lambda: [
            # Tier 1: Mid-cap with wide cross-exchange spreads (0.2-1%+)
            "BARDUSDT", "ZAMAUSDT", "SAHARAUSDT", "ARKMUSDT", "CFGUSDT",
            "GUSDT", "WIFUSDT", "THETAUSDT", "TNSRUSDT",
            # Tier 2: Decent spreads (0.1-0.2%)
            "WOOUSDT", "MINAUSDT", "ARUSDT", "YFIUSDT", "CITYUSDT",
            "PYTHUSDT", "NOTUSDT", "AGLDUSDT", "BICOUSDT",
            # Tier 3: Liquid pairs for monitoring (tight spreads)
            "BTCUSDT", "ETHUSDT", "SOLUSDT",
        ]
    )
    max_spread_anomaly: float = 0.05  # 5% — reject spreads above this (likely stale)


@dataclass
class MultiSimConfig:
    """Multi-exchange simulation parameters."""

    exchange_ids: list[str] = field(
        default_factory=lambda: ["sim_binance", "sim_bybit", "sim_okx"]
    )
    initial_balances_per_exchange: dict[str, float] = field(
        default_factory=lambda: {
            "USDT": 10000.0,
            "BTC": 0.15,
            "ETH": 4.0,
            "BNB": 15.0,
        }
    )
    ou_theta: float = 0.1  # Mean reversion speed
    ou_sigma: float = 0.002  # Spread volatility (~0.2%)
    ou_mu: float = 0.0  # Long-run mean (zero)


@dataclass
class RebalanceConfig:
    """Rebalancing settings for cross-exchange capital management."""

    enabled: bool = True
    deviation_threshold: float = 0.25  # 25% deviation triggers rebalance
    min_rebalance_usd: float = 500.0  # Minimum transfer amount
    cooldown_sec: float = 30.0  # 30 seconds between rebalances (default for sim)
    check_interval_sec: float = 20.0  # How often to check balances
    target_allocation: dict[str, float] = field(default_factory=dict)
    # e.g. {"sim_binance": 0.33, "sim_bybit": 0.33, "sim_okx": 0.34}
    # If empty, defaults to equal allocation across all exchanges
    preferred_chain: str = "TRC-20"  # For stablecoin transfers
    transfer_fee_usd: float = 1.0  # Simulated transfer fee


@dataclass
class Config:
    """Root configuration."""

    mode: str = "simulation"  # "simulation" or "live"
    trading: TradingConfig = field(default_factory=TradingConfig)
    fees: FeeConfig = field(default_factory=FeeConfig)
    websocket: WebSocketConfig = field(default_factory=WebSocketConfig)
    simulation: SimulationConfig = field(default_factory=SimulationConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    cross_exchange: CrossExchangeConfig = field(default_factory=CrossExchangeConfig)
    multi_sim: MultiSimConfig = field(default_factory=MultiSimConfig)
    rebalance: RebalanceConfig = field(default_factory=RebalanceConfig)
