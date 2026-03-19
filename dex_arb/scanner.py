"""DEX-CEX scanner — compares DEX prices against CEX prices, alerts on spreads."""

import logging
from time import time_ns

from dex_arb.dex_price_feed import DexPriceFeed
from dex_arb.models import Chain, DexCexOpportunity, DexQuote
from dex_arb.token_safety import TokenSafetyChecker

logger = logging.getLogger(__name__)


class DexCexScanner:
    """
    Scans for price discrepancies between DEX and CEX.

    Phase 1: Alert only (no execution).
    Uses DexScreener for DEX prices, existing CEX REST for CEX prices.
    """

    def __init__(
        self,
        dex_feed: DexPriceFeed,
        safety_checker: TokenSafetyChecker,
        min_spread_alert: float = 0.03,  # 3%
        max_spread: float = 0.50,  # 50% (anomaly)
        gas_estimate_usd: float = 0.10,  # BSC gas
        cex_fee: float = 0.001,  # 0.1%
    ):
        self.dex_feed = dex_feed
        self.safety_checker = safety_checker
        self.min_spread_alert = min_spread_alert
        self.max_spread = max_spread
        self.gas_estimate_usd = gas_estimate_usd
        self.cex_fee = cex_fee

        self.total_scans: int = 0
        self.total_opportunities: int = 0

    async def scan_token(
        self,
        symbol: str,
        cex_price: float,
        cex_name: str = "kucoin",
        chain: Chain = Chain.BSC,
    ) -> DexCexOpportunity | None:
        """
        Compare a single token's DEX price against CEX price.

        Returns opportunity if spread exceeds threshold.
        """
        self.total_scans += 1

        # Get DEX price
        dex_quotes = await self.dex_feed.search_token(symbol)
        if not dex_quotes:
            return None

        # Filter for target chain and best liquidity
        chain_quotes = [q for q in dex_quotes if q.chain == chain and q.liquidity_usd > 1000]
        if not chain_quotes:
            return None

        best_dex = max(chain_quotes, key=lambda q: q.liquidity_usd)

        if best_dex.price_usd <= 0 or cex_price <= 0:
            return None

        # Calculate spread both directions
        if cex_price > best_dex.price_usd:
            # Buy DEX, sell CEX
            gross = (cex_price - best_dex.price_usd) / best_dex.price_usd
            direction = "dex→cex"
            buy_price = best_dex.price_usd
            sell_price = cex_price
        else:
            # Buy CEX, sell DEX
            gross = (best_dex.price_usd - cex_price) / cex_price
            direction = "cex→dex"
            buy_price = cex_price
            sell_price = best_dex.price_usd

        # Anomaly filter
        if gross > self.max_spread:
            return None

        # Deduct estimated costs
        net = gross - self.cex_fee - (self.gas_estimate_usd / (buy_price * 100))  # gas relative to $100 position

        if net < self.min_spread_alert:
            return None

        # Safety check
        safety = None
        if best_dex.contract_address:
            safety = await self.safety_checker.check(best_dex.contract_address, chain)
            if safety.is_honeypot:
                logger.warning("HONEYPOT: %s on %s — skipping", symbol, chain.value)
                return None

        self.total_opportunities += 1

        return DexCexOpportunity(
            token=symbol,
            chain=chain,
            dex_price=best_dex.price_usd,
            cex_price=cex_price,
            dex_name=best_dex.dex,
            cex_name=cex_name,
            gross_spread=gross,
            estimated_gas=self.gas_estimate_usd,
            estimated_fees=self.cex_fee,
            net_spread=net,
            direction=direction,
            contract_address=best_dex.contract_address,
            safety=safety,
        )

    async def scan_batch(
        self,
        tokens: dict[str, float],  # symbol -> cex_price
        cex_name: str = "kucoin",
        chain: Chain = Chain.BSC,
    ) -> list[DexCexOpportunity]:
        """Scan multiple tokens against CEX prices."""
        opportunities = []
        for symbol, cex_price in tokens.items():
            opp = await self.scan_token(symbol, cex_price, cex_name, chain)
            if opp:
                opportunities.append(opp)
        opportunities.sort(key=lambda o: -o.net_spread)
        return opportunities

    def stats(self) -> dict:
        return {
            "total_scans": self.total_scans,
            "total_opportunities": self.total_opportunities,
        }
