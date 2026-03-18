"""OKX WebSocket client for real-time price feeds."""

import asyncio
import logging
from time import time_ns
from typing import Callable

try:
    import orjson
    _loads = orjson.loads
except ImportError:
    import json
    _loads = json.loads

import aiohttp

from core.models import OrderBook, OrderBookLevel, Ticker

logger = logging.getLogger(__name__)


class OKXWebSocket:
    """
    OKX V5 public WebSocket for real-time ticker data.

    Channel-based subscriptions:
    - tickers: best bid/ask + 24h stats
    - books5: top 5 order book levels
    """

    def __init__(
        self,
        on_ticker: Callable[[Ticker], None] | None = None,
        on_order_book: Callable[[OrderBook], None] | None = None,
    ):
        self.on_ticker = on_ticker
        self.on_order_book = on_order_book

        self._ws_url = "wss://ws.okx.com:8443/ws/v5/public"
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._running = False
        self._reconnect_count = 0
        self._last_message_time = 0

        self.total_messages = 0
        self.total_reconnects = 0

    @staticmethod
    def _to_inst_id(symbol: str) -> str:
        """BTCUSDT → BTC-USDT"""
        for quote in ["USDT", "USDC", "BTC", "ETH"]:
            if symbol.endswith(quote) and len(symbol) > len(quote):
                return f"{symbol[:-len(quote)]}-{quote}"
        return symbol

    @staticmethod
    def _to_symbol(inst_id: str) -> str:
        """BTC-USDT → BTCUSDT"""
        return inst_id.replace("-", "")

    async def connect(self, symbols: set[str]) -> None:
        """Connect and subscribe to ticker channels."""
        if not symbols:
            return

        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(self._ws_url, heartbeat=20)
        self._running = True
        self._reconnect_count = 0
        self._last_message_time = time_ns() // 1_000_000

        # Subscribe to tickers
        args = [{"channel": "tickers", "instId": self._to_inst_id(s)} for s in symbols]
        await self._ws.send_json({"op": "subscribe", "args": args})

        logger.info("OKX WS connected — %d symbols", len(symbols))

    async def listen(self) -> None:
        if self._ws is None:
            raise RuntimeError("Not connected")

        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._last_message_time = time_ns() // 1_000_000
                    self.total_messages += 1
                    self._process_message(msg.data)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False

    async def listen_with_reconnect(self, symbols: set[str]) -> None:
        while self._running or self._reconnect_count == 0:
            try:
                if self._ws is None or self._ws.closed:
                    await self.connect(symbols)
                await self.listen()
            except Exception as e:
                logger.error("OKX WS error: %s", e)

            if not self._running and self._reconnect_count > 0:
                break

            self._reconnect_count += 1
            self.total_reconnects += 1

            if self._reconnect_count > 10:
                logger.error("OKX WS max reconnects exceeded")
                break

            delay = min(2 ** (self._reconnect_count - 1), 60)
            logger.info("OKX WS reconnecting in %.1fs...", delay)
            await asyncio.sleep(delay)
            await self._cleanup()

    def _process_message(self, raw: str) -> None:
        try:
            data = _loads(raw)
        except (ValueError, TypeError):
            return

        # Subscription confirmation
        if "event" in data:
            return

        arg = data.get("arg", {})
        channel = arg.get("channel", "")

        if channel == "tickers":
            for item in data.get("data", []):
                self._handle_ticker(item)

    def _handle_ticker(self, data: dict) -> None:
        if self.on_ticker is None:
            return

        try:
            symbol = self._to_symbol(data["instId"])
            ticker = Ticker(
                symbol=symbol,
                bid=float(data.get("bidPx", 0)),
                ask=float(data.get("askPx", 0)),
                timestamp_ms=int(data.get("ts", time_ns() // 1_000_000)),
            )
            if ticker.bid > 0 and ticker.ask > 0:
                self.on_ticker(ticker)
        except (KeyError, ValueError) as e:
            logger.debug("OKX ticker parse error: %s", e)

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    @property
    def is_healthy(self) -> bool:
        if not self.is_connected:
            return False
        now = time_ns() // 1_000_000
        return (now - self._last_message_time) < 5000

    async def stop(self) -> None:
        self._running = False
        await self._cleanup()

    async def _cleanup(self) -> None:
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        self._ws = None
        self._session = None

    def stats(self) -> dict:
        return {
            "connected": self.is_connected,
            "healthy": self.is_healthy,
            "total_messages": self.total_messages,
            "total_reconnects": self.total_reconnects,
        }
