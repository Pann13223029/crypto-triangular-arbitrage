"""Funding rate scanner — scans all KuCoin perps for funding rate spikes."""

import logging
from time import time_ns

import aiohttp

from funding_arb.models import FundingDirection, FundingOpportunity

logger = logging.getLogger(__name__)

KUCOIN_FUTURES_URL = "https://api-futures.kucoin.com"


class FundingScanner:
    """
    Scans all KuCoin USDT perpetual contracts for funding rate spikes.

    Designed to run every 8 hours (before funding timestamps) or on demand.
    Returns ranked opportunities above the entry threshold.
    """

    def __init__(
        self,
        min_funding_rate: float = 0.001,  # 0.10% per 8h
        max_funding_rate: float = 0.03,  # 3% — anomaly filter
    ):
        self.min_funding_rate = min_funding_rate
        self.max_funding_rate = max_funding_rate

        # Cached contract info
        self.contracts: dict[str, dict] = {}

        # Stats
        self.total_scans: int = 0
        self.total_opportunities: int = 0

    async def scan(self) -> list[FundingOpportunity]:
        """
        Scan all KuCoin USDT perps for funding rate spikes.

        Returns opportunities sorted by absolute funding rate (highest first).
        """
        session = aiohttp.ClientSession()

        try:
            # Get active contracts
            url = f"{KUCOIN_FUTURES_URL}/api/v1/contracts/active"
            async with session.get(url) as resp:
                data = await resp.json()
                contracts = data.get("data", [])

            usdt_perps = [
                c for c in contracts
                if c.get("quoteCurrency") == "USDT"
                and not c.get("isInverse")
                and c.get("status") == "Open"
            ]

            # Cache contract info
            for c in usdt_perps:
                self.contracts[c["symbol"]] = c

            # Fetch funding rates
            opportunities: list[FundingOpportunity] = []

            for c in usdt_perps:
                sym = c["symbol"]
                try:
                    url2 = f"{KUCOIN_FUTURES_URL}/api/v1/funding-rate/{sym}/current"
                    async with session.get(url2) as resp:
                        data = await resp.json()
                        d = data.get("data", {})

                    rate = float(d.get("value", 0))
                    predicted = float(d.get("predictedValue", 0))
                    abs_rate = abs(rate)

                    # Filter
                    if abs_rate < self.min_funding_rate:
                        continue
                    if abs_rate > self.max_funding_rate:
                        continue

                    # Determine direction
                    direction = (
                        FundingDirection.LONGS_PAY
                        if rate > 0
                        else FundingDirection.SHORTS_PAY
                    )

                    # Extract base asset from symbol (LRCUSDTM → LRC)
                    base = sym.replace("USDTM", "")

                    opp = FundingOpportunity(
                        symbol=sym,
                        base_asset=base,
                        funding_rate=rate,
                        predicted_rate=predicted,
                        direction=direction,
                        daily_rate=rate * 3,
                        annualized=rate * 3 * 365,
                    )
                    opportunities.append(opp)

                except Exception as e:
                    logger.debug("Failed to get funding for %s: %s", sym, e)

            # Sort by absolute rate
            opportunities.sort(key=lambda o: -o.abs_rate)

            self.total_scans += 1
            self.total_opportunities = len(opportunities)

            logger.info(
                "Funding scan: %d perps, %d above %.2f%% threshold",
                len(usdt_perps), len(opportunities), self.min_funding_rate * 100,
            )

        finally:
            await session.close()

        return opportunities

    def get_contract_info(self, symbol: str) -> dict:
        """Get cached contract details (multiplier, lot size, etc.)."""
        return self.contracts.get(symbol, {})

    def stats(self) -> dict:
        return {
            "total_scans": self.total_scans,
            "contracts_cached": len(self.contracts),
            "last_opportunities": self.total_opportunities,
        }
