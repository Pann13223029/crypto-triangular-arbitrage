"""Tests for CrossExchangeBook and CrossExchangeScanner."""

import pytest
from time import time_ns

from config.settings import FeeSchedule
from core.models import Ticker
from cross_exchange.book import CrossExchangeBook
from cross_exchange.models import ExchangeQuote
from cross_exchange.scanner import CrossExchangeScanner


def now_ms():
    return time_ns() // 1_000_000


def fee_schedules():
    return {
        "binance": FeeSchedule(exchange_id="binance", taker_fee=0.00075),
        "bybit": FeeSchedule(exchange_id="bybit", taker_fee=0.00075),
        "okx": FeeSchedule(exchange_id="okx", taker_fee=0.001),
    }


class TestCrossExchangeBook:

    def test_needs_two_exchanges(self):
        book = CrossExchangeBook("BTCUSDT", fee_schedules(), min_net_spread=0.0)
        quote = ExchangeQuote("binance", "BTCUSDT", 67000, 67010, timestamp_ms=now_ms())
        opp = book.update(quote)
        assert opp is None  # Only 1 exchange

    def test_detects_spread_across_exchanges(self):
        book = CrossExchangeBook("BTCUSDT", fee_schedules(), min_net_spread=0.0)

        book.update(ExchangeQuote("binance", "BTCUSDT", 66900, 67000, timestamp_ms=now_ms()))
        opp = book.update(ExchangeQuote("bybit", "BTCUSDT", 67200, 67300, timestamp_ms=now_ms()))

        assert opp is not None
        assert opp.buy_exchange == "binance"  # Lower ask
        assert opp.sell_exchange == "bybit"  # Higher bid
        assert opp.buy_price == 67000
        assert opp.sell_price == 67200
        assert opp.gross_spread > 0

    def test_rejects_same_exchange(self):
        book = CrossExchangeBook("BTCUSDT", fee_schedules(), min_net_spread=0.0)

        # Both quotes from binance — second overwrites first
        book.update(ExchangeQuote("binance", "BTCUSDT", 67000, 67100, timestamp_ms=now_ms()))
        opp = book.update(ExchangeQuote("binance", "BTCUSDT", 67200, 67300, timestamp_ms=now_ms()))
        assert opp is None

    def test_rejects_negative_spread(self):
        book = CrossExchangeBook("BTCUSDT", fee_schedules(), min_net_spread=0.0)

        # Bybit bid < Binance ask — no opportunity
        book.update(ExchangeQuote("binance", "BTCUSDT", 67100, 67200, timestamp_ms=now_ms()))
        opp = book.update(ExchangeQuote("bybit", "BTCUSDT", 66900, 67000, timestamp_ms=now_ms()))
        assert opp is None

    def test_net_spread_accounts_for_fees(self):
        book = CrossExchangeBook("BTCUSDT", fee_schedules(), min_net_spread=0.001)

        # Gross spread ~0.15% but fees are 0.075% + 0.075% = 0.15%
        # Net spread ~0% — below threshold
        book.update(ExchangeQuote("binance", "BTCUSDT", 66990, 67000, timestamp_ms=now_ms()))
        opp = book.update(ExchangeQuote("bybit", "BTCUSDT", 67100, 67200, timestamp_ms=now_ms()))
        # 67100/67000 - 1 = 0.00149 gross, net = 0.00149 - 0.0015 = ~0
        assert opp is None

    def test_stale_quote_ignored(self):
        book = CrossExchangeBook("BTCUSDT", fee_schedules(), staleness_ms=500, min_net_spread=0.0)

        old_ts = now_ms() - 2000  # 2 seconds ago
        book.update(ExchangeQuote("binance", "BTCUSDT", 66900, 67000, timestamp_ms=old_ts))
        opp = book.update(ExchangeQuote("bybit", "BTCUSDT", 67200, 67300, timestamp_ms=now_ms()))

        # Binance quote is stale
        assert opp is None

    def test_three_exchanges_picks_best(self):
        book = CrossExchangeBook("ETHUSDT", fee_schedules(), min_net_spread=0.0)
        ts = now_ms()

        book.update(ExchangeQuote("binance", "ETHUSDT", 3440, 3450, timestamp_ms=ts))
        book.update(ExchangeQuote("bybit", "ETHUSDT", 3455, 3460, timestamp_ms=ts))
        opp = book.update(ExchangeQuote("okx", "ETHUSDT", 3458, 3465, timestamp_ms=ts))

        assert opp is not None
        assert opp.buy_exchange == "binance"  # Lowest ask: 3450
        assert opp.sell_exchange == "okx"  # Highest bid: 3458

    def test_spread_summary(self):
        book = CrossExchangeBook("BTCUSDT", fee_schedules(), min_net_spread=0.0)
        ts = now_ms()

        book.update(ExchangeQuote("binance", "BTCUSDT", 66900, 67000, timestamp_ms=ts))
        book.update(ExchangeQuote("bybit", "BTCUSDT", 67200, 67300, timestamp_ms=ts))

        summary = book.spread_summary()
        assert summary is not None
        assert summary["buy_exchange"] == "binance"
        assert summary["sell_exchange"] == "bybit"


class TestCrossExchangeScanner:

    def test_detects_opportunity(self):
        scanner = CrossExchangeScanner(
            symbols=["BTCUSDT"],
            fee_schedules=fee_schedules(),
            min_net_spread=0.0,
        )

        scanner.update("binance", Ticker("BTCUSDT", 66900, 67000, now_ms()))
        opp = scanner.update("bybit", Ticker("BTCUSDT", 67200, 67300, now_ms()))

        assert opp is not None
        assert opp.symbol == "BTCUSDT"
        assert scanner.total_opportunities == 1

    def test_ignores_unknown_symbol(self):
        scanner = CrossExchangeScanner(
            symbols=["BTCUSDT"],
            fee_schedules=fee_schedules(),
        )

        opp = scanner.update("binance", Ticker("DOGEUSDT", 0.15, 0.16, now_ms()))
        assert opp is None

    def test_dedup_cooldown(self):
        scanner = CrossExchangeScanner(
            symbols=["BTCUSDT"],
            fee_schedules=fee_schedules(),
            min_net_spread=0.0,
            dedup_cooldown_ms=5000,
        )

        scanner.update("binance", Ticker("BTCUSDT", 66900, 67000, now_ms()))
        opp1 = scanner.update("bybit", Ticker("BTCUSDT", 67200, 67300, now_ms()))
        assert opp1 is not None

        # Same opportunity again — should be deduped
        opp2 = scanner.update("bybit", Ticker("BTCUSDT", 67200, 67300, now_ms()))
        assert opp2 is None
        assert scanner.total_deduped == 1

    def test_stats(self):
        scanner = CrossExchangeScanner(
            symbols=["BTCUSDT", "ETHUSDT"],
            fee_schedules=fee_schedules(),
        )

        scanner.update("binance", Ticker("BTCUSDT", 67000, 67010, now_ms()))
        scanner.update("bybit", Ticker("BTCUSDT", 67005, 67015, now_ms()))

        stats = scanner.stats()
        assert stats["tracked_symbols"] == 2
        assert stats["total_updates"] == 2
