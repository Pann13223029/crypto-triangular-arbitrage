"""Tests for DEX-CEX arb models and scanner."""

import pytest

from dex_arb.models import (
    Chain,
    DexCexOpportunity,
    DexQuote,
    TokenSafety,
    TokenSafetyLevel,
)


class TestDexModels:

    def test_dex_quote(self):
        q = DexQuote(
            token="CAKE",
            chain=Chain.BSC,
            dex="pancakeswap",
            price_usd=2.50,
            liquidity_usd=500000,
        )
        assert q.token == "CAKE"
        assert q.chain == Chain.BSC

    def test_token_safety_safe(self):
        s = TokenSafety(
            token="CAKE", chain=Chain.BSC, contract_address="0x...",
            is_honeypot=False, is_open_source=True, has_proxy=False,
            buy_tax=0.0, sell_tax=0.0, safety_score=95,
            level=TokenSafetyLevel.SAFE,
        )
        assert s.is_safe

    def test_token_safety_honeypot(self):
        s = TokenSafety(
            token="SCAM", chain=Chain.BSC, contract_address="0x...",
            is_honeypot=True, safety_score=0,
            level=TokenSafetyLevel.DANGEROUS,
        )
        assert not s.is_safe

    def test_token_safety_high_tax(self):
        s = TokenSafety(
            token="TAX", chain=Chain.BSC, contract_address="0x...",
            is_honeypot=False, sell_tax=0.10, safety_score=60,
            level=TokenSafetyLevel.CAUTION,
        )
        assert not s.is_safe  # 10% sell tax

    def test_opportunity(self):
        opp = DexCexOpportunity(
            token="CAKE",
            chain=Chain.BSC,
            dex_price=2.40,
            cex_price=2.55,
            dex_name="pancakeswap",
            cex_name="kucoin",
            gross_spread=0.0625,
            net_spread=0.05,
            direction="dex→cex",
        )
        assert opp.gross_spread > opp.net_spread
        assert opp.direction == "dex→cex"
