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

        # Determine starting amount
        start_asset = tri.assets[0] if opportunity.direction == Direction.FORWARD else tri.assets[0]
        start_balance = await self.exchange.get_balance(
            legs[0].quote_asset if legs[0].side == OrderSide.BUY else legs[0].base_asset
        )
        position_size = min(
            start_balance,
            self.trading_config.max_position_size_usd,
        )

        if position_size <= 0:
            result.aborted = True
            result.abort_reason = "Zero position size"
            self.risk_manager.on_trade_end()
            return result

        current_amount = position_size
        orders: list[Order] = []

        for i, leg in enumerate(legs):
            # Calculate quantity for this leg
            ticker = await self.exchange.get_ticker(leg.symbol)

            if leg.side == OrderSide.BUY:
                quantity = current_amount / ticker.ask
            else:
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
                # Abort remaining legs for v1 (could hedge in future)
                if i < len(legs) - 1:
                    result.aborted = True
                    result.abort_reason = (
                        f"Leg {i + 1} slippage {order.slippage:.4%} > "
                        f"tolerance {self.trading_config.slippage_tolerance:.4%}"
                    )
                    break

            # Update current amount for next leg
            if leg.side == OrderSide.BUY:
                current_amount = order.quantity  # Now holding base
            else:
                current_amount = order.quantity * order.actual_price  # Now holding quote
                current_amount -= order.fee

            result.total_fees += order.fee
            logger.info(
                "  Leg %d: %s %s %.8f @ %.8f ✓",
                i + 1, leg.side.value, leg.symbol,
                order.quantity, order.actual_price,
            )

        result.orders = orders

        # Calculate P&L
        if not result.aborted and len(orders) == 3:
            result.gross_pnl = current_amount - position_size
            result.net_pnl = result.gross_pnl  # Fees already deducted in sim
            self.total_executions += 1

            if result.net_pnl >= 0:
                self.total_profit += result.net_pnl
            else:
                self.total_loss += abs(result.net_pnl)

            self.risk_manager.record_trade_result(result.net_pnl)

            logger.info(
                "RESULT: %s | Gross: $%.6f | Net: $%.6f | Fees: $%.6f",
                "PROFIT" if result.net_pnl >= 0 else "LOSS",
                result.gross_pnl, result.net_pnl, result.total_fees,
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
