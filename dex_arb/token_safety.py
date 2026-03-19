"""Token safety checker via GoPlus Security API."""

import logging

import aiohttp

from dex_arb.models import Chain, TokenSafety, TokenSafetyLevel

logger = logging.getLogger(__name__)

GOPLUS_URL = "https://api.gopluslabs.ai/api/v1"


class TokenSafetyChecker:
    """
    Checks token safety using GoPlus Security API.

    Free, no auth required. Covers BSC, ETH, Arbitrum.
    Detects: honeypots, high tax tokens, proxy contracts, etc.
    """

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, TokenSafety] = {}  # contract_address -> result

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def check(self, contract_address: str, chain: Chain) -> TokenSafety:
        """Check token safety. Results are cached."""
        cache_key = f"{chain.value}:{contract_address}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        chain_id = {"BSC": "56", "ETHEREUM": "1", "ARBITRUM": "42161"}.get(chain.value, "56")
        session = await self._get_session()

        try:
            url = f"{GOPLUS_URL}/token_security/{chain_id}"
            async with session.get(url, params={"contract_addresses": contract_address}) as resp:
                if resp.status != 200:
                    return self._unknown(contract_address, chain)
                data = await resp.json()

            result = data.get("result", {}).get(contract_address.lower(), {})
            if not result:
                return self._unknown(contract_address, chain)

            is_honeypot = result.get("is_honeypot", "0") == "1"
            is_open_source = result.get("is_open_source", "0") == "1"
            has_proxy = result.get("is_proxy", "0") == "1"
            buy_tax = float(result.get("buy_tax", 0))
            sell_tax = float(result.get("sell_tax", 0))

            # Calculate safety score
            score = 100
            if is_honeypot:
                score = 0
            else:
                if not is_open_source:
                    score -= 30
                if has_proxy:
                    score -= 15
                if sell_tax > 0.05:
                    score -= 25
                elif sell_tax > 0.01:
                    score -= 10
                if buy_tax > 0.05:
                    score -= 20
                elif buy_tax > 0.01:
                    score -= 5

            if score >= 80:
                level = TokenSafetyLevel.SAFE
            elif score >= 50:
                level = TokenSafetyLevel.CAUTION
            else:
                level = TokenSafetyLevel.DANGEROUS

            safety = TokenSafety(
                token="",
                chain=chain,
                contract_address=contract_address,
                is_honeypot=is_honeypot,
                is_open_source=is_open_source,
                has_proxy=has_proxy,
                buy_tax=buy_tax,
                sell_tax=sell_tax,
                safety_score=max(0, score),
                level=level,
            )

            self._cache[cache_key] = safety

            if is_honeypot:
                logger.warning("HONEYPOT detected: %s on %s", contract_address, chain.value)
            elif level == TokenSafetyLevel.DANGEROUS:
                logger.warning("Dangerous token: %s score=%d", contract_address, score)

            return safety

        except Exception as e:
            logger.debug("GoPlus check failed: %s", e)
            return self._unknown(contract_address, chain)

    def _unknown(self, address: str, chain: Chain) -> TokenSafety:
        return TokenSafety(
            token="", chain=chain, contract_address=address,
            level=TokenSafetyLevel.UNKNOWN,
        )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
