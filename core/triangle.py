"""Triangle discovery — build asset graph and enumerate all valid 3-node cycles."""

from collections import defaultdict
from itertools import combinations

from core.models import (
    Direction,
    OrderSide,
    TradingPair,
    Triangle,
    TriangleLeg,
)


class TriangleGraph:
    """
    Directed graph of tradeable assets.

    Nodes = assets (BTC, ETH, USDT, ...)
    Edges = trading pairs connecting two assets.

    Discovers all valid 3-node cycles (triangles).
    """

    def __init__(self):
        # asset -> set of connected assets
        self.adjacency: dict[str, set[str]] = defaultdict(set)
        # (base, quote) -> TradingPair
        self.pair_map: dict[tuple[str, str], TradingPair] = {}
        # symbol -> TradingPair
        self.symbol_map: dict[str, TradingPair] = {}
        # symbol -> list of Triangle containing that symbol
        self.symbol_to_triangles: dict[str, list[Triangle]] = defaultdict(list)
        # All discovered triangles
        self.triangles: list[Triangle] = []

    def add_pair(self, pair: TradingPair) -> None:
        """Add a trading pair to the graph."""
        self.adjacency[pair.base_asset].add(pair.quote_asset)
        self.adjacency[pair.quote_asset].add(pair.base_asset)
        self.pair_map[(pair.base_asset, pair.quote_asset)] = pair
        self.symbol_map[pair.symbol] = pair

    def load_pairs(self, pairs: list[TradingPair]) -> None:
        """Load multiple trading pairs into the graph."""
        for pair in pairs:
            self.add_pair(pair)

    def _find_pair(self, asset_a: str, asset_b: str) -> TradingPair | None:
        """Find the trading pair between two assets (either direction)."""
        if (asset_a, asset_b) in self.pair_map:
            return self.pair_map[(asset_a, asset_b)]
        if (asset_b, asset_a) in self.pair_map:
            return self.pair_map[(asset_b, asset_a)]
        return None

    def _build_leg(
        self, from_asset: str, to_asset: str, pair: TradingPair
    ) -> TriangleLeg:
        """
        Build a trade leg to convert from_asset → to_asset.

        If from_asset is the quote asset (e.g., USDT in BTCUSDT),
        we BUY the base (BTC) with our quote (USDT).

        If from_asset is the base asset (e.g., BTC in BTCUSDT),
        we SELL the base (BTC) to get quote (USDT).
        """
        if from_asset == pair.quote_asset:
            # We have quote, want base → BUY
            return TriangleLeg(
                symbol=pair.symbol,
                side=OrderSide.BUY,
                base_asset=pair.base_asset,
                quote_asset=pair.quote_asset,
            )
        else:
            # We have base, want quote → SELL
            return TriangleLeg(
                symbol=pair.symbol,
                side=OrderSide.SELL,
                base_asset=pair.base_asset,
                quote_asset=pair.quote_asset,
            )

    def discover_triangles(self, max_triangles: int = 5000) -> list[Triangle]:
        """
        Find all valid 3-node cycles in the graph.

        A valid triangle (A, B, C) requires trading pairs between
        all three pairs: A-B, B-C, and C-A.
        """
        assets = list(self.adjacency.keys())
        triangles: list[Triangle] = []
        seen: set[frozenset[str]] = set()
        tri_id = 0

        for a, b, c in combinations(assets, 3):
            # Check all three edges exist
            pair_ab = self._find_pair(a, b)
            pair_bc = self._find_pair(b, c)
            pair_ca = self._find_pair(c, a)

            if pair_ab is None or pair_bc is None or pair_ca is None:
                continue

            # Deduplicate by asset set
            asset_key = frozenset([a, b, c])
            if asset_key in seen:
                continue
            seen.add(asset_key)

            # Forward path: A → B → C → A
            forward_legs = (
                self._build_leg(a, b, pair_ab),
                self._build_leg(b, c, pair_bc),
                self._build_leg(c, a, pair_ca),
            )

            # Reverse path: A → C → B → A
            reverse_legs = (
                self._build_leg(a, c, pair_ca),
                self._build_leg(c, b, pair_bc),
                self._build_leg(b, a, pair_ab),
            )

            triangle = Triangle(
                id=tri_id,
                assets=(a, b, c),
                forward_legs=forward_legs,
                reverse_legs=reverse_legs,
            )
            triangles.append(triangle)
            tri_id += 1

            if len(triangles) >= max_triangles:
                break

        self.triangles = triangles
        self._build_symbol_index()
        return triangles

    def _build_symbol_index(self) -> None:
        """Build reverse index: symbol → list of triangles containing it."""
        self.symbol_to_triangles.clear()
        for tri in self.triangles:
            for symbol in tri.symbols:
                self.symbol_to_triangles[symbol].append(tri)

    def get_affected_triangles(self, symbol: str) -> list[Triangle]:
        """Get all triangles affected by a price update for `symbol`."""
        return self.symbol_to_triangles.get(symbol, [])

    def get_subscribed_symbols(self) -> set[str]:
        """Get all unique symbols we need to subscribe to."""
        symbols: set[str] = set()
        for tri in self.triangles:
            symbols.update(tri.symbols)
        return symbols

    def stats(self) -> dict:
        """Summary statistics about the graph."""
        return {
            "total_assets": len(self.adjacency),
            "total_pairs": len(self.symbol_map),
            "total_triangles": len(self.triangles),
            "subscribed_symbols": len(self.get_subscribed_symbols()),
        }
