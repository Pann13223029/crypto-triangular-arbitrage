"""Tests for triangle discovery."""

import pytest

from core.models import OrderSide, TradingPair
from core.triangle import TriangleGraph


def make_pair(symbol: str, base: str, quote: str) -> TradingPair:
    return TradingPair(symbol=symbol, base_asset=base, quote_asset=quote)


@pytest.fixture
def simple_pairs() -> list[TradingPair]:
    """Three pairs forming exactly one triangle: USDT-BTC-ETH."""
    return [
        make_pair("BTCUSDT", "BTC", "USDT"),
        make_pair("ETHUSDT", "ETH", "USDT"),
        make_pair("ETHBTC", "ETH", "BTC"),
    ]


@pytest.fixture
def multi_pairs() -> list[TradingPair]:
    """Pairs forming multiple triangles."""
    return [
        make_pair("BTCUSDT", "BTC", "USDT"),
        make_pair("ETHUSDT", "ETH", "USDT"),
        make_pair("BNBUSDT", "BNB", "USDT"),
        make_pair("ETHBTC", "ETH", "BTC"),
        make_pair("BNBBTC", "BNB", "BTC"),
        make_pair("BNBETH", "BNB", "ETH"),
    ]


class TestTriangleGraph:
    def test_add_pair(self):
        graph = TriangleGraph()
        pair = make_pair("BTCUSDT", "BTC", "USDT")
        graph.add_pair(pair)

        assert "BTC" in graph.adjacency
        assert "USDT" in graph.adjacency["BTC"]
        assert "BTC" in graph.adjacency["USDT"]
        assert graph.symbol_map["BTCUSDT"] == pair

    def test_discover_single_triangle(self, simple_pairs):
        graph = TriangleGraph()
        graph.load_pairs(simple_pairs)
        triangles = graph.discover_triangles()

        assert len(triangles) == 1
        tri = triangles[0]
        assert set(tri.assets) == {"USDT", "BTC", "ETH"}

    def test_discover_multiple_triangles(self, multi_pairs):
        graph = TriangleGraph()
        graph.load_pairs(multi_pairs)
        triangles = graph.discover_triangles()

        # 4 assets with all pairs = 4 triangles
        # USDT-BTC-ETH, USDT-BTC-BNB, USDT-ETH-BNB, BTC-ETH-BNB
        assert len(triangles) == 4

    def test_no_triangle_without_all_edges(self):
        """Two pairs alone can't form a triangle."""
        graph = TriangleGraph()
        graph.load_pairs([
            make_pair("BTCUSDT", "BTC", "USDT"),
            make_pair("ETHUSDT", "ETH", "USDT"),
        ])
        triangles = graph.discover_triangles()
        assert len(triangles) == 0

    def test_forward_legs_direction(self, simple_pairs):
        graph = TriangleGraph()
        graph.load_pairs(simple_pairs)
        triangles = graph.discover_triangles()
        tri = triangles[0]

        # Each leg should have a valid symbol and side
        for leg in tri.forward_legs:
            assert leg.symbol in {"BTCUSDT", "ETHUSDT", "ETHBTC"}
            assert leg.side in {OrderSide.BUY, OrderSide.SELL}

    def test_reverse_legs_are_opposite_path(self, simple_pairs):
        graph = TriangleGraph()
        graph.load_pairs(simple_pairs)
        triangles = graph.discover_triangles()
        tri = triangles[0]

        # Forward and reverse use the same symbols
        fwd_symbols = {leg.symbol for leg in tri.forward_legs}
        rev_symbols = {leg.symbol for leg in tri.reverse_legs}
        assert fwd_symbols == rev_symbols

    def test_symbols_frozenset(self, simple_pairs):
        graph = TriangleGraph()
        graph.load_pairs(simple_pairs)
        triangles = graph.discover_triangles()
        tri = triangles[0]

        assert tri.symbols == frozenset({"BTCUSDT", "ETHUSDT", "ETHBTC"})

    def test_symbol_to_triangles_index(self, multi_pairs):
        graph = TriangleGraph()
        graph.load_pairs(multi_pairs)
        graph.discover_triangles()

        # BTCUSDT appears in triangles containing BTC and USDT
        affected = graph.get_affected_triangles("BTCUSDT")
        assert len(affected) >= 1
        for tri in affected:
            assert "BTCUSDT" in tri.symbols

    def test_subscribed_symbols(self, multi_pairs):
        graph = TriangleGraph()
        graph.load_pairs(multi_pairs)
        graph.discover_triangles()

        symbols = graph.get_subscribed_symbols()
        assert "BTCUSDT" in symbols
        assert "ETHBTC" in symbols

    def test_max_triangles_cap(self, multi_pairs):
        graph = TriangleGraph()
        graph.load_pairs(multi_pairs)
        triangles = graph.discover_triangles(max_triangles=2)

        assert len(triangles) == 2

    def test_stats(self, multi_pairs):
        graph = TriangleGraph()
        graph.load_pairs(multi_pairs)
        graph.discover_triangles()

        s = graph.stats()
        assert s["total_assets"] == 4
        assert s["total_pairs"] == 6
        assert s["total_triangles"] == 4

    def test_deduplication(self):
        """Same triangle shouldn't appear twice."""
        graph = TriangleGraph()
        graph.load_pairs([
            make_pair("BTCUSDT", "BTC", "USDT"),
            make_pair("ETHUSDT", "ETH", "USDT"),
            make_pair("ETHBTC", "ETH", "BTC"),
        ])
        triangles = graph.discover_triangles()
        assert len(triangles) == 1

    def test_empty_graph(self):
        graph = TriangleGraph()
        triangles = graph.discover_triangles()
        assert len(triangles) == 0
        assert graph.stats()["total_triangles"] == 0
