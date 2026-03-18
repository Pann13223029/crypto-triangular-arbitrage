"""OKX REST API client."""

import base64
import hashlib
import hmac
import logging
import time
from datetime import datetime, timezone

import aiohttp

from config.settings import FeeSchedule
from core.models import Order, OrderBook, OrderBookLevel, OrderSide, OrderStatus, Ticker, TradingPair
from exchange.base import ExchangeBase

logger = logging.getLogger(__name__)

OKX_API_URL = "https://www.okx.com"


class OKXExchange(ExchangeBase):
    """OKX V5 API implementation of ExchangeBase."""

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        passphrase: str = "",
        exchange_id: str = "okx",
    ):
        self._api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase
        self._exchange_id = exchange_id
        self._fee_schedule = FeeSchedule(
            exchange_id=exchange_id,
            taker_fee=0.001,  # 0.10% default
            maker_fee=0.0008,  # 0.08% default
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

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> dict:
        """Generate OKX HMAC-SHA256 signature."""
        message = timestamp + method + path + body
        signature = base64.b64encode(
            hmac.new(
                self._api_secret.encode(), message.encode(), hashlib.sha256
            ).digest()
        ).decode()

        return {
            "OK-ACCESS-KEY": self._api_key,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self._passphrase,
            "Content-Type": "application/json",
        }

    def _iso_timestamp(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    async def _public_get(self, endpoint: str, params: dict | None = None) -> list:
        session = await self._get_session()
        url = f"{OKX_API_URL}{endpoint}"
        async with session.get(url, params=params) as resp:
            data = await resp.json()
            if data.get("code") != "0":
                raise RuntimeError(f"OKX API error: {data.get('msg', 'unknown')}")
            return data.get("data", [])

    async def _private_get(self, endpoint: str, params: dict | None = None) -> list:
        session = await self._get_session()
        url = f"{OKX_API_URL}{endpoint}"
        ts = self._iso_timestamp()
        path = endpoint
        if params:
            path += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        headers = self._sign(ts, "GET", path)
        async with session.get(url, params=params, headers=headers) as resp:
            data = await resp.json()
            if data.get("code") != "0":
                raise RuntimeError(f"OKX API error: {data.get('msg', 'unknown')}")
            return data.get("data", [])

    async def _private_post(self, endpoint: str, body: dict) -> list:
        session = await self._get_session()
        url = f"{OKX_API_URL}{endpoint}"
        ts = self._iso_timestamp()

        import json
        body_str = json.dumps(body)
        headers = self._sign(ts, "POST", endpoint, body_str)
        async with session.post(url, data=body_str, headers=headers) as resp:
            data = await resp.json()
            if data.get("code") != "0":
                raise RuntimeError(f"OKX API error: {data.get('msg', 'unknown')}")
            return data.get("data", [])

    @staticmethod
    def _to_symbol(inst_id: str) -> str:
        """Convert OKX instId (BTC-USDT) to standard symbol (BTCUSDT)."""
        return inst_id.replace("-", "")

    @staticmethod
    def _to_inst_id(symbol: str) -> str:
        """Convert standard symbol (BTCUSDT) to OKX instId (BTC-USDT).
        Heuristic: split before USDT/USDC/BTC/ETH suffix."""
        for quote in ["USDT", "USDC", "BTC", "ETH"]:
            if symbol.endswith(quote) and len(symbol) > len(quote):
                base = symbol[:-len(quote)]
                return f"{base}-{quote}"
        return symbol

    async def get_all_pairs(self) -> list[TradingPair]:
        result = await self._public_get("/api/v5/public/instruments", {
            "instType": "SPOT",
        })
        pairs = []
        for item in result:
            if item.get("state") != "live":
                continue

            symbol = self._to_symbol(item["instId"])
            pairs.append(TradingPair(
                symbol=symbol,
                base_asset=item["baseCcy"],
                quote_asset=item["quoteCcy"],
                min_qty=float(item.get("minSz", 0)),
                step_size=float(item.get("lotSz", 0)),
                min_notional=0.0,
            ))

        logger.info("OKX: loaded %d trading pairs", len(pairs))
        return pairs

    async def get_ticker(self, symbol: str) -> Ticker:
        inst_id = self._to_inst_id(symbol)
        result = await self._public_get("/api/v5/market/ticker", {
            "instId": inst_id,
        })
        if not result:
            raise ValueError(f"No ticker data for {symbol}")

        t = result[0]
        return Ticker(
            symbol=symbol,
            bid=float(t.get("bidPx", 0)),
            ask=float(t.get("askPx", 0)),
            timestamp_ms=int(t.get("ts", time.time() * 1000)),
        )

    async def get_order_book(self, symbol: str, depth: int = 5) -> OrderBook:
        inst_id = self._to_inst_id(symbol)
        result = await self._public_get("/api/v5/market/books", {
            "instId": inst_id,
            "sz": str(depth),
        })
        if not result:
            raise ValueError(f"No order book for {symbol}")

        data = result[0]
        bids = [OrderBookLevel(float(b[0]), float(b[1])) for b in data.get("bids", [])]
        asks = [OrderBookLevel(float(a[0]), float(a[1])) for a in data.get("asks", [])]

        return OrderBook(
            symbol=symbol,
            bids=bids,
            asks=asks,
            timestamp_ms=int(data.get("ts", time.time() * 1000)),
        )

    async def get_balance(self, asset: str) -> float:
        result = await self._private_get("/api/v5/account/balance", {
            "ccy": asset,
        })
        for account in result:
            for detail in account.get("details", []):
                if detail["ccy"] == asset:
                    return float(detail.get("availBal", 0))
        return 0.0

    async def get_all_balances(self) -> dict[str, float]:
        result = await self._private_get("/api/v5/account/balance")
        balances = {}
        for account in result:
            for detail in account.get("details", []):
                available = float(detail.get("availBal", 0))
                if available > 0:
                    balances[detail["ccy"]] = available
        return balances

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        price: float | None = None,
    ) -> Order:
        inst_id = self._to_inst_id(symbol)
        body = {
            "instId": inst_id,
            "tdMode": "cash",
            "side": "buy" if side == OrderSide.BUY else "sell",
            "ordType": "limit" if price else "market",
            "sz": str(quantity),
        }
        if price:
            body["px"] = str(price)

        try:
            result = await self._private_post("/api/v5/trade/order", body)
            order_id = result[0].get("ordId", "") if result else ""
            return Order(
                id=order_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                expected_price=price or 0,
                status=OrderStatus.FILLED,
                timestamp_ms=int(time.time() * 1000),
            )
        except Exception as e:
            logger.error("OKX order failed: %s", e)
            return Order(
                symbol=symbol, side=side, quantity=quantity,
                status=OrderStatus.FAILED,
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
