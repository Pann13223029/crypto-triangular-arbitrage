"""KuCoin WebSocket client for real-time price feeds."""

import asyncio
import logging
import uuid
from time import time_ns
from typing import Callable

try:
    import orjson
    _loads = orjson.loads
except ImportError:
    import json
    _loads = json.loads

import aiohttp

from core.models import Ticker

logger = logging.getLogger(__name__)


class KuCoinWebSocket:
    """
    KuCoin WebSocket for real-time ticker data.

    KuCoin requires a connection token from REST API before
    connecting to WebSocket. Uses /market/ticker topic.
    """

    def __init__(
        self,
        on_ticker: Callable[[Ticker], None] | None = None,
    ):
        self.on_ticker = on_ticker

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._running = False
        self._reconnect_count = 0
        self._last_message_time = 0
        self._ping_task = None

        self.total_messages = 0
        self.total_reconnects = 0

    @staticmethod
    def _to_kucoin_symbol(symbol: str) -> str:
        for quote in ["USDT", "USDC", "BTC", "ETH"]:
            if symbol.endswith(quote) and len(symbol) > len(quote):
                return f"{symbol[:-len(quote)]}-{quote}"
        return symbol

    @staticmethod
    def _to_symbol(kc_symbol: str) -> str:
        return kc_symbol.replace("-", "")

    async def _get_ws_endpoint(self) -> tuple[str, int]:
        """Get WebSocket endpoint + ping interval from KuCoin."""
        session = await self._ensure_session()
        url = "https://api.kucoin.com/api/v1/bullet-public"
        async with session.post(url) as resp:
            data = await resp.json()
            if data.get("code") != "200000":
                raise RuntimeError(f"KuCoin WS token error: {data}")

            token_data = data["data"]
            token = token_data["token"]
            servers = token_data.get("instanceServers", [])
            if not servers:
                raise RuntimeError("No KuCoin WS servers available")

            endpoint = servers[0]["endpoint"]
            ping_interval = servers[0].get("pingInterval", 18000)  # ms

            ws_url = f"{endpoint}?token={token}&connectId={uuid.uuid4()}"
            return ws_url, ping_interval

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def connect(self, symbols: set[str]) -> None:
        """Connect and subscribe to ticker streams."""
        if not symbols:
            return

        await self._ensure_session()
        ws_url, ping_interval_ms = await self._get_ws_endpoint()

        self._ws = await self._session.ws_connect(ws_url)
        self._running = True
        self._reconnect_count = 0
        self._last_message_time = time_ns() // 1_000_000

        # Subscribe to tickers for all symbols
        kc_symbols = [self._to_kucoin_symbol(s) for s in symbols]
        topic = ",".join(kc_symbols)

        await self._ws.send_json({
            "id": str(uuid.uuid4()),
            "type": "subscribe",
            "topic": f"/market/ticker:{topic}",
            "privateChannel": False,
            "response": True,
        })

        # Start ping task (KuCoin requires periodic pings)
        self._ping_task = asyncio.create_task(
            self._ping_loop(ping_interval_ms / 1000)
        )

        logger.info("KuCoin WS connected — %d symbols", len(symbols))

    async def _ping_loop(self, interval_sec: float) -> None:
        """Send periodic pings to keep connection alive."""
        try:
            while self._running and self._ws and not self._ws.closed:
                await asyncio.sleep(interval_sec)
                await self._ws.send_json({
                    "id": str(uuid.uuid4()),
                    "type": "ping",
                })
        except (asyncio.CancelledError, Exception):
            pass

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
                logger.error("KuCoin WS error: %s", e)

            if not self._running and self._reconnect_count > 0:
                break

            self._reconnect_count += 1
            self.total_reconnects += 1

            if self._reconnect_count > 10:
                logger.error("KuCoin WS max reconnects exceeded")
                break

            delay = min(2 ** (self._reconnect_count - 1), 60)
            logger.info("KuCoin WS reconnecting in %.1fs...", delay)
            await asyncio.sleep(delay)
            await self._cleanup()

    def _process_message(self, raw: str) -> None:
        try:
            data = _loads(raw)
        except (ValueError, TypeError):
            return

        msg_type = data.get("type", "")

        # Ticker update
        if msg_type == "message" and data.get("subject") == "trade.ticker":
            self._handle_ticker(data.get("data", {}), data.get("topic", ""))

    def _handle_ticker(self, data: dict, topic: str) -> None:
        if self.on_ticker is None:
            return

        try:
            # Extract symbol from topic: /market/ticker:BTC-USDT
            kc_symbol = topic.split(":")[-1] if ":" in topic else ""
            symbol = self._to_symbol(kc_symbol)

            ticker = Ticker(
                symbol=symbol,
                bid=float(data.get("bestBid", 0)),
                ask=float(data.get("bestAsk", 0)),
                timestamp_ms=int(data.get("time", time_ns() // 1_000_000)),
            )
            if ticker.bid > 0 and ticker.ask > 0:
                self.on_ticker(ticker)
        except (KeyError, ValueError) as e:
            logger.debug("KuCoin ticker parse error: %s", e)

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
        if self._ping_task:
            self._ping_task.cancel()
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
