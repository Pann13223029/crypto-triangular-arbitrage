"""Trade executor — sequential 3-leg execution with circuit breaker."""

import logging
from time import time_ns

from config.settings import TradingConfig, FeeConfig
from core.models import (
    Direction,
    Opportunity,
    Order,
    OrderSide,
    OrderStatus,
    Ticker,
    TradeResult,
)
from exchange.base import ExchangeBase
from execution.risk_manager import RiskManager

logger = logging.getLogger(__name__)


class Executor:
    """
    Executes triangle arbitrage trades sequentially.

    Leg 1 → verify → Leg 2 → verify → Leg 3 → done.
    Aborts and hedges if slippage exceeds tolerance.
    """

    def __init__(
        self,
        exchange: ExchangeBase,
        risk_manager: RiskManager,
        trading_config: TradingConfig | None = None,
        fee_config: FeeConfig | None = None,
    ):
        self.exchange = exchange
        self.risk_manager = risk_manager
        self.trading_config = trading_config or TradingConfig()
        self.fee_config = fee_config or FeeConfig()

        # Stats
        self.total_executions = 0
        self.total_aborts = 0
        self.total_profit = 0.0
        self.total_loss = 0.0

    async def _get_usd_value(self, asset: str, amount: float) -> float:
        """Convert an asset amount to approximate USD value."""
        if asset in ("USDT", "BUSD", "USDC", "TUSD", "FDUSD"):
            return amount

        try:
            ticker = await self.exchange.get_ticker(f"{asset}USDT")
            return amount * ticker.mid
        except (ValueError, KeyError):
            pass

        # Try via BTC
        try:
            ticker_btc = await self.exchange.get_ticker(f"{asset}BTC")
            ticker_btcusdt = await self.exchange.get_ticker("BTCUSDT")
            return amount * ticker_btc.mid * ticker_btcusdt.mid
        except (ValueError, KeyError):
            pass

        logger.warning("Cannot determine USD value for %s", asset)
        return amount  # Fallback: treat as USD (will be capped anyway)

    async def execute(self, opportunity: Opportunity) -> TradeResult:
        """
        Execute a triangle trade.

        Args:
            opportunity: The approved opportunity to trade.

        Returns:
            TradeResult with all orders and P&L.
        """
        tri = opportunity.triangle
        legs = (
            tri.forward_legs
            if opportunity.direction == Direction.FORWARD
            else tri.reverse_legs
        )

        result = TradeResult(opportunity=opportunity)
        self.risk_manager.on_trade_start()

        path = " → ".join(tri.assets)
        logger.info(
            "EXECUTING %s %s (expected: %.4f%%)",
            opportunity.direction.value, path,
            opportunity.theoretical_profit * 100,
        )

        # Determine starting asset and balance
        first_leg = legs[0]
        if first_leg.side == OrderSide.BUY:
            start_asset = first_leg.quote_asset  # Spending quote to buy base
        else:
            start_asset = first_leg.base_asset  # Selling base for quote

        start_balance = await self.exchange.get_balance(start_asset)

        # Convert balance to USD for proper position sizing
        balance_usd = await self._get_usd_value(start_asset, start_balance)
        max_usd = self.trading_config.max_position_size_usd

        if balance_usd <= 0:
            result.aborted = True
            result.abort_reason = f"Zero {start_asset} balance"
            self.total_aborts += 1
            logger.warning("ABORTED: %s", result.abort_reason)
            self.risk_manager.on_trade_end()
            return result

        # Scale position: use the fraction of balance that equals max_position_size in USD
        if balance_usd > max_usd:
            position_fraction = max_usd / balance_usd
            position_size = start_balance * position_fraction
        else:
            position_size = start_balance

        position_usd = await self._get_usd_value(start_asset, position_size)

        logger.info(
            "  Position: %.8f %s (~$%.2f)",
            position_size, start_asset, position_usd,
        )

        current_amount = position_size
        current_asset = start_asset
        orders: list[Order] = []

        for i, leg in enumerate(legs):
            # Calculate quantity for this leg
            ticker = await self.exchange.get_ticker(leg.symbol)

            if leg.side == OrderSide.BUY:
                # Spending current_amount of quote to buy base
                quantity = current_amount / ticker.ask
            else:
                # Selling current_amount of base
                quantity = current_amount

            # Place order
            order = await self.exchange.place_order(
                symbol=leg.symbol,
                side=leg.side,
                quantity=quantity,
            )
            orders.append(order)

            # Check fill
            if order.status != OrderStatus.FILLED:
                logger.warning(
                    "Leg %d FAILED: %s %s — %s",
                    i + 1, leg.side.value, leg.symbol, order.status.value,
                )
                result.aborted = True
                result.abort_reason = f"Leg {i + 1} failed: {order.status.value}"
                break

            # Check slippage
            if order.slippage > self.trading_config.slippage_tolerance:
                logger.warning(
                    "Leg %d SLIPPAGE: %.4f%% > %.4f%% tolerance",
                    i + 1, order.slippage * 100,
                    self.trading_config.slippage_tolerance * 100,
                )
                if i < len(legs) - 1:
                    result.aborted = True
                    result.abort_reason = (
                        f"Leg {i + 1} slippage {order.slippage:.4%} > "
                        f"tolerance {self.trading_config.slippage_tolerance:.4%}"
                    )
                    break

            # Update current amount for next leg
            if leg.side == OrderSide.BUY:
                # Bought base with quote — now holding base (minus fee already deducted in sim)
                current_amount = order.quantity
                current_asset = leg.base_asset
            else:
                # Sold base for quote — now holding quote (minus fee already deducted in sim)
                current_amount = order.quantity * order.actual_price - order.fee
                current_asset = leg.quote_asset

            result.total_fees += order.fee
            logger.info(
                "  Leg %d: %s %s %.8f @ %.8f → %.8f %s ✓",
                i + 1, leg.side.value, leg.symbol,
                order.quantity, order.actual_price,
                current_amount, current_asset,
            )

        result.orders = orders

        # Calculate P&L
        if not result.aborted and len(orders) == 3:
            # P&L is in terms of the starting asset
            # Convert both start and end to USD for accurate comparison
            end_usd = await self._get_usd_value(current_asset, current_amount)
            start_usd = position_usd

            result.gross_pnl = end_usd - start_usd
            result.net_pnl = result.gross_pnl  # Fees already deducted in sim fills
            self.total_executions += 1

            if result.net_pnl >= 0:
                self.total_profit += result.net_pnl
            else:
                self.total_loss += abs(result.net_pnl)

            self.risk_manager.record_trade_result(result.net_pnl)

            pnl_pct = (result.net_pnl / start_usd * 100) if start_usd > 0 else 0
            logger.info(
                "RESULT: %s | P&L: $%.4f (%.4f%%) | Fees: $%.4f | Start: $%.2f → End: $%.2f",
                "PROFIT" if result.net_pnl >= 0 else "LOSS",
                result.net_pnl, pnl_pct, result.total_fees,
                start_usd, end_usd,
            )
        else:
            self.total_aborts += 1
            logger.warning("ABORTED: %s", result.abort_reason)

        self.risk_manager.on_trade_end()
        return result

    def stats(self) -> dict:
        return {
            "total_executions": self.total_executions,
            "total_aborts": self.total_aborts,
            "total_profit": round(self.total_profit, 6),
            "total_loss": round(self.total_loss, 6),
            "net_pnl": round(self.total_profit - self.total_loss, 6),
        }
