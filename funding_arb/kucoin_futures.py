"""KuCoin Futures API client for funding rate arbitrage."""

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

KUCOIN_FUTURES_URL = "https://api-futures.kucoin.com"


class KuCoinFutures:
    """
    KuCoin Futures API for funding rate arb.

    Handles: place orders, get positions, account balance,
    set leverage/margin mode, funding history.
    """

    def __init__(self, api_key: str, api_secret: str, passphrase: str):
        self._api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _sign(self, timestamp: str, method: str, endpoint: str, body: str = "") -> dict:
        str_to_sign = timestamp + method + endpoint + body
        signature = base64.b64encode(
            hmac.new(
                self._api_secret.encode(), str_to_sign.encode(), hashlib.sha256
            ).digest()
        ).decode()
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

    async def _public_get(self, endpoint: str, params: dict | None = None) -> Any:
        session = await self._get_session()
        url = f"{KUCOIN_FUTURES_URL}{endpoint}"
        async with session.get(url, params=params) as resp:
            data = await resp.json()
            if data.get("code") != "200000":
                raise RuntimeError(f"KuCoin Futures: {data.get('msg', data)}")
            return data.get("data")

    async def _private_get(self, endpoint: str, params: dict | None = None) -> Any:
        session = await self._get_session()
        ts = str(int(time.time() * 1000))
        path = endpoint
        if params:
            path += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        headers = self._sign(ts, "GET", path)
        url = f"{KUCOIN_FUTURES_URL}{endpoint}"
        async with session.get(url, params=params, headers=headers) as resp:
            data = await resp.json()
            if data.get("code") != "200000":
                raise RuntimeError(f"KuCoin Futures: {data.get('msg', data)}")
            return data.get("data")

    async def _private_post(self, endpoint: str, body: dict) -> Any:
        session = await self._get_session()
        ts = str(int(time.time() * 1000))
        body_str = json.dumps(body)
        headers = self._sign(ts, "POST", endpoint, body_str)
        url = f"{KUCOIN_FUTURES_URL}{endpoint}"
        async with session.post(url, data=body_str, headers=headers) as resp:
            data = await resp.json()
            if data.get("code") != "200000":
                raise RuntimeError(f"KuCoin Futures: {data.get('msg', data)}")
            return data.get("data")

    async def _private_delete(self, endpoint: str, params: dict | None = None) -> Any:
        session = await self._get_session()
        ts = str(int(time.time() * 1000))
        path = endpoint
        if params:
            path += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        headers = self._sign(ts, "DELETE", path)
        url = f"{KUCOIN_FUTURES_URL}{endpoint}"
        async with session.delete(url, params=params, headers=headers) as resp:
            data = await resp.json()
            if data.get("code") != "200000":
                raise RuntimeError(f"KuCoin Futures: {data.get('msg', data)}")
            return data.get("data")

    # --- Account ---

    async def get_account_balance(self) -> dict:
        """Get futures account balance (USDT)."""
        return await self._private_get("/api/v1/account-overview", {"currency": "USDT"})

    async def transfer_to_futures(self, amount: float) -> dict:
        """Transfer USDT from main/trade account to futures account."""
        return await self._private_post("/api/v3/transfer-out", {
            "currency": "USDT",
            "amount": amount,
            "recAccountType": "FUTURES",
        })

    # --- Contract Info ---

    async def get_contract(self, symbol: str) -> dict:
        """Get contract details."""
        return await self._public_get(f"/api/v1/contracts/{symbol}")

    async def get_funding_rate(self, symbol: str) -> dict:
        """Get current funding rate for a symbol."""
        return await self._public_get(f"/api/v1/funding-rate/{symbol}/current")

    # --- Position ---

    async def get_position(self, symbol: str) -> dict:
        """Get current position for a symbol."""
        return await self._private_get("/api/v1/position", {"symbol": symbol})

    async def get_all_positions(self) -> list:
        """Get all open positions."""
        return await self._private_get("/api/v1/positions")

    # --- Orders ---

    async def place_order(
        self,
        symbol: str,
        side: str,  # "buy" or "sell"
        size: int,  # Number of lots
        leverage: int = 2,
        order_type: str = "market",
        price: float | None = None,
        stop_price: float | None = None,
        stop_type: str | None = None,  # "down" or "up"
        client_oid: str | None = None,
    ) -> dict:
        """
        Place a futures order.

        For funding arb short: side="sell", opens short position
        """
        import uuid

        body = {
            "clientOid": client_oid or str(uuid.uuid4()),
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "size": size,  # Number of lots
            "leverage": leverage,
        }

        if order_type == "limit" and price is not None:
            body["price"] = str(price)
            body["timeInForce"] = "GTC"

        logger.info(
            "FUTURES ORDER: %s %s %s %d lots (leverage: %dx)%s",
            order_type.upper(), side.upper(), symbol, size, leverage,
            f" @ {price}" if price else "",
        )

        result = await self._private_post("/api/v1/orders", body)
        logger.info("Order placed: %s", result.get("orderId", ""))
        return result

    async def place_stop_order(
        self,
        symbol: str,
        side: str,
        size: int,
        stop_price: float,
        stop_type: str = "down",  # "down" = trigger when price falls below
        leverage: int = 2,
    ) -> dict:
        """Place a stop-loss order on exchange (not in bot)."""
        import uuid

        body = {
            "clientOid": str(uuid.uuid4()),
            "symbol": symbol,
            "side": side,
            "type": "market",
            "stop": stop_type,  # "up" for short stop-loss (trigger when price rises)
            "stopPriceType": "TP",  # Trade price
            "stopPrice": str(stop_price),
            "size": size,
            "leverage": leverage,
            "reduceOnly": True,
        }

        logger.info(
            "STOP ORDER: %s %s %s %d lots, trigger @ %.4f (%s)",
            "MARKET", side.upper(), symbol, size, stop_price, stop_type,
        )

        result = await self._private_post("/api/v1/orders", body)
        return result

    async def cancel_order(self, order_id: str) -> dict:
        """Cancel a specific order."""
        return await self._private_delete(f"/api/v1/orders/{order_id}")

    async def cancel_all_orders(self, symbol: str | None = None) -> dict:
        """Cancel all orders, optionally filtered by symbol."""
        params = {}
        if symbol:
            params["symbol"] = symbol
        return await self._private_delete("/api/v1/orders", params)

    # --- Margin Mode ---

    async def get_margin_mode(self, symbol: str) -> str:
        """Get current margin mode for a symbol."""
        try:
            result = await self._private_get("/api/v2/getMarginMode", {"symbol": symbol})
            return result.get("marginMode", "CROSS")
        except Exception:
            return "CROSS"  # Default

    async def set_isolated_margin(self, symbol: str) -> dict:
        """Switch to isolated margin mode."""
        return await self._private_post("/api/v2/position/changeMarginMode", {
            "symbol": symbol,
            "marginMode": "ISOLATED",
        })

    # --- Funding History ---

    async def get_funding_history(self, symbol: str) -> list:
        """Get funding fee history for a symbol."""
        result = await self._private_get("/api/v1/funding-history", {
            "symbol": symbol,
        })
        return result.get("dataList", []) if isinstance(result, dict) else result

    # --- Order Book ---

    async def get_order_book(self, symbol: str, depth: int = 20) -> dict:
        """Get futures order book."""
        return await self._public_get(f"/api/v1/level2/depth{depth}", {
            "symbol": symbol,
        })

    async def check_depth(self, symbol: str, min_depth_usd: float = 500) -> bool:
        """Check if order book has sufficient depth within 0.5%."""
        book = await self.get_order_book(symbol, 20)
        if not book:
            return False

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        if not bids or not asks:
            return False

        mid = (float(bids[0][0]) + float(asks[0][0])) / 2
        threshold = mid * 0.005  # 0.5%

        bid_depth = sum(
            float(b[0]) * float(b[1])
            for b in bids
            if float(b[0]) >= mid - threshold
        )
        ask_depth = sum(
            float(a[0]) * float(a[1])
            for a in asks
            if float(a[0]) <= mid + threshold
        )

        min_side = min(bid_depth, ask_depth)
        return min_side >= min_depth_usd

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
