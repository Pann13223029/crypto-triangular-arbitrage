"""Data models for DEX-CEX arbitrage."""

from dataclasses import dataclass, field
from enum import Enum
from time import time_ns


class Chain(str, Enum):
    BSC = "BSC"
    ARBITRUM = "ARBITRUM"
    ETHEREUM = "ETHEREUM"
    SOLANA = "SOLANA"


class TokenSafetyLevel(str, Enum):
    SAFE = "SAFE"          # Score >= 80
    CAUTION = "CAUTION"    # Score 50-79
    DANGEROUS = "DANGEROUS"  # Score < 50
    UNKNOWN = "UNKNOWN"


@dataclass
class DexQuote:
    """Price quote from a DEX."""

    token: str  # e.g. "CAKE"
    chain: Chain
    dex: str  # e.g. "pancakeswap", "uniswap"
    price_usd: float
    liquidity_usd: float = 0.0
    volume_24h: float = 0.0
    contract_address: str = ""
    timestamp_ms: int = field(default_factory=lambda: time_ns() // 1_000_000)


@dataclass
class TokenSafety:
    """Safety assessment of a token contract."""

    token: str
    chain: Chain
    contract_address: str
    is_honeypot: bool = False
    is_open_source: bool = True
    has_proxy: bool = False
    buy_tax: float = 0.0
    sell_tax: float = 0.0
    safety_score: int = 0  # 0-100
    level: TokenSafetyLevel = TokenSafetyLevel.UNKNOWN

    @property
    def is_safe(self) -> bool:
        return (
            not self.is_honeypot
            and self.safety_score >= 80
            and self.sell_tax < 0.05  # < 5% sell tax
        )


@dataclass
class DexCexOpportunity:
    """A detected DEX-CEX arbitrage opportunity."""

    token: str
    chain: Chain
    dex_price: float
    cex_price: float
    dex_name: str
    cex_name: str
    gross_spread: float  # (high - low) / low
    estimated_gas: float = 0.0
    estimated_fees: float = 0.0
    net_spread: float = 0.0
    direction: str = ""  # "dex→cex" or "cex→dex"
    contract_address: str = ""
    safety: TokenSafety | None = None
    is_new_listing: bool = False
    timestamp_ms: int = field(default_factory=lambda: time_ns() // 1_000_000)
