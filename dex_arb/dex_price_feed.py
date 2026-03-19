"""DEX price feeds via REST APIs (1inch, DexScreener)."""

import logging

import aiohttp

from dex_arb.models import Chain, DexQuote

logger = logging.getLogger(__name__)

DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex"


class DexPriceFeed:
    """
    Fetches DEX token prices via REST APIs.

    Uses DexScreener (free, no auth, covers all chains).
    Fallback: 1inch API for BSC/ETH price quotes.
    """

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def get_token_price(self, contract_address: str, chain: Chain) -> DexQuote | None:
        """Get token price from DexScreener by contract address."""
        session = await self._get_session()
        chain_id = {"BSC": "bsc", "ARBITRUM": "arbitrum", "ETHEREUM": "ethereum", "SOLANA": "solana"}.get(chain.value, "bsc")

        url = f"{DEXSCREENER_URL}/tokens/{contract_address}"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

            pairs = data.get("pairs", [])
            if not pairs:
                return None

            # Find the best USDT/USDC pair on the target chain
            best = None
            for pair in pairs:
                if pair.get("chainId") != chain_id:
                    continue
                quote = pair.get("quoteToken", {}).get("symbol", "")
                if quote in ("USDT", "USDC", "BUSD", "WBNB", "WETH"):
                    if best is None or float(pair.get("liquidity", {}).get("usd", 0)) > float(best.get("liquidity", {}).get("usd", 0)):
                        best = pair

            if best is None:
                return None

            return DexQuote(
                token=best.get("baseToken", {}).get("symbol", ""),
                chain=chain,
                dex=best.get("dexId", "unknown"),
                price_usd=float(best.get("priceUsd", 0)),
                liquidity_usd=float(best.get("liquidity", {}).get("usd", 0)),
                volume_24h=float(best.get("volume", {}).get("h24", 0)),
                contract_address=contract_address,
            )

        except Exception as e:
            logger.debug("DexScreener error for %s: %s", contract_address, e)
            return None

    async def search_token(self, symbol: str) -> list[DexQuote]:
        """Search for a token by symbol across all chains."""
        session = await self._get_session()
        url = f"{DEXSCREENER_URL}/search/?q={symbol}"

        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()

            results = []
            for pair in data.get("pairs", [])[:10]:
                chain_map = {"bsc": Chain.BSC, "arbitrum": Chain.ARBITRUM, "ethereum": Chain.ETHEREUM, "solana": Chain.SOLANA}
                chain_id = pair.get("chainId", "")
                chain = chain_map.get(chain_id)
                if chain is None:
                    continue

                base = pair.get("baseToken", {})
                if base.get("symbol", "").upper() != symbol.upper():
                    continue

                results.append(DexQuote(
                    token=base.get("symbol", ""),
                    chain=chain,
                    dex=pair.get("dexId", ""),
                    price_usd=float(pair.get("priceUsd", 0)),
                    liquidity_usd=float(pair.get("liquidity", {}).get("usd", 0)),
                    volume_24h=float(pair.get("volume", {}).get("h24", 0)),
                    contract_address=base.get("address", ""),
                ))

            return results

        except Exception as e:
            logger.debug("DexScreener search error: %s", e)
            return []

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
