"""Abstract exchange interface — shared by all exchange implementations."""

from abc import ABC, abstractmethod

from config.settings import FeeSchedule
from core.models import Order, OrderBook, OrderSide, Ticker, TradingPair


class ExchangeBase(ABC):
    """Base class for all exchange implementations."""

    @property
    @abstractmethod
    def exchange_id(self) -> str:
        """Unique exchange identifier (e.g., 'binance', 'bybit')."""
        ...

    @property
    @abstractmethod
    def fee_schedule(self) -> FeeSchedule:
        """Current fee schedule for this exchange."""
        ...

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
        """Place a market (price=None) or limit order."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Clean up resources."""
        ...

    # --- Cross-exchange methods (optional, default raises) ---

    async def get_withdrawal_fee(self, asset: str, chain: str) -> float:
        raise NotImplementedError("Withdrawal not supported")

    async def withdraw(self, asset: str, amount: float, address: str, chain: str) -> str:
        raise NotImplementedError("Withdrawal not supported")

    async def get_deposit_address(self, asset: str, chain: str) -> str:
        raise NotImplementedError("Deposit address not supported")
