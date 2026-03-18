"""Binance WebSocket client for real-time price and order book feeds."""

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

from config.settings import WebSocketConfig
from core.models import OrderBookLevel, OrderBook, Ticker

logger = logging.getLogger(__name__)


class BinanceWebSocket:
    """
    Manages WebSocket connections to Binance for real-time data.

    Subscribes to individual ticker and depth streams for
    symbols in our triangle set.
    """

    def __init__(
        self,
        config: WebSocketConfig | None = None,
        on_ticker: Callable[[Ticker], None] | None = None,
        on_order_book: Callable[[OrderBook], None] | None = None,
        use_book_ticker: bool = False,
    ):
        self.config = config or WebSocketConfig()
        self.on_ticker = on_ticker
        self.on_order_book = on_order_book
        self.use_book_ticker = use_book_ticker

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._running = False
        self._reconnect_count = 0
        self._last_message_time = 0

        # Stats
        self.total_messages = 0
        self.total_reconnects = 0

    async def connect(self, symbols: set[str]) -> None:
        """
        Connect to Binance WebSocket and subscribe to streams.

        Args:
            symbols: Set of trading pair symbols (e.g., {"BTCUSDT", "ETHBTC"}).
        """
        if not symbols:
            logger.warning("No symbols to subscribe to")
            return

        # Build combined stream URL
        streams = []
        for symbol in symbols:
            s = symbol.lower()
            if self.use_book_ticker:
                streams.append(f"{s}@bookTicker")
            else:
                streams.append(f"{s}@ticker")
                streams.append(f"{s}@depth{self.config.order_book_depth}@100ms")

        url = f"{self.config.base_url}/{'/'.join(streams[:1])}"
        # For many streams, use the combined stream endpoint
        if len(streams) > 1:
            stream_param = "/".join(streams)
            url = f"wss://stream.binance.com:9443/stream?streams={stream_param}"

        logger.info("Connecting to Binance WebSocket (%d streams)...", len(streams))

        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(url, heartbeat=20)
            self._running = True
            self._reconnect_count = 0
            self._last_message_time = time_ns() // 1_000_000
            logger.info("WebSocket connected — %d symbols, %d streams", len(symbols), len(streams))
        except Exception as e:
            logger.error("WebSocket connection failed: %s", e)
            await self._cleanup()
            raise

    async def listen(self) -> None:
        """
        Listen for messages. Runs until stopped or disconnected.

        Calls on_ticker/on_order_book callbacks on each message.
        """
        if self._ws is None:
            raise RuntimeError("Not connected. Call connect() first.")

        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._last_message_time = time_ns() // 1_000_000
                    self.total_messages += 1
                    self._process_message(msg.data)

                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error("WebSocket error: %s", self._ws.exception())
                    break

                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                    logger.warning("WebSocket closed by server")
                    break

        except asyncio.CancelledError:
            logger.info("WebSocket listener cancelled")
        except Exception as e:
            logger.error("WebSocket listener error: %s", e)
        finally:
            self._running = False

    async def listen_with_reconnect(self, symbols: set[str]) -> None:
        """
        Listen with automatic reconnection on disconnect.

        Uses exponential backoff on repeated failures.
        """
        while self._running or self._reconnect_count == 0:
            try:
                if self._ws is None or self._ws.closed:
                    await self.connect(symbols)

                await self.listen()

            except Exception as e:
                logger.error("WebSocket error: %s", e)

            if not self._running and self._reconnect_count > 0:
                break

            # Reconnect with backoff
            self._reconnect_count += 1
            self.total_reconnects += 1

            if self._reconnect_count > self.config.reconnect_max_retries:
                logger.error("Max reconnect retries exceeded")
                break

            delay = min(
                self.config.reconnect_base_delay_sec * (2 ** (self._reconnect_count - 1)),
                60.0,
            )
            logger.info("Reconnecting in %.1fs (attempt %d)...", delay, self._reconnect_count)
            await asyncio.sleep(delay)
            await self._cleanup()

    def _process_message(self, raw: str) -> None:
        """Parse and dispatch a WebSocket message."""
        try:
            data = _loads(raw)
        except (ValueError, TypeError):
            logger.warning("Invalid JSON: %s", str(raw)[:100])
            return

        # Combined stream format: {"stream": "...", "data": {...}}
        if "stream" in data:
            stream = data["stream"]
            payload = data.get("data", {})
        else:
            stream = data.get("e", "")
            payload = data

        if "@bookTicker" in str(stream) or payload.get("e") == "bookTicker":
            self._handle_book_ticker(payload)
        elif "@ticker" in str(stream) or payload.get("e") == "24hrTicker":
            self._handle_ticker(payload)
        elif "@depth" in str(stream) or payload.get("e") == "depthUpdate":
            self._handle_depth(payload)

    def _handle_book_ticker(self, data: dict) -> None:
        """Parse bookTicker update (best bid/ask only — fastest stream)."""
        if self.on_ticker is None:
            return

        try:
            ticker = Ticker(
                symbol=data["s"],
                bid=float(data["b"]),
                ask=float(data["a"]),
                timestamp_ms=int(data.get("E", time_ns() // 1_000_000)),
            )
            self.on_ticker(ticker)
        except (KeyError, ValueError) as e:
            logger.debug("Failed to parse bookTicker: %s", e)

    def _handle_ticker(self, data: dict) -> None:
        """Parse 24hr ticker update (fallback for non-bookTicker streams)."""
        if self.on_ticker is None:
            return

        try:
            ticker = Ticker(
                symbol=data["s"],
                bid=float(data["b"]),
                ask=float(data["a"]),
                timestamp_ms=int(data.get("E", time_ns() // 1_000_000)),
            )
            self.on_ticker(ticker)
        except (KeyError, ValueError) as e:
            logger.debug("Failed to parse ticker: %s", e)

    def _handle_depth(self, data: dict) -> None:
        """Parse partial order book depth update."""
        if self.on_order_book is None:
            return

        try:
            # Depth snapshot format
            if "bids" in data:
                bids_raw = data["bids"]
                asks_raw = data["asks"]
                symbol = data.get("s", "")
            # Depth update format
            elif "b" in data:
                bids_raw = data["b"]
                asks_raw = data["a"]
                symbol = data.get("s", "")
            else:
                return

            bids = [
                OrderBookLevel(price=float(b[0]), quantity=float(b[1]))
                for b in bids_raw
            ]
            asks = [
                OrderBookLevel(price=float(a[0]), quantity=float(a[1]))
                for a in asks_raw
            ]

            order_book = OrderBook(
                symbol=symbol,
                bids=bids,
                asks=asks,
                timestamp_ms=int(data.get("E", time_ns() // 1_000_000)),
            )
            self.on_order_book(order_book)
        except (KeyError, ValueError, IndexError) as e:
            logger.debug("Failed to parse depth: %s", e)

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    @property
    def is_healthy(self) -> bool:
        """Check if we've received data recently."""
        if not self.is_connected:
            return False
        now = time_ns() // 1_000_000
        return (now - self._last_message_time) < (self.config.health_timeout_sec * 1000)

    async def stop(self) -> None:
        """Stop listening and close connection."""
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
