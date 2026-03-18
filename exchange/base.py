"""Abstract exchange interface — shared by SimulatedExchange and LiveExchange."""

from abc import ABC, abstractmethod

from core.models import Order, OrderBook, OrderSide, Ticker, TradingPair


class ExchangeBase(ABC):
    """Base class for all exchange implementations."""

    @abstractmethod
    async def get_all_pairs(self) -> list[TradingPair]:
        """Fetch all available trading pairs."""
        ...

    @abstractmethod
    async def get_ticker(self, symbol: str) -> Ticker:
        """Get current bid/ask for a symbol."""
        ...

    @abstractmethod
    async def get_order_book(self, symbol: str, depth: int = 5) -> OrderBook:
        """Get order book snapshot."""
        ...

    @abstractmethod
    async def get_balance(self, asset: str) -> float:
        """Get available balance for an asset."""
        ...

    @abstractmethod
    async def get_all_balances(self) -> dict[str, float]:
        """Get all non-zero balances."""
        ...

    @abstractmethod
    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        price: float | None = None,
    ) -> Order:
        """
        Place a market (price=None) or limit order.

        Returns filled Order with actual_price and fee populated.
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """Clean up resources."""
        ...
