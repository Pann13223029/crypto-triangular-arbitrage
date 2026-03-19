"""Pair discovery — scans all common pairs across exchanges to find profitable spreads."""

import logging
from time import time_ns

from config.settings import FeeSchedule
from cross_exchange.pair_manager import PairCandidate

logger = logging.getLogger(__name__)


class PairDiscovery:
    """
    Scans all common USDT pairs between two exchanges via REST API
    to discover profitable cross-exchange spreads.

    Called every 30 minutes (or on emergency trigger) by the PairManager.
    """

    def __init__(
        self,
        fee_schedules: dict[str, FeeSchedule],
        min_gross_spread: float = 0.002,  # 0.2% minimum gross
        max_gross_spread: float = 0.05,  # 5% maximum (anomaly filter)
    ):
        self.fee_schedules = fee_schedules
        self.min_gross_spread = min_gross_spread
        self.max_gross_spread = max_gross_spread

    async def scan(
        self,
        exchanges: dict[str, object],
        quote_asset: str = "USDT",
    ) -> list[PairCandidate]:
        """
        Scan all common pairs across exchanges for profitable spreads.

        Uses REST API (get_ticker) — not WebSocket. Designed for periodic
        discovery, not real-time scanning.

        Returns list of PairCandidate sorted by net spread descending.
        """
        # Collect all USDT symbols per exchange
        symbols_per_exchange: dict[str, set[str]] = {}
        for ex_id, ex in exchanges.items():
            try:
                pairs = await ex.get_all_pairs()
                symbols_per_exchange[ex_id] = {
                    p.symbol for p in pairs
                    if p.quote_asset == quote_asset
                }
            except Exception as e:
                logger.warning("Failed to get pairs from %s: %s", ex_id, e)
                symbols_per_exchange[ex_id] = set()

        # Find common symbols (at least 2 exchanges)
        all_symbols: set[str] = set()
        for syms in symbols_per_exchange.values():
            all_symbols |= syms

        # For each symbol, check spreads between all exchange pairs
        candidates: list[PairCandidate] = []
        exchange_ids = list(exchanges.keys())
        scanned = 0
        errors = 0

        for symbol in sorted(all_symbols):
            # Which exchanges have this symbol?
            available_exchanges = [
                ex_id for ex_id in exchange_ids
                if symbol in symbols_per_exchange.get(ex_id, set())
            ]
            if len(available_exchanges) < 2:
                continue

            # Get ticker from each exchange
            tickers: dict[str, tuple[float, float]] = {}  # ex_id -> (bid, ask)
            for ex_id in available_exchanges:
                try:
                    ticker = await exchanges[ex_id].get_ticker(symbol)
                    if ticker.bid > 0 and ticker.ask > 0:
                        tickers[ex_id] = (ticker.bid, ticker.ask)
                except Exception:
                    errors += 1
                    continue

            if len(tickers) < 2:
                continue

            scanned += 1

            # Find best spread across all exchange pairs
            best_candidate = None
            best_net = -1

            for buy_ex in tickers:
                for sell_ex in tickers:
                    if buy_ex == sell_ex:
                        continue

                    buy_ask = tickers[buy_ex][1]  # Ask price on buy exchange
                    sell_bid = tickers[sell_ex][0]  # Bid price on sell exchange

                    if sell_bid <= buy_ask:
                        continue

                    gross = (sell_bid - buy_ask) / buy_ask

                    # Anomaly filter
                    if gross > self.max_gross_spread or gross < self.min_gross_spread:
                        continue

                    buy_fee = self.fee_schedules.get(buy_ex, FeeSchedule()).taker_fee
                    sell_fee = self.fee_schedules.get(sell_ex, FeeSchedule()).taker_fee
                    net = gross - buy_fee - sell_fee

                    if net > best_net:
                        best_net = net
                        best_candidate = PairCandidate(
                            symbol=symbol,
                            best_route=f"{buy_ex}→{sell_ex}",
                            buy_exchange=buy_ex,
                            sell_exchange=sell_ex,
                            gross_spread=gross,
                            net_spread=net,
                            price=buy_ask,
                        )

            if best_candidate and best_candidate.net_spread > 0:
                candidates.append(best_candidate)

        candidates.sort(key=lambda c: -c.net_spread)

        logger.info(
            "Discovery scan: %d symbols checked, %d profitable, %d errors",
            scanned, len(candidates), errors,
        )

        return candidates
