"""KuCoin REST API client."""

import base64
import hashlib
import hmac
import logging
import time

import aiohttp

from config.settings import FeeSchedule
from core.models import Order, OrderBook, OrderBookLevel, OrderSide, OrderStatus, Ticker, TradingPair
from exchange.base import ExchangeBase

logger = logging.getLogger(__name__)

KUCOIN_API_URL = "https://api.kucoin.com"


class KuCoinExchange(ExchangeBase):
    """KuCoin V2 API implementation of ExchangeBase."""

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        passphrase: str = "",
        exchange_id: str = "kucoin",
    ):
        self._api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase
        self._exchange_id = exchange_id
        self._fee_schedule = FeeSchedule(
            exchange_id=exchange_id,
            taker_fee=0.001,  # 0.10% default
            maker_fee=0.001,  # 0.10% default
        )
        self._session: aiohttp.ClientSession | None = None

    @property
    def exchange_id(self) -> str:
        return self._exchange_id

    @property
    def fee_schedule(self) -> FeeSchedule:
        return self._fee_schedule

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _sign(self, timestamp: str, method: str, endpoint: str, body: str = "") -> dict:
        """Generate KuCoin HMAC-SHA256 signature."""
        str_to_sign = timestamp + method + endpoint + body
        signature = base64.b64encode(
            hmac.new(
                self._api_secret.encode(), str_to_sign.encode(), hashlib.sha256
            ).digest()
        ).decode()

        # KuCoin also signs the passphrase
        passphrase_sign = base64.b64encode(
            hmac.new(
                self._api_secret.encode(), self._passphrase.encode(), hashlib.sha256
            ).digest()
        ).decode()

        return {
            "KC-API-KEY": self._api_key,
            "KC-API-SIGN": signature,
            "KC-API-TIMESTAMP": timestamp,
            "KC-API-PASSPHRASE": passphrase_sign,
            "KC-API-KEY-VERSION": "2",
            "Content-Type": "application/json",
        }

    async def _public_get(self, endpoint: str, params: dict | None = None) -> dict:
        session = await self._get_session()
        url = f"{KUCOIN_API_URL}{endpoint}"
        async with session.get(url, params=params) as resp:
            data = await resp.json()
            if data.get("code") != "200000":
                raise RuntimeError(f"KuCoin API error: {data.get('msg', data)}")
            return data.get("data", {})

    async def _private_get(self, endpoint: str, params: dict | None = None) -> dict:
        session = await self._get_session()
        ts = str(int(time.time() * 1000))
        path = endpoint
        if params:
            path += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        headers = self._sign(ts, "GET", path)
        url = f"{KUCOIN_API_URL}{endpoint}"
        async with session.get(url, params=params, headers=headers) as resp:
            data = await resp.json()
            if data.get("code") != "200000":
                raise RuntimeError(f"KuCoin API error: {data.get('msg', data)}")
            return data.get("data", {})

    async def _private_post(self, endpoint: str, body: dict) -> dict:
        session = await self._get_session()
        ts = str(int(time.time() * 1000))

        import json
        body_str = json.dumps(body)
        headers = self._sign(ts, "POST", endpoint, body_str)
        url = f"{KUCOIN_API_URL}{endpoint}"
        async with session.post(url, data=body_str, headers=headers) as resp:
            data = await resp.json()
            if data.get("code") != "200000":
                raise RuntimeError(f"KuCoin API error: {data.get('msg', data)}")
            return data.get("data", {})

    @staticmethod
    def _to_symbol(kucoin_symbol: str) -> str:
        """Convert KuCoin symbol (BTC-USDT) to standard (BTCUSDT)."""
        return kucoin_symbol.replace("-", "")

    @staticmethod
    def _to_kucoin_symbol(symbol: str) -> str:
        """Convert standard symbol (BTCUSDT) to KuCoin (BTC-USDT)."""
        for quote in ["USDT", "USDC", "BTC", "ETH"]:
            if symbol.endswith(quote) and len(symbol) > len(quote):
                return f"{symbol[:-len(quote)]}-{quote}"
        return symbol

    async def get_all_pairs(self) -> list[TradingPair]:
        result = await self._public_get("/api/v2/symbols")
        pairs = []

        items = result if isinstance(result, list) else result.get("items", result)
        if isinstance(items, dict):
            items = items.get("items", [])

        for item in items:
            if not item.get("enableTrading", False):
                continue

            symbol = self._to_symbol(item["symbol"])
            pairs.append(TradingPair(
                symbol=symbol,
                base_asset=item["baseCurrency"],
                quote_asset=item["quoteCurrency"],
                min_qty=float(item.get("baseMinSize", 0)),
                step_size=float(item.get("baseIncrement", 0)),
                min_notional=float(item.get("quoteMinSize", 0)),
            ))

        logger.info("KuCoin: loaded %d trading pairs", len(pairs))
        return pairs

    async def get_ticker(self, symbol: str) -> Ticker:
        kc_symbol = self._to_kucoin_symbol(symbol)
        result = await self._public_get(f"/api/v1/market/orderbook/level1", {
            "symbol": kc_symbol,
        })
        return Ticker(
            symbol=symbol,
            bid=float(result.get("bestBid", 0)),
            ask=float(result.get("bestAsk", 0)),
            timestamp_ms=int(result.get("time", time.time() * 1000)),
        )

    async def get_order_book(self, symbol: str, depth: int = 5) -> OrderBook:
        kc_symbol = self._to_kucoin_symbol(symbol)
        # KuCoin offers depth20 and depth100
        result = await self._public_get(f"/api/v1/market/orderbook/level2_20", {
            "symbol": kc_symbol,
        })
        bids = [OrderBookLevel(float(b[0]), float(b[1])) for b in result.get("bids", [])[:depth]]
        asks = [OrderBookLevel(float(a[0]), float(a[1])) for a in result.get("asks", [])[:depth]]

        return OrderBook(
            symbol=symbol,
            bids=bids,
            asks=asks,
            timestamp_ms=int(result.get("time", time.time() * 1000)),
        )

    async def get_balance(self, asset: str) -> float:
        result = await self._private_get("/api/v1/accounts", {
            "currency": asset,
            "type": "trade",
        })
        items = result if isinstance(result, list) else []
        for account in items:
            if account.get("currency") == asset and account.get("type") == "trade":
                return float(account.get("available", 0))
        return 0.0

    async def get_all_balances(self) -> dict[str, float]:
        result = await self._private_get("/api/v1/accounts", {"type": "trade"})
        items = result if isinstance(result, list) else []
        return {
            acc["currency"]: float(acc["available"])
            for acc in items
            if float(acc.get("available", 0)) > 0
        }

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        price: float | None = None,
    ) -> Order:
        import uuid
        kc_symbol = self._to_kucoin_symbol(symbol)

        body = {
            "clientOid": str(uuid.uuid4()),
            "side": "buy" if side == OrderSide.BUY else "sell",
            "symbol": kc_symbol,
            "type": "limit" if price else "market",
            "size": str(quantity),
        }
        if price:
            body["price"] = str(price)

        try:
            result = await self._private_post("/api/v1/orders", body)
            return Order(
                id=result.get("orderId", ""),
                symbol=symbol,
                side=side,
                quantity=quantity,
                expected_price=price or 0,
                status=OrderStatus.FILLED,
                timestamp_ms=int(time.time() * 1000),
            )
        except Exception as e:
            logger.error("KuCoin order failed: %s", e)
            return Order(
                symbol=symbol, side=side, quantity=quantity,
                status=OrderStatus.FAILED,
            )

    async def get_ws_token(self) -> dict:
        """Get WebSocket connection token (public endpoint)."""
        session = await self._get_session()
        url = f"{KUCOIN_API_URL}/api/v1/bullet-public"
        async with session.post(url) as resp:
            data = await resp.json()
            if data.get("code") != "200000":
                raise RuntimeError(f"KuCoin WS token error: {data}")
            return data.get("data", {})

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
