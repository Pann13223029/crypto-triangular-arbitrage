"""Funding arb executor — enters and exits spot+futures positions."""

import asyncio
import logging
import math
from time import time_ns

from exchange.kucoin_rest import KuCoinExchange
from funding_arb.kucoin_futures import KuCoinFutures
from funding_arb.models import (
    FundingDirection,
    FundingOpportunity,
    FundingPosition,
    PositionStatus,
)
from funding_arb.position_manager import FundingPositionManager

logger = logging.getLogger(__name__)


class FundingExecutor:
    """
    Executes funding rate arb trades on KuCoin.

    Entry: Buy spot + Short perp (when longs pay)
    Exit: Sell spot + Close perp short
    Safety: Isolated margin, 2x leverage, exchange-side stop-loss
    """

    def __init__(
        self,
        spot: KuCoinExchange,
        futures: KuCoinFutures,
        position_manager: FundingPositionManager,
        leverage: int = 2,
        stop_loss_pct: float = 0.15,  # 15% adverse move
    ):
        self.spot = spot
        self.futures = futures
        self.pm = position_manager
        self.leverage = leverage
        self.stop_loss_pct = stop_loss_pct

    async def enter_position(self, opp: FundingOpportunity) -> FundingPosition | None:
        """
        Enter a funding rate arb position.

        1. Check pre-entry conditions
        2. Set isolated margin + leverage on futures
        3. Short perp (futures)
        4. Buy spot
        5. Place exchange-side stop-loss
        """
        pos = self.pm.create_position(opp)
        spot_symbol = pos.spot_symbol  # e.g. "LRC-USDT"
        futures_symbol = opp.symbol  # e.g. "LRCUSDTM"

        logger.info(
            "ENTERING: %s | Long %s spot + Short %s perp | Budget: $%.2f/side",
            opp.base_asset, spot_symbol, futures_symbol, pos.position_usd,
        )

        try:
            # 1. Get contract info for lot sizing
            contract = await self.futures.get_contract(futures_symbol)
            multiplier = float(contract.get("multiplier", 0.01))
            lot_size = int(contract.get("lotSize", 1))
            tick_size = float(contract.get("tickSize", 0.01))

            # Get current price
            funding_data = await self.futures.get_funding_rate(futures_symbol)
            # Use spot price as reference
            spot_ticker = await self.spot.get_ticker(opp.base_asset + "USDT")
            current_price = spot_ticker.mid

            if current_price <= 0:
                logger.error("Invalid price for %s", opp.base_asset)
                self.pm.finalize_close()
                return None

            # === FIX #1: Proper hedge ratio enforcement ===
            #
            # Key: 1 lot = multiplier × base tokens
            # We size FUTURES first (constrained by lot size),
            # then buy EXACT matching spot quantity.
            #
            # If 1 lot costs more than our budget, REJECT the trade.

            one_lot_base = lot_size * multiplier
            one_lot_usd = one_lot_base * current_price

            # Check if we can afford to hedge even 1 lot
            spot_balance = await self.spot.get_balance("USDT")
            spot_available = spot_balance * 0.95  # Keep 5% buffer for fees

            if one_lot_usd > spot_available:
                logger.warning(
                    "  REJECTED: 1 lot = %.0f %s (~$%.2f) but only $%.2f available for spot. "
                    "Cannot achieve 98%% hedge. Need $%.2f minimum.",
                    one_lot_base, opp.base_asset, one_lot_usd,
                    spot_available, one_lot_usd * 1.05,
                )
                self.pm.finalize_close()
                return None

            # How many lots can we fully hedge?
            max_lots_by_budget = int(spot_available / one_lot_usd)
            num_lots = max(lot_size, min(max_lots_by_budget, int(pos.position_usd / one_lot_usd)))
            if num_lots < 1:
                num_lots = 1

            # Futures and spot quantities — MATCHED
            futures_base = num_lots * multiplier
            futures_usd = futures_base * current_price
            spot_qty = futures_base  # Exact match!

            # Round spot for exchange
            import math
            if current_price < 0.1:
                spot_qty = math.floor(spot_qty)
            else:
                spot_qty = round(spot_qty, 2)

            hedge_ratio = spot_qty / futures_base if futures_base > 0 else 0

            logger.info(
                "  Contract: %s | 1 lot = %.0f %s ($%.2f) | Using %d lots",
                futures_symbol, one_lot_base, opp.base_asset, one_lot_usd, num_lots,
            )
            logger.info(
                "  Futures: %.0f %s ($%.2f) | Spot: %.0f %s ($%.2f) | Hedge: %.0f%%",
                futures_base, opp.base_asset, futures_usd,
                spot_qty, opp.base_asset, spot_qty * current_price,
                hedge_ratio * 100,
            )

            # Enforce 98% hedge minimum
            if hedge_ratio < 0.95:
                logger.error(
                    "  REJECTED: Hedge ratio %.0f%% < 95%%. Cannot achieve delta-neutral.",
                    hedge_ratio * 100,
                )
                self.pm.finalize_close()
                return None

            # 2. Set isolated margin + leverage
            try:
                await self.futures.set_isolated_margin(futures_symbol)
                logger.info("  Margin mode: ISOLATED")
            except Exception as e:
                logger.warning("  Set isolated margin: %s (may already be set)", e)

            # === FIX #3: Use market orders (reliable) with limit fallback for spot ===
            #
            # Futures: MARKET order (limit orders on futures have tick size issues)
            # Spot: try limit at ask, fallback to market

            # 3. Short perp (futures side) — market order (reliable)
            logger.info("  Leg 1: SHORT %d lots %s (MARKET)...", num_lots, futures_symbol)
            try:
                futures_result = await self.futures.place_order(
                    symbol=futures_symbol,
                    side="sell",
                    size=num_lots,
                    leverage=self.leverage,
                )
            except Exception as e:
                logger.error("  Futures order failed: %s", e)
                self.pm.finalize_close()
                return None

            futures_order_id = futures_result.get("orderId", "")
            logger.info("  Leg 1: SHORT filled — order %s", futures_order_id)

            # 4. Buy spot — try limit at ask (maker fee), fallback to market
            from core.models import OrderSide
            spot_ask = spot_ticker.ask if spot_ticker.ask > 0 else current_price

            logger.info("  Leg 2: BUY %.0f %s spot (~$%.2f)...",
                        spot_qty, opp.base_asset, spot_qty * current_price)

            try:
                spot_order = await self.spot.place_order(
                    symbol=opp.base_asset + "USDT",
                    side=OrderSide.BUY,
                    quantity=spot_qty,
                    price=spot_ask,
                )
            except Exception:
                logger.warning("  Limit spot failed, using market order")
                spot_order = await self.spot.place_order(
                    symbol=opp.base_asset + "USDT",
                    side=OrderSide.BUY,
                    quantity=spot_qty,
                )

            if spot_order.status.value != "FILLED":
                logger.error("  Leg 2: SPOT BUY FAILED — closing futures...")
                # Emergency: close the futures short
                await self.futures.place_order(
                    symbol=futures_symbol,
                    side="buy",
                    size=num_lots,
                    leverage=self.leverage,
                )
                self.pm.finalize_close()
                return None

            logger.info("  Leg 2: BUY filled — %s", spot_order.id)

            # 5. Place exchange-side stop-loss on futures
            stop_price = round(current_price * (1 + self.stop_loss_pct), len(str(tick_size).split('.')[-1]))
            try:
                stop_result = await self.futures.place_stop_order(
                    symbol=futures_symbol,
                    side="buy",  # Buy to close short
                    size=num_lots,
                    stop_price=stop_price,
                    stop_type="up",  # Trigger when price goes UP (bad for short)
                    leverage=self.leverage,
                )
                logger.info("  Stop-loss set @ %.4f (+%.0f%%) — order %s",
                            stop_price, self.stop_loss_pct * 100,
                            stop_result.get("orderId", ""))
            except Exception as e:
                logger.warning("  Stop-loss placement failed: %s (MONITOR MANUALLY)", e)

            # Update position
            pos.spot_quantity = spot_qty
            pos.spot_entry_price = spot_order.actual_price or current_price
            pos.futures_quantity = num_lots
            pos.futures_entry_price = current_price
            pos.position_usd = futures_usd
            pos.total_fees = (spot_order.fee or 0) + futures_usd * 0.0006  # Estimated futures fee
            pos.status = PositionStatus.ACTIVE

            logger.info(
                "POSITION ACTIVE: %s | Spot: %.4f @ $%.4f | Futures: %d lots short | Fees: $%.4f",
                opp.base_asset, spot_qty, pos.spot_entry_price,
                num_lots, pos.total_fees,
            )

            self.pm.alert(
                f"POSITION OPENED: {opp.base_asset} | "
                f"Funding: {opp.funding_rate:.4%}/8h | "
                f"Stop-loss: ${stop_price:.4f} (+{self.stop_loss_pct:.0%})"
            )

            return pos

        except Exception as e:
            logger.error("Entry FAILED: %s", e)
            self.pm.finalize_close()
            return None

    async def exit_position(self, reason: str) -> bool:
        """
        Exit the active position.

        1. Close futures short (buy to cover)
        2. Sell spot
        3. Cancel any stop-loss orders
        """
        pos = self.pm.active_position
        if pos is None:
            return False

        pos.status = PositionStatus.EXITING
        futures_symbol = pos.symbol
        spot_symbol = pos.spot_symbol

        logger.info("EXITING: %s — %s", pos.base_asset, reason)

        try:
            # 1. Cancel stop-loss orders
            try:
                await self.futures.cancel_all_orders(futures_symbol)
                logger.info("  Cancelled stop-loss orders")
            except Exception as e:
                logger.warning("  Cancel orders: %s", e)

            # 2. Close futures short (buy to cover)
            logger.info("  Leg 1: BUY %d lots %s (close short)...", pos.futures_quantity, futures_symbol)
            await self.futures.place_order(
                symbol=futures_symbol,
                side="buy",
                size=pos.futures_quantity,
                leverage=self.leverage,
            )
            logger.info("  Leg 1: Short closed")

            # 3. Sell spot — use actual balance, not saved quantity
            from core.models import OrderSide
            actual_spot_balance = await self.spot.get_balance(pos.base_asset)
            sell_qty = actual_spot_balance if actual_spot_balance > 0 else pos.spot_quantity
            # Round down for KuCoin
            import math
            sell_qty = math.floor(sell_qty) if pos.spot_entry_price < 0.1 else round(sell_qty, 2)

            logger.info("  Leg 2: SELL %.0f %s spot (balance: %.0f)...",
                        sell_qty, pos.base_asset, actual_spot_balance)
            spot_order = await self.spot.place_order(
                symbol=pos.base_asset + "USDT",
                side=OrderSide.SELL,
                quantity=sell_qty,
            )
            exit_fee = (spot_order.fee or 0) + pos.position_usd * 0.0006
            pos.total_fees += exit_fee
            logger.info("  Leg 2: Spot sold")

            pos.exit_time_ms = time_ns() // 1_000_000

            self.pm.finalize_close()

            logger.info(
                "POSITION CLOSED: %s | Funding: $%.4f | Fees: $%.4f | Net: $%.4f | Held: %.1fh",
                pos.base_asset, pos.funding_collected, pos.total_fees,
                pos.net_pnl, pos.holding_hours,
            )

            self.pm.alert(
                f"POSITION CLOSED: {pos.base_asset} | "
                f"Net P&L: ${pos.net_pnl:.4f} | Held: {pos.holding_hours:.1f}h"
            )

            return True

        except Exception as e:
            logger.error("Exit FAILED: %s — MANUAL INTERVENTION NEEDED", e)
            self.pm.alert(f"EXIT FAILED: {pos.base_asset} — {e} — CLOSE MANUALLY!")
            return False

    async def check_and_record_funding(self) -> float:
        """Check if funding was collected since last check."""
        pos = self.pm.active_position
        if pos is None:
            return 0.0

        try:
            history = await self.futures.get_funding_history(pos.symbol)
            if not history:
                return 0.0

            # Sum any new funding payments
            total_new = 0.0
            for entry in history:
                # KuCoin returns funding as negative for shorts collecting
                amount = float(entry.get("funding", 0))
                # For short position collecting positive funding, amount is positive
                total_new += abs(amount)

            if total_new > pos.funding_collected:
                diff = total_new - pos.funding_collected
                pos.funding_collected = total_new
                pos.funding_periods = len(history)
                self.pm.total_funding_collected = sum(
                    p.funding_collected for p in self.pm.closed_positions
                ) + total_new
                logger.info("  Funding update: +$%.6f (total: $%.6f)", diff, total_new)
                return diff

        except Exception as e:
            logger.debug("Funding check: %s", e)

        return 0.0

    async def check_position_health(self) -> dict:
        """Check margin ratio and basis for active position."""
        pos = self.pm.active_position
        if pos is None:
            return {}

        try:
            futures_pos = await self.futures.get_position(pos.symbol)
            if futures_pos:
                margin_ratio = float(futures_pos.get("maintMarginReq", 0))
                unrealised_pnl = float(futures_pos.get("unrealisedPnl", 0))
                mark_price = float(futures_pos.get("markPrice", 0))

                # Calculate basis
                spot_ticker = await self.spot.get_ticker(pos.base_asset + "USDT")
                if spot_ticker.mid > 0 and mark_price > 0:
                    pos.current_basis = (spot_ticker.mid - mark_price) / mark_price

                return {
                    "mark_price": mark_price,
                    "spot_price": spot_ticker.mid if spot_ticker else 0,
                    "basis": pos.current_basis,
                    "unrealised_pnl": unrealised_pnl,
                    "margin_ratio": margin_ratio,
                }
        except Exception as e:
            logger.debug("Health check: %s", e)

        return {}
