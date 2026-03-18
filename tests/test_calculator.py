"""Tests for profit calculator."""

import pytest

from core.calculator import ProfitCalculator
from core.models import (
    Direction,
    OrderBook,
    OrderBookLevel,
    OrderSide,
    Ticker,
    TradingPair,
    TriangleLeg,
    Triangle,
)
from core.triangle import TriangleGraph


# --- Helpers ---

def make_ticker(symbol: str, bid: float, ask: float) -> Ticker:
    return Ticker(symbol=symbol, bid=bid, ask=ask, timestamp_ms=0)


def make_triangle() -> Triangle:
    """USDT → BTC → ETH → USDT triangle."""
    graph = TriangleGraph()
    graph.load_pairs([
        TradingPair(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT"),
        TradingPair(symbol="ETHUSDT", base_asset="ETH", quote_asset="USDT"),
        TradingPair(symbol="ETHBTC", base_asset="ETH", quote_asset="BTC"),
    ])
    triangles = graph.discover_triangles()
    return triangles[0]


# --- Tests ---

class TestProfitCalculator:

    def test_no_arbitrage_equal_prices(self):
        """When prices are perfectly aligned, profit should be negative (fees eat it)."""
        calc = ProfitCalculator(fee_rate=0.00075)
        tri = make_triangle()

        # Perfectly aligned prices: BTC=67000, ETH=3500, ETHBTC=3500/67000
        eth_btc_price = 3500.0 / 67000.0
        tickers = {
            "BTCUSDT": make_ticker("BTCUSDT", 67000.0, 67000.0),
            "ETHUSDT": make_ticker("ETHUSDT", 3500.0, 3500.0),
            "ETHBTC": make_ticker("ETHBTC", eth_btc_price, eth_btc_price),
        }

        fwd, rev, direction = calc.triangle_profit(tri, tickers)
        # With fees, both should be negative
        assert fwd < 0
        assert rev < 0

    def test_profitable_forward(self):
        """Create a price dislocation that makes forward path profitable."""
        calc = ProfitCalculator(fee_rate=0.00075)
        tri = make_triangle()

        # Create dislocation: ETHBTC is cheap relative to ETHUSDT/BTCUSDT
        tickers = {
            "BTCUSDT": make_ticker("BTCUSDT", 67000.0, 67000.0),
            "ETHUSDT": make_ticker("ETHUSDT", 3600.0, 3600.0),
            "ETHBTC": make_ticker("ETHBTC", 0.050, 0.050),
            # Fair ETHBTC = 3600/67000 = 0.05373
            # Buying ETH at 0.050 BTC and selling at 3600 USDT is profitable
        }

        fwd, rev, direction = calc.triangle_profit(tri, tickers)
        # At least one direction should be profitable
        best = max(fwd, rev)
        assert best > 0, f"Expected profit but got fwd={fwd}, rev={rev}"

    def test_fee_impact(self):
        """Higher fees should reduce or eliminate profit."""
        tri = make_triangle()

        tickers = {
            "BTCUSDT": make_ticker("BTCUSDT", 67000.0, 67000.0),
            "ETHUSDT": make_ticker("ETHUSDT", 3600.0, 3600.0),
            "ETHBTC": make_ticker("ETHBTC", 0.052, 0.052),
        }

        low_fee = ProfitCalculator(fee_rate=0.00075)
        high_fee = ProfitCalculator(fee_rate=0.01)  # 1% per trade

        _, _, _ = low_fee.triangle_profit(tri, tickers)
        fwd_low, rev_low, _ = low_fee.triangle_profit(tri, tickers)
        fwd_high, rev_high, _ = high_fee.triangle_profit(tri, tickers)

        assert max(fwd_low, rev_low) > max(fwd_high, rev_high)

    def test_missing_ticker_returns_negative(self):
        """Missing price data should return -1."""
        calc = ProfitCalculator(fee_rate=0.00075)
        tri = make_triangle()

        tickers = {
            "BTCUSDT": make_ticker("BTCUSDT", 67000.0, 67000.0),
            # Missing ETHUSDT and ETHBTC
        }

        fwd, rev, _ = calc.triangle_profit(tri, tickers)
        assert fwd == -1.0 or rev == -1.0

    def test_zero_price_returns_negative(self):
        """Zero bid/ask should return -1."""
        calc = ProfitCalculator(fee_rate=0.00075)
        tri = make_triangle()

        tickers = {
            "BTCUSDT": make_ticker("BTCUSDT", 67000.0, 67000.0),
            "ETHUSDT": make_ticker("ETHUSDT", 0.0, 0.0),
            "ETHBTC": make_ticker("ETHBTC", 0.05, 0.05),
        }

        fwd, rev, _ = calc.triangle_profit(tri, tickers)
        assert fwd < 0

    def test_spread_impact(self):
        """Wider spreads should reduce profit."""
        calc = ProfitCalculator(fee_rate=0.00075)
        tri = make_triangle()

        # Tight spread
        tight = {
            "BTCUSDT": make_ticker("BTCUSDT", 67000.0, 67001.0),
            "ETHUSDT": make_ticker("ETHUSDT", 3600.0, 3600.5),
            "ETHBTC": make_ticker("ETHBTC", 0.050, 0.05001),
        }

        # Wide spread
        wide = {
            "BTCUSDT": make_ticker("BTCUSDT", 66900.0, 67100.0),
            "ETHUSDT": make_ticker("ETHUSDT", 3580.0, 3620.0),
            "ETHBTC": make_ticker("ETHBTC", 0.049, 0.051),
        }

        fwd_tight, rev_tight, _ = calc.triangle_profit(tri, tight)
        fwd_wide, rev_wide, _ = calc.triangle_profit(tri, wide)

        best_tight = max(fwd_tight, rev_tight)
        best_wide = max(fwd_wide, rev_wide)
        assert best_tight > best_wide

    def test_batch_calculate_finds_profitable(self):
        """Batch calculation should find profitable triangles."""
        calc = ProfitCalculator(fee_rate=0.00075)

        graph = TriangleGraph()
        graph.load_pairs([
            TradingPair(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT"),
            TradingPair(symbol="ETHUSDT", base_asset="ETH", quote_asset="USDT"),
            TradingPair(symbol="ETHBTC", base_asset="ETH", quote_asset="BTC"),
        ])
        triangles = graph.discover_triangles()

        # Profitable dislocation
        tickers = {
            "BTCUSDT": make_ticker("BTCUSDT", 67000.0, 67000.0),
            "ETHUSDT": make_ticker("ETHUSDT", 3600.0, 3600.0),
            "ETHBTC": make_ticker("ETHBTC", 0.050, 0.050),
        }

        opps = calc.batch_calculate(triangles, tickers, min_profit=0.0)
        assert len(opps) >= 1
        assert opps[0].theoretical_profit > 0

    def test_batch_calculate_filters_below_threshold(self):
        """Batch should filter out opportunities below min_profit."""
        calc = ProfitCalculator(fee_rate=0.00075)

        graph = TriangleGraph()
        graph.load_pairs([
            TradingPair(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT"),
            TradingPair(symbol="ETHUSDT", base_asset="ETH", quote_asset="USDT"),
            TradingPair(symbol="ETHBTC", base_asset="ETH", quote_asset="BTC"),
        ])
        triangles = graph.discover_triangles()

        # Slight dislocation
        tickers = {
            "BTCUSDT": make_ticker("BTCUSDT", 67000.0, 67000.0),
            "ETHUSDT": make_ticker("ETHUSDT", 3500.0, 3500.0),
            "ETHBTC": make_ticker("ETHBTC", 0.05220, 0.05220),
        }

        # Very high threshold
        opps = calc.batch_calculate(triangles, tickers, min_profit=0.5)
        assert len(opps) == 0

    def test_batch_sorted_by_profit(self):
        """Results should be sorted by profit descending."""
        calc = ProfitCalculator(fee_rate=0.00075)

        graph = TriangleGraph()
        graph.load_pairs([
            TradingPair(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT"),
            TradingPair(symbol="ETHUSDT", base_asset="ETH", quote_asset="USDT"),
            TradingPair(symbol="BNBUSDT", base_asset="BNB", quote_asset="USDT"),
            TradingPair(symbol="ETHBTC", base_asset="ETH", quote_asset="BTC"),
            TradingPair(symbol="BNBBTC", base_asset="BNB", quote_asset="BTC"),
            TradingPair(symbol="BNBETH", base_asset="BNB", quote_asset="ETH"),
        ])
        triangles = graph.discover_triangles()

        tickers = {
            "BTCUSDT": make_ticker("BTCUSDT", 67000.0, 67000.0),
            "ETHUSDT": make_ticker("ETHUSDT", 3600.0, 3600.0),
            "BNBUSDT": make_ticker("BNBUSDT", 600.0, 600.0),
            "ETHBTC": make_ticker("ETHBTC", 0.050, 0.050),
            "BNBBTC": make_ticker("BNBBTC", 0.0085, 0.0085),
            "BNBETH": make_ticker("BNBETH", 0.16, 0.16),
        }

        opps = calc.batch_calculate(triangles, tickers, min_profit=-1.0)
        if len(opps) >= 2:
            for i in range(len(opps) - 1):
                assert opps[i].theoretical_profit >= opps[i + 1].theoretical_profit


class TestOrderBookProfit:

    def test_executable_buy_price(self):
        """Test order book buy price calculation."""
        book = OrderBook(
            symbol="BTCUSDT",
            asks=[
                OrderBookLevel(price=67000.0, quantity=0.1),
                OrderBookLevel(price=67010.0, quantity=0.2),
                OrderBookLevel(price=67020.0, quantity=0.5),
            ],
            bids=[],
        )

        # Buy 0.1 BTC — should fill at first level
        price = book.executable_buy_price(0.1)
        assert price == 67000.0

        # Buy 0.3 BTC — walks into second level
        price = book.executable_buy_price(0.3)
        assert price is not None
        assert price > 67000.0
        assert price < 67010.0

    def test_executable_sell_price(self):
        """Test order book sell price calculation."""
        book = OrderBook(
            symbol="BTCUSDT",
            bids=[
                OrderBookLevel(price=67000.0, quantity=0.1),
                OrderBookLevel(price=66990.0, quantity=0.2),
                OrderBookLevel(price=66980.0, quantity=0.5),
            ],
            asks=[],
        )

        # Sell 0.1 BTC — fills at best bid
        price = book.executable_sell_price(0.1)
        assert price == 67000.0

        # Sell 0.3 BTC — walks into second level
        price = book.executable_sell_price(0.3)
        assert price is not None
        assert price < 67000.0
        assert price > 66990.0

    def test_insufficient_liquidity(self):
        """Should return None when book can't fill the order."""
        book = OrderBook(
            symbol="BTCUSDT",
            asks=[
                OrderBookLevel(price=67000.0, quantity=0.01),
            ],
            bids=[],
        )

        price = book.executable_buy_price(1.0)  # Need 1 BTC, only 0.01 available
        assert price is None

    def test_empty_book(self):
        book = OrderBook(symbol="BTCUSDT")
        assert book.best_bid == 0.0
        assert book.best_ask == 0.0
        assert book.executable_buy_price(1.0) is None
        assert book.executable_sell_price(1.0) is None
