"""Configuration settings using dataclasses for type safety."""

from dataclasses import dataclass, field
from pathlib import Path

# Project root
BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass
class TradingConfig:
    """Risk and trading parameters."""

    min_profit_threshold: float = 0.001  # 0.1% minimum net profit
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
            "BTC": 0.0,
            "ETH": 0.0,
            "BNB": 10.0,  # For fee payment
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
class Config:
    """Root configuration."""

    mode: str = "simulation"  # "simulation" or "live"
    trading: TradingConfig = field(default_factory=TradingConfig)
    fees: FeeConfig = field(default_factory=FeeConfig)
    websocket: WebSocketConfig = field(default_factory=WebSocketConfig)
    simulation: SimulationConfig = field(default_factory=SimulationConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
