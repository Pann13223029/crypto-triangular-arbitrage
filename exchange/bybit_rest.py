"""Bybit V5 REST API client."""

import hashlib
import hmac
import logging
import time

import aiohttp

from config.settings import FeeSchedule
from core.models import Order, OrderBook, OrderBookLevel, OrderSide, OrderStatus, Ticker, TradingPair
from exchange.base import ExchangeBase

logger = logging.getLogger(__name__)

BYBIT_API_URL = "https://api.bybit.com"


class BybitExchange(ExchangeBase):
    """Bybit V5 API implementation of ExchangeBase."""

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        exchange_id: str = "bybit",
    ):
        self._api_key = api_key
        self._api_secret = api_secret
        self._exchange_id = exchange_id
        self._fee_schedule = FeeSchedule(
            exchange_id=exchange_id,
            taker_fee=0.001,  # 0.10% default, lower with VIP
            maker_fee=0.001,
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

    def _sign(self, params: dict) -> dict:
        """Generate Bybit V5 HMAC-SHA256 signature."""
        timestamp = str(int(time.time() * 1000))
        recv_window = "5000"

        param_str = timestamp + self._api_key + recv_window
        if params:
            param_str += "&".join(f"{k}={v}" for k, v in sorted(params.items()))

        signature = hmac.new(
            self._api_secret.encode(), param_str.encode(), hashlib.sha256
        ).hexdigest()

        return {
            "X-BAPI-API-KEY": self._api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
        }

    async def _public_get(self, endpoint: str, params: dict | None = None) -> dict:
        session = await self._get_session()
        url = f"{BYBIT_API_URL}{endpoint}"
        async with session.get(url, params=params) as resp:
            data = await resp.json()
            if data.get("retCode") != 0:
                raise RuntimeError(f"Bybit API error: {data.get('retMsg', 'unknown')}")
            return data.get("result", {})

    async def _private_get(self, endpoint: str, params: dict | None = None) -> dict:
        session = await self._get_session()
        url = f"{BYBIT_API_URL}{endpoint}"
        headers = self._sign(params or {})
        async with session.get(url, params=params, headers=headers) as resp:
            data = await resp.json()
            if data.get("retCode") != 0:
                raise RuntimeError(f"Bybit API error: {data.get('retMsg', 'unknown')}")
            return data.get("result", {})

    async def _private_post(self, endpoint: str, body: dict) -> dict:
        session = await self._get_session()
        url = f"{BYBIT_API_URL}{endpoint}"
        headers = self._sign(body)
        headers["Content-Type"] = "application/json"
        async with session.post(url, json=body, headers=headers) as resp:
            data = await resp.json()
            if data.get("retCode") != 0:
                raise RuntimeError(f"Bybit API error: {data.get('retMsg', 'unknown')}")
            return data.get("result", {})

    async def get_all_pairs(self) -> list[TradingPair]:
        result = await self._public_get("/v5/market/instruments-info", {
            "category": "spot",
            "limit": "1000",
        })
        pairs = []
        for item in result.get("list", []):
            if item.get("status") != "Trading":
                continue

            lot_filter = item.get("lotSizeFilter", {})
            pairs.append(TradingPair(
                symbol=item["symbol"],
                base_asset=item["baseCoin"],
                quote_asset=item["quoteCoin"],
                min_qty=float(lot_filter.get("minOrderQty", 0)),
                step_size=float(lot_filter.get("basePrecision", 0)),
                min_notional=float(lot_filter.get("minOrderAmt", 0)),
            ))

        logger.info("Bybit: loaded %d trading pairs", len(pairs))
        return pairs

    async def get_ticker(self, symbol: str) -> Ticker:
        result = await self._public_get("/v5/market/tickers", {
            "category": "spot",
            "symbol": symbol,
        })
        items = result.get("list", [])
        if not items:
            raise ValueError(f"No ticker data for {symbol}")

        t = items[0]
        return Ticker(
            symbol=t["symbol"],
            bid=float(t.get("bid1Price", 0)),
            ask=float(t.get("ask1Price", 0)),
            timestamp_ms=int(time.time() * 1000),
        )

    async def get_order_book(self, symbol: str, depth: int = 5) -> OrderBook:
        result = await self._public_get("/v5/market/orderbook", {
            "category": "spot",
            "symbol": symbol,
            "limit": str(depth),
        })

        bids = [OrderBookLevel(float(b[0]), float(b[1])) for b in result.get("b", [])]
        asks = [OrderBookLevel(float(a[0]), float(a[1])) for a in result.get("a", [])]

        return OrderBook(
            symbol=symbol,
            bids=bids,
            asks=asks,
            timestamp_ms=int(result.get("ts", time.time() * 1000)),
        )

    async def get_balance(self, asset: str) -> float:
        result = await self._private_get("/v5/account/wallet-balance", {
            "accountType": "UNIFIED",
        })
        for account in result.get("list", []):
            for coin in account.get("coin", []):
                if coin["coin"] == asset:
                    return float(coin.get("availableToWithdraw", 0))
        return 0.0

    async def get_all_balances(self) -> dict[str, float]:
        result = await self._private_get("/v5/account/wallet-balance", {
            "accountType": "UNIFIED",
        })
        balances = {}
        for account in result.get("list", []):
            for coin in account.get("coin", []):
                available = float(coin.get("availableToWithdraw", 0))
                if available > 0:
                    balances[coin["coin"]] = available
        return balances

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        price: float | None = None,
    ) -> Order:
        body = {
            "category": "spot",
            "symbol": symbol,
            "side": "Buy" if side == OrderSide.BUY else "Sell",
            "orderType": "Limit" if price else "Market",
            "qty": str(quantity),
        }
        if price:
            body["price"] = str(price)

        try:
            result = await self._private_post("/v5/order/create", body)
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
            logger.error("Bybit order failed: %s", e)
            return Order(
                symbol=symbol, side=side, quantity=quantity,
                status=OrderStatus.FAILED,
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
