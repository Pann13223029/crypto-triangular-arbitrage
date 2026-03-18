"""Records live price data for later backtesting replay."""

import csv
import logging
import os
from datetime import datetime
from pathlib import Path
from time import time_ns

from core.models import OrderBook, Ticker

logger = logging.getLogger(__name__)


class DataRecorder:
    """
    Records live ticker and order book data to CSV files.

    Files are organized by date for easy replay:
        data/recordings/2026-03-18_tickers.csv
        data/recordings/2026-03-18_orderbooks.csv
    """

    def __init__(self, output_dir: str = "data/recordings"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self._date = datetime.now().strftime("%Y-%m-%d")
        self._ticker_file = None
        self._ticker_writer = None
        self._book_file = None
        self._book_writer = None

        self.total_tickers = 0
        self.total_books = 0

        self._init_files()

    def _init_files(self) -> None:
        """Open CSV files and write headers."""
        ticker_path = self.output_dir / f"{self._date}_tickers.csv"
        book_path = self.output_dir / f"{self._date}_orderbooks.csv"

        ticker_exists = ticker_path.exists()
        book_exists = book_path.exists()

        self._ticker_file = open(ticker_path, "a", newline="")
        self._ticker_writer = csv.writer(self._ticker_file)
        if not ticker_exists:
            self._ticker_writer.writerow(["timestamp_ms", "symbol", "bid", "ask"])

        self._book_file = open(book_path, "a", newline="")
        self._book_writer = csv.writer(self._book_file)
        if not book_exists:
            self._book_writer.writerow([
                "timestamp_ms", "symbol", "level",
                "bid_price", "bid_qty", "ask_price", "ask_qty",
            ])

        logger.info("Recording to %s", self.output_dir)

    def record_ticker(self, ticker: Ticker) -> None:
        """Append a ticker to the CSV."""
        ts = ticker.timestamp_ms or (time_ns() // 1_000_000)
        self._ticker_writer.writerow([ts, ticker.symbol, ticker.bid, ticker.ask])
        self.total_tickers += 1

        # Flush periodically
        if self.total_tickers % 1000 == 0:
            self._ticker_file.flush()

    def record_order_book(self, book: OrderBook) -> None:
        """Append order book levels to the CSV."""
        ts = book.timestamp_ms or (time_ns() // 1_000_000)
        depth = max(len(book.bids), len(book.asks))

        for i in range(depth):
            bid_p = book.bids[i].price if i < len(book.bids) else 0
            bid_q = book.bids[i].quantity if i < len(book.bids) else 0
            ask_p = book.asks[i].price if i < len(book.asks) else 0
            ask_q = book.asks[i].quantity if i < len(book.asks) else 0

            self._book_writer.writerow([ts, book.symbol, i, bid_p, bid_q, ask_p, ask_q])

        self.total_books += 1
        if self.total_books % 500 == 0:
            self._book_file.flush()

    def close(self) -> None:
        """Flush and close files."""
        if self._ticker_file:
            self._ticker_file.flush()
            self._ticker_file.close()
        if self._book_file:
            self._book_file.flush()
            self._book_file.close()

        logger.info(
            "Recording stopped — %d tickers, %d books saved",
            self.total_tickers, self.total_books,
        )

    def stats(self) -> dict:
        return {
            "output_dir": str(self.output_dir),
            "date": self._date,
            "total_tickers": self.total_tickers,
            "total_books": self.total_books,
        }
