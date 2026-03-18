"""Binance REST API client for fetching exchange info and placing orders."""

import logging
from typing import Any

import aiohttp

from core.models import TradingPair

logger = logging.getLogger(__name__)

BINANCE_API_URL = "https://api.binance.com"


class BinanceREST:
    """
    Binance REST API client.

    Used for:
    - Fetching exchange info (all trading pairs)
    - Account balances (live mode)
    - Placing orders (live mode)
    """

    def __init__(self, api_key: str = "", api_secret: str = ""):
        self.api_key = api_key
        self.api_secret = api_secret
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"X-MBX-APIKEY": self.api_key} if self.api_key else {}
            )
        return self._session

    async def get_exchange_info(self) -> dict[str, Any]:
        """Fetch exchange info (all symbols, filters, etc.)."""
        session = await self._get_session()
        url = f"{BINANCE_API_URL}/api/v3/exchangeInfo"

        async with session.get(url) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Exchange info failed ({resp.status}): {text}")
            return await resp.json()

    async def get_all_pairs(
        self,
        quote_assets: list[str] | None = None,
        min_volume_usd: float = 0.0,
    ) -> list[TradingPair]:
        """
        Fetch all TRADING status pairs from Binance.

        Args:
            quote_assets: Filter by these quote assets (e.g., ["USDT", "BTC"]).
            min_volume_usd: Minimum 24h volume filter (requires ticker data).

        Returns:
            List of TradingPair objects.
        """
        info = await self.get_exchange_info()
        pairs: list[TradingPair] = []

        for sym_info in info.get("symbols", []):
            if sym_info.get("status") != "TRADING":
                continue

            base = sym_info["baseAsset"]
            quote = sym_info["quoteAsset"]

            if quote_assets and quote not in quote_assets:
                continue

            # Parse filters
            min_qty = 0.0
            step_size = 0.0
            min_notional = 0.0

            for f in sym_info.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    min_qty = float(f.get("minQty", 0))
                    step_size = float(f.get("stepSize", 0))
                elif f["filterType"] == "NOTIONAL":
                    min_notional = float(f.get("minNotional", 0))

            pairs.append(TradingPair(
                symbol=sym_info["symbol"],
                base_asset=base,
                quote_asset=quote,
                min_qty=min_qty,
                step_size=step_size,
                min_notional=min_notional,
            ))

        logger.info("Loaded %d trading pairs from Binance", len(pairs))
        return pairs

    async def get_ticker_prices(self) -> dict[str, float]:
        """Fetch all current prices (simple price endpoint)."""
        session = await self._get_session()
        url = f"{BINANCE_API_URL}/api/v3/ticker/price"

        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Ticker prices failed ({resp.status})")
            data = await resp.json()

        return {item["symbol"]: float(item["price"]) for item in data}

    async def get_ticker_24h(self, symbol: str) -> dict[str, Any]:
        """Fetch 24h ticker stats for a symbol."""
        session = await self._get_session()
        url = f"{BINANCE_API_URL}/api/v3/ticker/24hr"

        async with session.get(url, params={"symbol": symbol}) as resp:
            if resp.status != 200:
                raise RuntimeError(f"24h ticker failed ({resp.status})")
            return await resp.json()

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
