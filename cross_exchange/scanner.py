"""CrossExchangeScanner — finds arbitrage opportunities across exchanges."""

import logging
from time import time_ns

from config.settings import FeeSchedule
from core.models import Ticker
from cross_exchange.balance_tracker import BalanceTracker
from cross_exchange.book import CrossExchangeBook
from cross_exchange.models import CrossExchangeOpportunity, ExchangeQuote

logger = logging.getLogger(__name__)


class CrossExchangeScanner:
    """
    Scans for cross-exchange arbitrage opportunities.

    Maintains a CrossExchangeBook per symbol. On each price update
    from any exchange, checks that symbol's book for profitable spreads.

    Includes pre-flight balance check to eliminate opportunities
    that can't be executed (insufficient balance on either side).
    """

    def __init__(
        self,
        symbols: list[str],
        fee_schedules: dict[str, FeeSchedule],
        min_net_spread: float = 0.0005,
        staleness_ms: int = 1000,
        dedup_cooldown_ms: int = 3000,
        balance_tracker: BalanceTracker | None = None,
        min_trade_usd: float = 10.0,
        max_spread_anomaly: float = 0.05,
    ):
        self.fee_schedules = fee_schedules
        self.min_net_spread = min_net_spread
        self.dedup_cooldown_ms = dedup_cooldown_ms
        self.balance_tracker = balance_tracker
        self.min_trade_usd = min_trade_usd

        # One book per symbol
        self.books: dict[str, CrossExchangeBook] = {
            symbol: CrossExchangeBook(
                symbol=symbol,
                fee_schedules=fee_schedules,
                staleness_ms=staleness_ms,
                min_net_spread=min_net_spread,
                max_spread_anomaly=max_spread_anomaly,
            )
            for symbol in symbols
        }

        # Dedup: "symbol:buy:sell" -> last emission time
        self._last_emitted: dict[str, int] = {}

        # Stats
        self.total_updates: int = 0
        self.total_opportunities: int = 0
        self.total_deduped: int = 0
        self.total_preflight_rejected: int = 0

    def update(
        self, exchange_id: str, ticker: Ticker
    ) -> CrossExchangeOpportunity | None:
        """
        Process a price update from one exchange.

        Includes pre-flight balance check to reject opportunities
        that would abort at execution time.
        """
        self.total_updates += 1

        book = self.books.get(ticker.symbol)
        if book is None:
            return None

        quote = ExchangeQuote(
            exchange_id=exchange_id,
            symbol=ticker.symbol,
            bid=ticker.bid,
            ask=ticker.ask,
            timestamp_ms=ticker.timestamp_ms,
        )

        opp = book.update(quote)
        if opp is None:
            return None

        # Pre-flight balance check
        if self.balance_tracker is not None:
            quote_asset = "USDT"
            base_asset = opp.symbol.replace(quote_asset, "")

            buy_usdt = self.balance_tracker.get_balance(opp.buy_exchange, quote_asset)
            sell_base = self.balance_tracker.get_balance(opp.sell_exchange, base_asset)
            sell_base_usd = sell_base * opp.sell_price if opp.sell_price > 0 else 0

            if buy_usdt < self.min_trade_usd or sell_base_usd < self.min_trade_usd:
                self.total_preflight_rejected += 1
                return None

        # Dedup check
        key = f"{opp.symbol}:{opp.buy_exchange}:{opp.sell_exchange}"
        now_ms = time_ns() // 1_000_000
        last = self._last_emitted.get(key, 0)
        if (now_ms - last) < self.dedup_cooldown_ms:
            self.total_deduped += 1
            return None

        self._last_emitted[key] = now_ms
        self.total_opportunities += 1

        logger.info(
            "Cross-exchange: %s BUY %s @ %.4f → SELL %s @ %.4f "
            "(gross: %.4f%%, net: %.4f%%)",
            opp.symbol,
            opp.buy_exchange, opp.buy_price,
            opp.sell_exchange, opp.sell_price,
            opp.gross_spread * 100,
            opp.net_spread * 100,
        )

        return opp

    def stats(self) -> dict:
        return {
            "tracked_symbols": len(self.books),
            "total_updates": self.total_updates,
            "total_opportunities": self.total_opportunities,
            "total_deduped": self.total_deduped,
            "preflight_rejected": self.total_preflight_rejected,
        }
