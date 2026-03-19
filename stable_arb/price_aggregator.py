"""Multi-source stablecoin price aggregator."""

import logging
from time import time_ns
from typing import Callable

from core.models import Ticker
from stable_arb.models import StablePrice

logger = logging.getLogger(__name__)

# Map of CEX pair symbols to stablecoin names
PAIR_TO_STABLE = {
    "USDCUSDT": ("USDC", True),   # USDC priced in USDT → price = direct
    "DAIUSDT": ("DAI", True),
    "FDUSDUSDT": ("FDUSD", True),
    "TUSDUSDT": ("TUSD", True),
    "USDTUSD": ("USDT", True),     # If available
}

# For USDT monitoring: we infer USDT price from USDC/USDT (inverse)
# If USDCUSDT = 1.0005, then USDT relative to USDC = 1/1.0005 ≈ 0.9995


class StablePriceAggregator:
    """
    Collects stablecoin prices from multiple CEX sources.

    Feeds prices into a DepegDetector callback.
    """

    def __init__(
        self,
        on_price: Callable[[StablePrice], None] | None = None,
    ):
        self.on_price = on_price
        self.total_prices: int = 0

    def handle_ticker(self, source: str, ticker: Ticker) -> None:
        """Process a ticker from a CEX WebSocket feed."""
        symbol = ticker.symbol

        if symbol == "USDCUSDT":
            # USDC price relative to USDT
            usdc_price = ticker.mid
            if usdc_price > 0:
                self._emit(StablePrice("USDC", source, usdc_price, ticker.timestamp_ms))
                # Infer USDT price: if USDC/USDT=1.001, USDT is worth 1/1.001=0.999 in USDC terms
                usdt_price = 1.0 / usdc_price if usdc_price > 0 else 1.0
                self._emit(StablePrice("USDT", source, usdt_price, ticker.timestamp_ms))

        elif symbol == "DAIUSDT":
            if ticker.mid > 0:
                self._emit(StablePrice("DAI", source, ticker.mid, ticker.timestamp_ms))

        elif symbol == "FDUSDUSDT":
            if ticker.mid > 0:
                self._emit(StablePrice("FDUSD", source, ticker.mid, ticker.timestamp_ms))

        elif symbol == "TUSDUSDT":
            if ticker.mid > 0:
                self._emit(StablePrice("TUSD", source, ticker.mid, ticker.timestamp_ms))

    def _emit(self, price: StablePrice) -> None:
        self.total_prices += 1
        if self.on_price:
            self.on_price(price)

    @staticmethod
    def get_ws_symbols() -> set[str]:
        """Symbols to subscribe to on CEX WebSockets."""
        return {"USDCUSDT", "DAIUSDT", "FDUSDUSDT", "TUSDUSDT"}
