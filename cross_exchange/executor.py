"""Cross-exchange executor — simultaneous limit orders with state machine."""

import asyncio
import logging
from time import time_ns

from config.settings import CrossExchangeConfig, TradingConfig
from core.models import Order, OrderSide, OrderStatus
from cross_exchange.models import (
    CrossExchangeOpportunity,
    CrossTradeResult,
    CrossTradeStatus,
)
from exchange.base import ExchangeBase

logger = logging.getLogger(__name__)


class CrossExchangeExecutor:
    """
    Executes cross-exchange arbitrage trades.

    Strategy: simultaneous limit orders on both exchanges.
    If one side fills but the other fails, emergency hedge.
    """

    def __init__(
        self,
        exchanges: dict[str, ExchangeBase],
        trading_config: TradingConfig | None = None,
        cx_config: CrossExchangeConfig | None = None,
    ):
        self.exchanges = exchanges
        self.trading_config = trading_config or TradingConfig()
        self.cx_config = cx_config or CrossExchangeConfig()

        # Stats
        self.total_executions = 0
        self.total_both_filled = 0
        self.total_partial = 0
        self.total_aborts = 0
        self.total_emergency_hedges = 0
        self.total_profit = 0.0
        self.total_loss = 0.0

    async def _get_usd_value(self, exchange: ExchangeBase, asset: str, amount: float) -> float:
        """Convert an asset amount to approximate USD value."""
        if asset in ("USDT", "BUSD", "USDC", "FDUSD"):
            return amount
        try:
            ticker = await exchange.get_ticker(f"{asset}USDT")
            return amount * ticker.mid
        except (ValueError, KeyError):
            return amount

    async def execute(self, opportunity: CrossExchangeOpportunity) -> CrossTradeResult:
        """
        Execute a cross-exchange arbitrage trade.

        1. Determine position size (USD-capped, balance-checked)
        2. Send simultaneous buy + sell orders
        3. Handle fills, partial fills, and failures
        """
        result = CrossTradeResult(opportunity=opportunity)

        buy_exchange = self.exchanges.get(opportunity.buy_exchange)
        sell_exchange = self.exchanges.get(opportunity.sell_exchange)

        if buy_exchange is None or sell_exchange is None:
            result.status = CrossTradeStatus.FAILED
            logger.warning("Exchange not found: %s or %s",
                           opportunity.buy_exchange, opportunity.sell_exchange)
            self.total_aborts += 1
            return result

        # --- Position sizing ---
        symbol = opportunity.symbol
        # Determine base and quote from the symbol (e.g., BTCUSDT -> BTC, USDT)
        # For USDT-quoted pairs, quote=USDT, base=everything else
        quote_asset = "USDT"
        base_asset = symbol.replace(quote_asset, "")

        # Check balances: need USDT on buy side, base asset on sell side
        buy_quote_balance = await buy_exchange.get_balance(quote_asset)
        sell_base_balance = await sell_exchange.get_balance(base_asset)

        buy_quote_usd = buy_quote_balance  # Already USDT
        sell_base_usd = await self._get_usd_value(sell_exchange, base_asset, sell_base_balance)

        max_usd = self.cx_config.max_position_size_usd
        available_usd = min(buy_quote_usd, sell_base_usd, max_usd)

        if available_usd < 1.0:  # Minimum $1
            result.status = CrossTradeStatus.FAILED
            logger.warning(
                "Insufficient balance: buy=%s $%.2f, sell=%s $%.2f %s",
                opportunity.buy_exchange, buy_quote_usd,
                opportunity.sell_exchange, sell_base_usd, base_asset,
            )
            self.total_aborts += 1
            return result

        # Calculate quantity in base asset
        quantity = available_usd / opportunity.buy_price

        logger.info(
            "CROSS-EXEC %s: BUY %s @ %.4f on %s → SELL @ %.4f on %s "
            "(qty: %.6f %s, ~$%.2f, net: %.4f%%)",
            symbol,
            base_asset, opportunity.buy_price, opportunity.buy_exchange,
            opportunity.sell_price, opportunity.sell_exchange,
            quantity, base_asset, available_usd,
            opportunity.net_spread * 100,
        )

        result.status = CrossTradeStatus.ORDERS_SENT

        # --- Simultaneous execution ---
        buy_task = asyncio.create_task(
            buy_exchange.place_order(
                symbol=symbol,
                side=OrderSide.BUY,
                quantity=quantity,
            )
        )
        sell_task = asyncio.create_task(
            sell_exchange.place_order(
                symbol=symbol,
                side=OrderSide.SELL,
                quantity=quantity,
            )
        )

        buy_order, sell_order = await asyncio.gather(
            buy_task, sell_task, return_exceptions=True
        )

        # Handle exceptions
        if isinstance(buy_order, Exception):
            logger.error("Buy order exception: %s", buy_order)
            buy_order = Order(status=OrderStatus.FAILED, symbol=symbol, side=OrderSide.BUY)
        if isinstance(sell_order, Exception):
            logger.error("Sell order exception: %s", sell_order)
            sell_order = Order(status=OrderStatus.FAILED, symbol=symbol, side=OrderSide.SELL)

        result.buy_order = buy_order
        result.sell_order = sell_order

        buy_filled = buy_order.status == OrderStatus.FILLED
        sell_filled = sell_order.status == OrderStatus.FILLED

        # --- Outcome handling ---
        if buy_filled and sell_filled:
            # Perfect execution
            result.status = CrossTradeStatus.BOTH_FILLED
            self.total_both_filled += 1

            buy_cost = buy_order.quantity * buy_order.actual_price + buy_order.fee
            sell_revenue = sell_order.quantity * sell_order.actual_price - sell_order.fee
            result.gross_pnl = sell_revenue - buy_cost
            result.net_pnl = result.gross_pnl
            result.total_fees = buy_order.fee + sell_order.fee

            logger.info(
                "  BOTH FILLED: buy @ %.4f, sell @ %.4f → P&L: $%.4f (fees: $%.4f)",
                buy_order.actual_price, sell_order.actual_price,
                result.net_pnl, result.total_fees,
            )

        elif buy_filled and not sell_filled:
            # Buy succeeded, sell failed — EMERGENCY: sell on buy exchange
            result.status = CrossTradeStatus.BUY_ONLY
            logger.warning("  BUY FILLED, SELL FAILED — emergency hedge")

            hedge = await self._emergency_hedge(
                buy_exchange, symbol, OrderSide.SELL, buy_order.quantity
            )
            result.hedge_order = hedge
            result.status = CrossTradeStatus.HEDGING

            if hedge.status == OrderStatus.FILLED:
                # Hedged — calculate loss
                buy_cost = buy_order.quantity * buy_order.actual_price + buy_order.fee
                hedge_revenue = hedge.quantity * hedge.actual_price - hedge.fee
                result.net_pnl = hedge_revenue - buy_cost
                result.total_fees = buy_order.fee + hedge.fee
                result.status = CrossTradeStatus.COMPLETED
            else:
                result.status = CrossTradeStatus.FAILED
                result.net_pnl = -(buy_order.quantity * buy_order.actual_price)
                logger.error("  HEDGE FAILED — unhedged exposure!")

            self.total_emergency_hedges += 1

        elif not buy_filled and sell_filled:
            # Sell succeeded, buy failed — EMERGENCY: buy on sell exchange
            result.status = CrossTradeStatus.SELL_ONLY
            logger.warning("  SELL FILLED, BUY FAILED — emergency hedge")

            hedge = await self._emergency_hedge(
                sell_exchange, symbol, OrderSide.BUY, sell_order.quantity
            )
            result.hedge_order = hedge
            result.status = CrossTradeStatus.HEDGING

            if hedge.status == OrderStatus.FILLED:
                hedge_cost = hedge.quantity * hedge.actual_price + hedge.fee
                sell_revenue = sell_order.quantity * sell_order.actual_price - sell_order.fee
                result.net_pnl = sell_revenue - hedge_cost
                result.total_fees = sell_order.fee + hedge.fee
                result.status = CrossTradeStatus.COMPLETED
            else:
                result.status = CrossTradeStatus.FAILED
                result.net_pnl = -(sell_order.quantity * sell_order.actual_price)
                logger.error("  HEDGE FAILED — unhedged exposure!")

            self.total_emergency_hedges += 1

        else:
            # Neither filled — no exposure
            result.status = CrossTradeStatus.NEITHER
            result.net_pnl = 0.0
            logger.info("  NEITHER FILLED — no exposure")

        # --- Record P&L ---
        self.total_executions += 1
        if result.net_pnl >= 0:
            self.total_profit += result.net_pnl
        else:
            self.total_loss += abs(result.net_pnl)

        if result.status == CrossTradeStatus.BOTH_FILLED:
            pnl_pct = (result.net_pnl / available_usd * 100) if available_usd > 0 else 0
            logger.info(
                "  RESULT: %s $%.4f (%.4f%%) | Fees: $%.4f",
                "PROFIT" if result.net_pnl >= 0 else "LOSS",
                result.net_pnl, pnl_pct, result.total_fees,
            )

        result.status = CrossTradeStatus.COMPLETED
        return result

    async def _emergency_hedge(
        self, exchange: ExchangeBase, symbol: str, side: OrderSide, quantity: float
    ) -> Order:
        """Place an emergency market order to close exposure."""
        logger.warning(
            "  EMERGENCY HEDGE: %s %s %.6f on %s",
            side.value, symbol, quantity, exchange.exchange_id,
        )
        return await exchange.place_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
        )

    def stats(self) -> dict:
        net_pnl = self.total_profit - self.total_loss
        return {
            "total_executions": self.total_executions,
            "both_filled": self.total_both_filled,
            "partial_fills": self.total_partial,
            "aborts": self.total_aborts,
            "emergency_hedges": self.total_emergency_hedges,
            "total_profit": round(self.total_profit, 4),
            "total_loss": round(self.total_loss, 4),
            "net_pnl": round(net_pnl, 4),
            "win_rate": (
                f"{self.total_both_filled / max(self.total_executions, 1) * 100:.1f}%"
            ),
        }
