"""CrossExchangeBook — aggregated order book for one symbol across exchanges."""

import logging
from time import time_ns

from config.settings import FeeSchedule
from cross_exchange.models import CrossExchangeOpportunity, ExchangeQuote

logger = logging.getLogger(__name__)


class CrossExchangeBook:
    """
    Aggregated best bid/ask across exchanges for a single symbol.

    On each price update, checks if the lowest ask and highest bid
    are on different exchanges with a profitable spread after fees.
    """

    def __init__(
        self,
        symbol: str,
        fee_schedules: dict[str, FeeSchedule],
        staleness_ms: int = 1000,
        min_net_spread: float = 0.0005,
        max_spread_anomaly: float = 0.05,
    ):
        self.symbol = symbol
        self.fee_schedules = fee_schedules
        self.staleness_ms = staleness_ms
        self.min_net_spread = min_net_spread
        self.max_spread_anomaly = max_spread_anomaly

        # exchange_id -> ExchangeQuote
        self.quotes: dict[str, ExchangeQuote] = {}

    def update(self, quote: ExchangeQuote) -> CrossExchangeOpportunity | None:
        """
        Update a quote and check for opportunity.

        Returns a CrossExchangeOpportunity if a profitable spread
        exists between two different exchanges, else None.
        """
        self.quotes[quote.exchange_id] = quote

        # Need at least 2 exchanges
        fresh = self._fresh_quotes()
        if len(fresh) < 2:
            return None

        # Find best buy (lowest ask) and best sell (highest bid)
        best_buy = min(fresh.values(), key=lambda q: q.ask)
        best_sell = max(fresh.values(), key=lambda q: q.bid)

        # Must be on different exchanges
        if best_buy.exchange_id == best_sell.exchange_id:
            return None

        # Must have positive spread
        if best_sell.bid <= best_buy.ask:
            return None

        # Calculate spreads
        gross_spread = (best_sell.bid - best_buy.ask) / best_buy.ask

        # Anomaly filter: reject impossibly wide spreads (likely stale/delisted)
        if gross_spread > self.max_spread_anomaly:
            logger.debug(
                "Anomaly: %s spread %.2f%% > %.2f%% max — likely stale price",
                self.symbol, gross_spread * 100, self.max_spread_anomaly * 100,
            )
            return None

        buy_fee = self.fee_schedules.get(
            best_buy.exchange_id, FeeSchedule()
        ).taker_fee
        sell_fee = self.fee_schedules.get(
            best_sell.exchange_id, FeeSchedule()
        ).taker_fee
        net_spread = gross_spread - buy_fee - sell_fee

        if net_spread < self.min_net_spread:
            return None

        # Quantity limited by available depth at best price
        max_qty = min(
            best_buy.ask_qty if best_buy.ask_qty > 0 else float("inf"),
            best_sell.bid_qty if best_sell.bid_qty > 0 else float("inf"),
        )

        return CrossExchangeOpportunity(
            symbol=self.symbol,
            buy_exchange=best_buy.exchange_id,
            sell_exchange=best_sell.exchange_id,
            buy_price=best_buy.ask,
            sell_price=best_sell.bid,
            gross_spread=gross_spread,
            net_spread=net_spread,
            max_quantity=max_qty,
        )

    def _fresh_quotes(self) -> dict[str, ExchangeQuote]:
        """Return only quotes that are not stale."""
        now = time_ns() // 1_000_000
        return {
            ex_id: q
            for ex_id, q in self.quotes.items()
            if (now - q.timestamp_ms) < self.staleness_ms
            and q.bid > 0
            and q.ask > 0
        }

    def spread_summary(self) -> dict | None:
        """Current spread info (for dashboard)."""
        fresh = self._fresh_quotes()
        if len(fresh) < 2:
            return None

        best_buy = min(fresh.values(), key=lambda q: q.ask)
        best_sell = max(fresh.values(), key=lambda q: q.bid)

        if best_buy.exchange_id == best_sell.exchange_id:
            return None

        gross = (best_sell.bid - best_buy.ask) / best_buy.ask
        return {
            "symbol": self.symbol,
            "buy_exchange": best_buy.exchange_id,
            "buy_price": best_buy.ask,
            "sell_exchange": best_sell.exchange_id,
            "sell_price": best_sell.bid,
            "gross_spread": gross,
            "exchanges_tracked": len(fresh),
        }
