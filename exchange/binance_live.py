"""Binance live exchange — full ExchangeBase for real trading."""

import hashlib
import hmac
import logging
import time
from urllib.parse import urlencode

import aiohttp

from config.settings import FeeSchedule
from core.models import Order, OrderBook, OrderBookLevel, OrderSide, OrderStatus, Ticker, TradingPair
from exchange.base import ExchangeBase

logger = logging.getLogger(__name__)

BINANCE_API_URL = "https://api.binance.com"


class BinanceLiveExchange(ExchangeBase):
    """Full Binance ExchangeBase implementation for live trading."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        exchange_id: str = "binance",
    ):
        if not api_key or not api_secret:
            raise ValueError("Binance API key and secret are required for live trading")

        self._api_key = api_key
        self._api_secret = api_secret
        self._exchange_id = exchange_id
        self._fee_schedule = FeeSchedule(
            exchange_id=exchange_id,
            taker_fee=0.00075,  # With BNB discount
            maker_fee=0.00075,
        )
        self._session: aiohttp.ClientSession | None = None
        self._pairs: dict[str, TradingPair] = {}

    @property
    def exchange_id(self) -> str:
        return self._exchange_id

    @property
    def fee_schedule(self) -> FeeSchedule:
        return self._fee_schedule

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"X-MBX-APIKEY": self._api_key}
            )
        return self._session

    def _sign(self, params: dict) -> str:
        """Generate HMAC-SHA256 signature for Binance."""
        query = urlencode(params)
        signature = hmac.new(
            self._api_secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        return signature

    async def _public_get(self, endpoint: str, params: dict | None = None) -> dict | list:
        session = await self._get_session()
        url = f"{BINANCE_API_URL}{endpoint}"
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Binance error ({resp.status}): {text}")
            return await resp.json()

    async def _signed_get(self, endpoint: str, params: dict | None = None) -> dict | list:
        session = await self._get_session()
        params = params or {}
        params["timestamp"] = str(int(time.time() * 1000))
        params["recvWindow"] = "5000"
        params["signature"] = self._sign(params)
        url = f"{BINANCE_API_URL}{endpoint}"
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Binance error ({resp.status}): {text}")
            return await resp.json()

    async def _signed_post(self, endpoint: str, params: dict) -> dict:
        session = await self._get_session()
        params["timestamp"] = str(int(time.time() * 1000))
        params["recvWindow"] = "5000"
        params["signature"] = self._sign(params)
        url = f"{BINANCE_API_URL}{endpoint}"
        async with session.post(url, params=params) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise RuntimeError(f"Binance order error ({resp.status}): {data}")
            return data

    async def get_all_pairs(self) -> list[TradingPair]:
        info = await self._public_get("/api/v3/exchangeInfo")
        pairs = []
        for sym in info.get("symbols", []):
            if sym.get("status") != "TRADING":
                continue
            min_qty = step_size = min_notional = 0.0
            for f in sym.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    min_qty = float(f.get("minQty", 0))
                    step_size = float(f.get("stepSize", 0))
                elif f["filterType"] == "NOTIONAL":
                    min_notional = float(f.get("minNotional", 0))

            pair = TradingPair(
                symbol=sym["symbol"],
                base_asset=sym["baseAsset"],
                quote_asset=sym["quoteAsset"],
                min_qty=min_qty,
                step_size=step_size,
                min_notional=min_notional,
            )
            pairs.append(pair)
            self._pairs[pair.symbol] = pair
        return pairs

    async def get_ticker(self, symbol: str) -> Ticker:
        data = await self._public_get("/api/v3/ticker/bookTicker", {"symbol": symbol})
        return Ticker(
            symbol=data["symbol"],
            bid=float(data["bidPrice"]),
            ask=float(data["askPrice"]),
            timestamp_ms=int(time.time() * 1000),
        )

    async def get_order_book(self, symbol: str, depth: int = 5) -> OrderBook:
        data = await self._public_get("/api/v3/depth", {"symbol": symbol, "limit": depth})
        return OrderBook(
            symbol=symbol,
            bids=[OrderBookLevel(float(b[0]), float(b[1])) for b in data.get("bids", [])],
            asks=[OrderBookLevel(float(a[0]), float(a[1])) for a in data.get("asks", [])],
            timestamp_ms=int(time.time() * 1000),
        )

    async def get_balance(self, asset: str) -> float:
        data = await self._signed_get("/api/v3/account")
        for bal in data.get("balances", []):
            if bal["asset"] == asset:
                return float(bal["free"])
        return 0.0

    async def get_all_balances(self) -> dict[str, float]:
        data = await self._signed_get("/api/v3/account")
        return {
            bal["asset"]: float(bal["free"])
            for bal in data.get("balances", [])
            if float(bal["free"]) > 0
        }

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        price: float | None = None,
    ) -> Order:
        """Place a real order on Binance."""
        # Quantize quantity to step size
        pair = self._pairs.get(symbol)
        if pair and pair.step_size > 0:
            precision = len(str(pair.step_size).rstrip('0').split('.')[-1])
            quantity = round(quantity, precision)

        params = {
            "symbol": symbol,
            "side": "BUY" if side == OrderSide.BUY else "SELL",
            "type": "LIMIT" if price else "MARKET",
            "quantity": f"{quantity}",
        }

        if price:
            params["price"] = f"{price}"
            params["timeInForce"] = "GTC"

        logger.info(
            "BINANCE ORDER: %s %s %s qty=%.8f%s",
            params["type"], params["side"], symbol, quantity,
            f" @ {price}" if price else "",
        )

        try:
            data = await self._signed_post("/api/v3/order", params)

            # Parse fill info
            fills = data.get("fills", [])
            total_qty = float(data.get("executedQty", quantity))
            total_cost = sum(float(f["price"]) * float(f["qty"]) for f in fills) if fills else 0
            avg_price = total_cost / total_qty if total_qty > 0 and fills else float(data.get("price", 0))
            total_fee = sum(float(f.get("commission", 0)) for f in fills)

            status_map = {
                "FILLED": OrderStatus.FILLED,
                "PARTIALLY_FILLED": OrderStatus.PARTIAL,
                "NEW": OrderStatus.PENDING,
                "CANCELED": OrderStatus.CANCELLED,
                "REJECTED": OrderStatus.FAILED,
                "EXPIRED": OrderStatus.CANCELLED,
            }

            return Order(
                id=str(data.get("orderId", "")),
                symbol=symbol,
                side=side,
                quantity=total_qty,
                expected_price=price or avg_price,
                actual_price=avg_price,
                fee=total_fee,
                status=status_map.get(data.get("status", ""), OrderStatus.FAILED),
                timestamp_ms=int(data.get("transactTime", time.time() * 1000)),
            )

        except Exception as e:
            logger.error("Binance order FAILED: %s", e)
            return Order(
                symbol=symbol,
                side=side,
                quantity=quantity,
                status=OrderStatus.FAILED,
                timestamp_ms=int(time.time() * 1000),
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
