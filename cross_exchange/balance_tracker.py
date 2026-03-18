"""BalanceTracker — tracks balances across multiple exchanges."""

import logging
from time import time_ns

from exchange.base import ExchangeBase

logger = logging.getLogger(__name__)


class BalanceTracker:
    """Aggregates and caches balances across all registered exchanges."""

    def __init__(self, exchanges: dict[str, ExchangeBase]):
        self.exchanges = exchanges
        self._cached: dict[str, dict[str, float]] = {}
        self._last_refresh_ms: dict[str, int] = {}

    async def refresh(self, exchange_id: str) -> None:
        """Refresh balances for a specific exchange."""
        ex = self.exchanges.get(exchange_id)
        if ex is None:
            return
        self._cached[exchange_id] = await ex.get_all_balances()
        self._last_refresh_ms[exchange_id] = time_ns() // 1_000_000

    async def refresh_all(self) -> None:
        """Refresh all exchange balances."""
        for ex_id in self.exchanges:
            await self.refresh(ex_id)

    def get_balance(self, exchange_id: str, asset: str) -> float:
        """Get cached balance for an exchange + asset."""
        return self._cached.get(exchange_id, {}).get(asset, 0.0)

    def get_exchange_balances(self, exchange_id: str) -> dict[str, float]:
        """Get all cached balances for one exchange."""
        return dict(self._cached.get(exchange_id, {}))

    def get_all(self) -> dict[str, dict[str, float]]:
        """All cached balances: {exchange_id: {asset: amount}}."""
        return {ex_id: dict(bals) for ex_id, bals in self._cached.items()}

    def total_balance(self, asset: str) -> float:
        """Total balance of an asset across all exchanges."""
        return sum(
            bals.get(asset, 0.0) for bals in self._cached.values()
        )

    def stats(self) -> dict:
        total_usd = sum(
            bals.get("USDT", 0.0) for bals in self._cached.values()
        )
        return {
            "exchanges": len(self._cached),
            "total_usdt": round(total_usd, 2),
            "per_exchange": {
                ex_id: {k: round(v, 8) for k, v in bals.items() if v > 1e-8}
                for ex_id, bals in self._cached.items()
            },
        }
