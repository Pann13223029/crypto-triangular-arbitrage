"""Tests for the real-time triangle scanner."""

import pytest

from core.calculator import ProfitCalculator
from core.models import Ticker, TradingPair
from core.scanner import TriangleScanner
from core.triangle import TriangleGraph


def make_ticker(symbol: str, bid: float, ask: float) -> Ticker:
    return Ticker(symbol=symbol, bid=bid, ask=ask, timestamp_ms=0)


@pytest.fixture
def scanner() -> TriangleScanner:
    """Scanner with a single triangle: USDT-BTC-ETH."""
    graph = TriangleGraph()
    graph.load_pairs([
        TradingPair(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT"),
        TradingPair(symbol="ETHUSDT", base_asset="ETH", quote_asset="USDT"),
        TradingPair(symbol="ETHBTC", base_asset="ETH", quote_asset="BTC"),
    ])
    graph.discover_triangles()

    calc = ProfitCalculator(fee_rate=0.00075)
    return TriangleScanner(graph, calc, min_profit=0.001)


@pytest.fixture
def multi_scanner() -> TriangleScanner:
    """Scanner with multiple triangles."""
    graph = TriangleGraph()
    graph.load_pairs([
        TradingPair(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT"),
        TradingPair(symbol="ETHUSDT", base_asset="ETH", quote_asset="USDT"),
        TradingPair(symbol="BNBUSDT", base_asset="BNB", quote_asset="USDT"),
        TradingPair(symbol="ETHBTC", base_asset="ETH", quote_asset="BTC"),
        TradingPair(symbol="BNBBTC", base_asset="BNB", quote_asset="BTC"),
        TradingPair(symbol="BNBETH", base_asset="BNB", quote_asset="ETH"),
    ])
    graph.discover_triangles()

    calc = ProfitCalculator(fee_rate=0.00075)
    return TriangleScanner(graph, calc, min_profit=0.001)


class TestTriangleScanner:

    def test_no_opportunity_without_all_prices(self, scanner):
        """Can't find opportunities with incomplete price data."""
        opps = scanner.update_ticker(
            make_ticker("BTCUSDT", 67000.0, 67000.0)
        )
        assert len(opps) == 0

    def test_no_opportunity_balanced_market(self, scanner):
        """Perfectly balanced prices yield no opportunity (fees eat profit)."""
        eth_btc = 3500.0 / 67000.0
        scanner.update_ticker(make_ticker("BTCUSDT", 67000.0, 67000.0))
        scanner.update_ticker(make_ticker("ETHUSDT", 3500.0, 3500.0))
        opps = scanner.update_ticker(
            make_ticker("ETHBTC", eth_btc, eth_btc)
        )
        assert len(opps) == 0

    def test_finds_opportunity_on_dislocation(self, scanner):
        """Detects opportunity when prices are dislocated."""
        scanner.update_ticker(make_ticker("BTCUSDT", 67000.0, 67000.0))
        scanner.update_ticker(make_ticker("ETHUSDT", 3600.0, 3600.0))
        opps = scanner.update_ticker(
            make_ticker("ETHBTC", 0.050, 0.050)
        )
        # 0.050 vs fair 3600/67000=0.05373 → ~7% dislocation
        assert len(opps) >= 1
        assert opps[0].theoretical_profit > 0.001

    def test_tick_counter(self, scanner):
        """Stats should track tick count."""
        scanner.update_ticker(make_ticker("BTCUSDT", 67000.0, 67000.0))
        scanner.update_ticker(make_ticker("ETHUSDT", 3500.0, 3500.0))
        scanner.update_ticker(make_ticker("ETHBTC", 0.0522, 0.0522))

        stats = scanner.stats()
        assert stats["total_ticks"] == 3

    def test_only_affected_triangles_scanned(self, multi_scanner):
        """Updating one symbol should only scan triangles containing it."""
        # Load all prices first
        multi_scanner.update_ticker(make_ticker("BTCUSDT", 67000.0, 67000.0))
        multi_scanner.update_ticker(make_ticker("ETHUSDT", 3500.0, 3500.0))
        multi_scanner.update_ticker(make_ticker("BNBUSDT", 600.0, 600.0))
        multi_scanner.update_ticker(make_ticker("ETHBTC", 0.0522, 0.0522))
        multi_scanner.update_ticker(make_ticker("BNBBTC", 0.00895, 0.00895))
        multi_scanner.update_ticker(make_ticker("BNBETH", 0.1714, 0.1714))

        scans_before = multi_scanner.total_scans

        # BNBETH is in fewer triangles than BTCUSDT
        multi_scanner.update_ticker(make_ticker("BNBETH", 0.1714, 0.1714))
        scans_bnbeth = multi_scanner.total_scans - scans_before

        scans_before = multi_scanner.total_scans
        multi_scanner.update_ticker(make_ticker("BTCUSDT", 67000.0, 67000.0))
        scans_btcusdt = multi_scanner.total_scans - scans_before

        # BTCUSDT appears in more triangles than BNBETH
        assert scans_btcusdt >= scans_bnbeth

    def test_bulk_update(self, scanner):
        """Bulk update processes multiple tickers efficiently."""
        tickers = [
            make_ticker("BTCUSDT", 67000.0, 67000.0),
            make_ticker("ETHUSDT", 3600.0, 3600.0),
            make_ticker("ETHBTC", 0.050, 0.050),
        ]
        opps = scanner.bulk_update(tickers)
        # Same dislocation as single update test
        assert len(opps) >= 1

    def test_unknown_symbol_ignored(self, scanner):
        """Updating a symbol not in any triangle is harmless."""
        opps = scanner.update_ticker(
            make_ticker("DOGEUSDT", 0.15, 0.15)
        )
        assert len(opps) == 0

    def test_stats_tracking(self, scanner):
        scanner.update_ticker(make_ticker("BTCUSDT", 67000.0, 67000.0))
        scanner.update_ticker(make_ticker("ETHUSDT", 3600.0, 3600.0))
        scanner.update_ticker(make_ticker("ETHBTC", 0.050, 0.050))

        stats = scanner.stats()
        assert stats["total_ticks"] == 3
        assert stats["tracked_symbols"] == 3
        assert stats["total_triangle_scans"] > 0
