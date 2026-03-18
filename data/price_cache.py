"""In-memory price cache — central price state shared across components."""

import logging
from time import time_ns

from core.models import OrderBook, Ticker

logger = logging.getLogger(__name__)


class PriceCache:
    """
    Thread-safe in-memory cache for real-time price data.

    Stores current tickers and order books for all subscribed symbols.
    Provides staleness detection for risk management.
    """

    def __init__(self, stale_threshold_ms: int = 5000):
        self.tickers: dict[str, Ticker] = {}
        self.order_books: dict[str, OrderBook] = {}
        self.stale_threshold_ms = stale_threshold_ms

        # Stats
        self.total_ticker_updates = 0
        self.total_book_updates = 0
        self._last_update_ms = 0

    def update_ticker(self, ticker: Ticker) -> None:
        """Update or insert a ticker."""
        self.tickers[ticker.symbol] = ticker
        self.total_ticker_updates += 1
        self._last_update_ms = time_ns() // 1_000_000

    def update_order_book(self, order_book: OrderBook) -> None:
        """Update or insert an order book."""
        self.order_books[order_book.symbol] = order_book
        self.total_book_updates += 1
        self._last_update_ms = time_ns() // 1_000_000

    def get_ticker(self, symbol: str) -> Ticker | None:
        return self.tickers.get(symbol)

    def get_order_book(self, symbol: str) -> OrderBook | None:
        return self.order_books.get(symbol)

    def is_stale(self) -> bool:
        """Check if cache hasn't been updated recently."""
        if self._last_update_ms == 0:
            return True
        now = time_ns() // 1_000_000
        return (now - self._last_update_ms) > self.stale_threshold_ms

    def has_all_tickers(self, symbols: set[str]) -> bool:
        """Check if we have price data for all required symbols."""
        return all(s in self.tickers for s in symbols)

    def missing_symbols(self, symbols: set[str]) -> set[str]:
        """Return symbols we don't have data for yet."""
        return symbols - set(self.tickers.keys())

    def stats(self) -> dict:
        return {
            "tracked_tickers": len(self.tickers),
            "tracked_books": len(self.order_books),
            "total_ticker_updates": self.total_ticker_updates,
            "total_book_updates": self.total_book_updates,
            "is_stale": self.is_stale(),
        }
