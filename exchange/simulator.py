"""Simulated exchange for paper trading with virtual balances."""

import asyncio
import logging
import random
import uuid
from time import time_ns

from config.settings import FeeConfig, FeeSchedule, SimulationConfig
from core.models import (
    Order,
    OrderBook,
    OrderBookLevel,
    OrderSide,
    OrderStatus,
    Ticker,
    TradingPair,
)
from exchange.base import ExchangeBase

logger = logging.getLogger(__name__)


class SimulatedExchange(ExchangeBase):
    """
    Paper trading exchange with virtual balances.

    Uses real price data from the scanner's price cache but simulates
    order fills with configurable slippage and fees.
    """

    def __init__(
        self,
        fee_config: FeeConfig | None = None,
        sim_config: SimulationConfig | None = None,
        exchange_id: str = "simulated",
    ):
        self.fee_config = fee_config or FeeConfig()
        self.sim_config = sim_config or SimulationConfig()
        self._exchange_id = exchange_id
        self._fee_schedule = FeeSchedule(
            exchange_id=exchange_id,
            taker_fee=self.fee_config.effective_fee,
            maker_fee=self.fee_config.maker_fee,
        )

        # Virtual balances
        self.balances: dict[str, float] = dict(self.sim_config.initial_balances)

        # Price data (injected from scanner/WebSocket)
        self.tickers: dict[str, Ticker] = {}
        self.order_books: dict[str, OrderBook] = {}

        # Trading pair metadata
        self.pairs: dict[str, TradingPair] = {}

        # Trade history
        self.order_history: list[Order] = []

        # Stats
        self.total_orders = 0
        self.total_fees_paid = 0.0

    def inject_ticker(self, ticker: Ticker) -> None:
        """Update price data (called by the scanner/WebSocket feed)."""
        self.tickers[ticker.symbol] = ticker

    def inject_order_book(self, order_book: OrderBook) -> None:
        """Update order book data."""
        self.order_books[order_book.symbol] = order_book

    def load_pairs(self, pairs: list[TradingPair]) -> None:
        """Load trading pair metadata."""
        for pair in pairs:
            self.pairs[pair.symbol] = pair

    def reset_balances(self) -> None:
        """Reset to initial balances."""
        self.balances = dict(self.sim_config.initial_balances)

    @property
    def exchange_id(self) -> str:
        return self._exchange_id

    @property
    def fee_schedule(self) -> FeeSchedule:
        return self._fee_schedule

    async def get_all_pairs(self) -> list[TradingPair]:
        return list(self.pairs.values())

    async def get_ticker(self, symbol: str) -> Ticker:
        if symbol not in self.tickers:
            raise ValueError(f"No price data for {symbol}")
        return self.tickers[symbol]

    async def get_order_book(self, symbol: str, depth: int = 5) -> OrderBook:
        if symbol in self.order_books:
            return self.order_books[symbol]
        # Generate synthetic order book from ticker
        ticker = self.tickers.get(symbol)
        if ticker is None:
            raise ValueError(f"No data for {symbol}")
        return self._synthetic_order_book(ticker, depth)

    async def get_balance(self, asset: str) -> float:
        return self.balances.get(asset, 0.0)

    async def get_all_balances(self) -> dict[str, float]:
        return {k: v for k, v in self.balances.items() if v > 0}

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        price: float | None = None,
    ) -> Order:
        """
        Simulate order execution.

        Applies slippage model and fees. Updates virtual balances.
        """
        # Simulate latency
        if self.sim_config.latency_ms > 0:
            await asyncio.sleep(self.sim_config.latency_ms / 1000)

        pair = self.pairs.get(symbol)
        if pair is None:
            return self._failed_order(symbol, side, quantity, "Unknown symbol")

        ticker = self.tickers.get(symbol)
        if ticker is None:
            return self._failed_order(symbol, side, quantity, "No price data")

        # Determine if this is a limit (maker) or market (taker) order
        is_limit = price is not None
        if is_limit:
            # Limit order: fill at the limit price, use maker fee
            fill_price = price
            fee_rate = self.fee_config.maker_fee
        else:
            # Market order: fill at market with slippage, use taker fee
            base_price = ticker.ask if side == OrderSide.BUY else ticker.bid
            if base_price <= 0:
                return self._failed_order(symbol, side, quantity, "Zero price")
            fill_price = self._apply_slippage(base_price, side)
            fee_rate = self.fee_config.effective_fee

        if side == OrderSide.BUY:
            # Spending quote to buy base
            cost = quantity * fill_price
            fee = cost * fee_rate
            total_cost = cost + fee

            # Check balance
            quote_balance = self.balances.get(pair.quote_asset, 0.0)
            if quote_balance < total_cost:
                return self._failed_order(
                    symbol, side, quantity,
                    f"Insufficient {pair.quote_asset}: need {total_cost:.8f}, have {quote_balance:.8f}",
                )

            # Execute
            self.balances[pair.quote_asset] = quote_balance - total_cost
            self.balances[pair.base_asset] = (
                self.balances.get(pair.base_asset, 0.0) + quantity
            )

        else:  # SELL
            # Selling base to get quote
            revenue = quantity * fill_price
            fee = revenue * fee_rate
            net_revenue = revenue - fee

            # Check balance
            base_balance = self.balances.get(pair.base_asset, 0.0)
            if base_balance < quantity:
                return self._failed_order(
                    symbol, side, quantity,
                    f"Insufficient {pair.base_asset}: need {quantity:.8f}, have {base_balance:.8f}",
                )

            # Execute
            self.balances[pair.base_asset] = base_balance - quantity
            self.balances[pair.quote_asset] = (
                self.balances.get(pair.quote_asset, 0.0) + net_revenue
            )

        self.total_orders += 1
        self.total_fees_paid += fee if side == OrderSide.BUY else fee

        order = Order(
            id=str(uuid.uuid4())[:8],
            symbol=symbol,
            side=side,
            quantity=quantity,
            expected_price=base_price,
            actual_price=fill_price,
            fee=fee,
            status=OrderStatus.FILLED,
            timestamp_ms=time_ns() // 1_000_000,
        )
        self.order_history.append(order)

        logger.debug(
            "SIM %s %s %.8f @ %.8f (slippage: %.4f%%, fee: %.8f)",
            side.value, symbol, quantity, fill_price,
            order.slippage * 100, fee,
        )

        return order

    def _apply_slippage(self, price: float, side: OrderSide) -> float:
        """Apply slippage model to the base price."""
        model = self.sim_config.slippage_model

        if model == "fixed":
            slip = self.sim_config.fixed_slippage
        elif model == "random":
            slip = random.uniform(0, self.sim_config.fixed_slippage * 2)
        elif model == "depth":
            # Use order book if available, otherwise fall back to fixed
            slip = self.sim_config.fixed_slippage
        else:
            slip = 0.0

        # Slippage is adverse: BUY pays more, SELL gets less
        if side == OrderSide.BUY:
            return price * (1 + slip)
        else:
            return price * (1 - slip)

    def _synthetic_order_book(self, ticker: Ticker, depth: int) -> OrderBook:
        """Generate a synthetic order book from ticker data."""
        spread_pct = max(ticker.spread, 0.0001)
        asks = []
        bids = []

        for i in range(depth):
            offset = spread_pct * (i + 1) * 0.5
            asks.append(OrderBookLevel(
                price=ticker.ask * (1 + offset),
                quantity=random.uniform(0.1, 2.0),
            ))
            bids.append(OrderBookLevel(
                price=ticker.bid * (1 - offset),
                quantity=random.uniform(0.1, 2.0),
            ))

        return OrderBook(
            symbol=ticker.symbol,
            asks=asks,
            bids=bids,
            timestamp_ms=ticker.timestamp_ms,
        )

    def _failed_order(
        self, symbol: str, side: OrderSide, quantity: float, reason: str
    ) -> Order:
        logger.warning("SIM order FAILED: %s %s %s — %s", side.value, symbol, quantity, reason)
        return Order(
            id=str(uuid.uuid4())[:8],
            symbol=symbol,
            side=side,
            quantity=quantity,
            status=OrderStatus.FAILED,
            timestamp_ms=time_ns() // 1_000_000,
        )

    async def close(self) -> None:
        pass

    def stats(self) -> dict:
        return {
            "total_orders": self.total_orders,
            "total_fees_paid": self.total_fees_paid,
            "balances": {k: round(v, 8) for k, v in self.balances.items() if v > 1e-10},
            "order_history_count": len(self.order_history),
        }
