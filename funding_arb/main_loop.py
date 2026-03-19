"""Main loop — state machine for funding rate arbitrage."""

import asyncio
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from funding_arb.executor import FundingExecutor
from funding_arb.kucoin_futures import KuCoinFutures
from funding_arb.models import FundingDirection, PositionStatus
from funding_arb.position_manager import FundingPositionManager
from funding_arb.scanner import FundingScanner
from funding_arb.state import (
    append_ledger, clear_state, load_state, load_watchlist,
    save_state, save_watchlist, watchlist_age_hours,
)
from funding_arb.timing import (
    funding_info, in_entry_window, just_passed_funding,
    minutes_until_next_funding,
)

logger = logging.getLogger(__name__)

# --- Config ---
ENTRY_WINDOW_MINUTES = 480  # Always in window — enter immediately when opportunity found
SCAN_INTERVAL_MIN = 15
MONITOR_INTERVAL_SEC = 300  # 5 minutes
FUNDING_CONFIRM_DELAY_SEC = 300  # Check 5 min after timestamp
APPROVAL_TIMEOUT_SEC = 300  # 5 min for human
MIN_FUNDING_RATE = 0.001  # 0.10%
MIN_FUNDING_RATE_AUTO = 0.002  # 0.20% for auto-enter
MAX_FUNDING_RATE = 0.03  # 3% anomaly filter
EXIT_FUNDING_RATE = 0.0012  # 0.12% — exit if rate drops below (panel: stop dead money)
STAY_THRESHOLD = 0.0018  # 0.18% — only stay for 3rd payment if rate still above this
MAX_HOLD_HOURS = 32  # Hard cap
BASIS_STOP_LOSS = 0.015  # 1.5%
EXIT_GRACE_MINUTES = 10
AUTO_ENTER = False
WATCHLIST_SIZE = 50
LEVERAGE = 2
STOP_LOSS_PCT = 0.15


def alert(message: str) -> None:
    """Terminal + sound alert."""
    print("\n" + "=" * 60)
    print(f"  🔔  {message}")
    print("=" * 60 + "\n")
    try:
        if sys.platform == "darwin":
            subprocess.Popen(
                ["afplay", "/System/Library/Sounds/Glass.aiff"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            print("\a", end="", flush=True)
    except Exception:
        print("\a", end="", flush=True)


async def get_approval(opportunity, timeout: int = APPROVAL_TIMEOUT_SEC) -> bool:
    """Ask human to approve entry. Returns True if approved."""
    fee_cost = 0.0024
    be = fee_cost / abs(opportunity.funding_rate)
    predicted = opportunity.predicted_rate

    print(f"\n{'='*60}")
    print(f"  OPPORTUNITY: {opportunity.symbol}")
    print(f"  Current rate:   {opportunity.funding_rate:.4%}/8h ({opportunity.daily_rate:.4%}/day)")
    print(f"  Predicted next: {predicted:.4%}/8h {'OK' if predicted >= 0 else 'WARNING: FLIPPING'}")
    print(f"  APY:            {opportunity.annualized * 100:.0f}%")
    print(f"  Strategy:       Long {opportunity.base_asset} spot + Short {opportunity.symbol} perp")
    print(f"  Break-even:     {be:.1f} periods ({be * 8:.0f}h)")
    print(f"  Next funding:   {minutes_until_next_funding():.0f} min")
    print(f"{'='*60}")
    print(f"  Enter? [y/n] (timeout {timeout}s): ", end="", flush=True)

    loop = asyncio.get_event_loop()
    try:
        response = await asyncio.wait_for(
            loop.run_in_executor(None, input),
            timeout=timeout,
        )
        return response.strip().lower() in ("y", "yes")
    except asyncio.TimeoutError:
        print("\n  (timeout — skipping)")
        return False


async def reconcile_with_exchange(futures: KuCoinFutures, spot) -> dict | None:
    """Check exchange for orphaned positions on startup."""
    state = load_state()

    # Check actual exchange positions
    try:
        positions = await futures.get_all_positions()
        open_positions = [p for p in (positions or []) if abs(float(p.get("currentQty", 0))) > 0]
    except Exception as e:
        logger.warning("Failed to check futures positions: %s", e)
        open_positions = []

    try:
        spot_balances = await spot.get_all_balances()
        non_usdt = {k: v for k, v in spot_balances.items() if k != "USDT" and v > 0.01}
    except Exception as e:
        logger.warning("Failed to check spot balances: %s", e)
        non_usdt = {}

    if open_positions:
        pos = open_positions[0]
        symbol = pos.get("symbol", "?")
        qty = float(pos.get("currentQty", 0))
        logger.warning("Found open futures position: %s qty=%s", symbol, qty)

        if state and state.get("symbol") == symbol:
            logger.info("State file matches — resuming monitoring")
            return state
        else:
            alert(f"ORPHANED FUTURES POSITION: {symbol} qty={qty} — close manually!")
            return {"orphaned": True, "symbol": symbol}

    if state and state.get("state") == "MONITORING":
        logger.warning("State file says MONITORING but no futures position found")
        if non_usdt:
            alert(f"State says in position but no futures found. Spot balances: {non_usdt}")
            return {"orphaned": True}
        else:
            logger.info("No orphaned position — clearing stale state")
            clear_state()

    return None


async def run_main_loop():
    """Main funding rate arb loop."""
    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s │ %(levelname)-7s │ %(name)-22s │ %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("data/funding_arb.log", mode="a"),
        ],
    )

    logger.info("=== Funding Rate Arbitrage Bot ===")
    logger.info("Capital: $30 | Leverage: %dx | Mode: %s",
                LEVERAGE, "AUTO" if AUTO_ENTER else "HUMAN-APPROVE")

    # Init exchange clients
    kc_key = os.getenv("KUCOIN_API_KEY", "")
    kc_secret = os.getenv("KUCOIN_API_SECRET", "")
    kc_pass = os.getenv("KUCOIN_PASSPHRASE", "")

    if not kc_key:
        logger.error("KUCOIN_API_KEY not set in .env")
        return

    from exchange.kucoin_rest import KuCoinExchange
    spot = KuCoinExchange(kc_key, kc_secret, kc_pass)
    futures = KuCoinFutures(kc_key, kc_secret, kc_pass)
    await spot.get_all_pairs()  # Cache pairs for order sizing

    scanner = FundingScanner(min_funding_rate=MIN_FUNDING_RATE * 0.5)
    pm = FundingPositionManager(
        kucoin_exchange=spot,
        total_capital=30.0,
        min_funding_rate=MIN_FUNDING_RATE,
        exit_funding_rate=EXIT_FUNDING_RATE,
        max_holding_days=MAX_HOLD_HOURS / 24,
        basis_stop_loss=BASIS_STOP_LOSS,
    )
    executor = FundingExecutor(
        spot=spot, futures=futures, position_manager=pm,
        leverage=LEVERAGE, stop_loss_pct=STOP_LOSS_PCT,
    )

    # Startup reconciliation
    logger.info("Checking for existing positions...")
    existing = await reconcile_with_exchange(futures, spot)
    if existing and existing.get("orphaned"):
        logger.critical("Orphaned position — resolve manually before running bot")
        await spot.close()
        await futures.close()
        return

    current_state = "MONITORING" if existing else "IDLE"
    if existing and not existing.get("orphaned"):
        # Rebuild position manager state from saved state file
        from funding_arb.models import FundingDirection, FundingPosition, PositionStatus
        restored = FundingPosition(
            symbol=existing.get("symbol", ""),
            base_asset=existing.get("base_asset", ""),
            spot_symbol=existing.get("base_asset", "") + "-USDT",
            direction=FundingDirection.LONGS_PAY,
            spot_quantity=existing.get("spot_quantity", 0),
            spot_entry_price=existing.get("spot_entry_price", 0),
            futures_quantity=existing.get("futures_quantity", 0),
            futures_entry_price=existing.get("futures_entry_price", 0),
            position_usd=existing.get("position_usd", 0),
            entry_funding_rate=existing.get("entry_funding_rate", 0),
            funding_collected=existing.get("funding_collected", 0),
            funding_periods=existing.get("funding_periods", 0),
            total_fees=existing.get("total_fees", 0),
            status=PositionStatus.ACTIVE,
        )
        # Parse entry time
        entry_time_str = existing.get("entry_time", "")
        if entry_time_str:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(entry_time_str)
                restored.entry_time_ms = int(dt.timestamp() * 1000)
            except Exception:
                pass

        pm.active_position = restored
        pm.total_entries = 1
        logger.info(
            "Resumed: %s | Funding: $%.4f (%d periods) | Fees: $%.4f | Held: %.1fh",
            restored.symbol, restored.funding_collected, restored.funding_periods,
            restored.total_fees, restored.holding_hours,
        )

    # Check balances
    try:
        fut_bal = await futures.get_account_balance()
        spot_bal = await spot.get_all_balances()
        logger.info("Futures USDT: $%.2f | Spot USDT: $%.2f",
                     float(fut_bal.get("availableBalance", 0)),
                     spot_bal.get("USDT", 0))
    except Exception as e:
        logger.warning("Balance check: %s", e)

    logger.info("State: %s | %s", current_state, funding_info())

    # --- Main Loop ---
    pending_opp = None
    exit_reason = ""

    try:
        while True:
            fi = funding_info()

            if current_state == "IDLE":
                if in_entry_window(ENTRY_WINDOW_MINUTES):
                    current_state = "SCANNING"
                    logger.info("Entering scan window — %s min to funding", fi["minutes_until"])
                else:
                    sleep_min = min(fi["minutes_until"] - ENTRY_WINDOW_MINUTES, 30)
                    sleep_min = max(sleep_min, 1)
                    logger.info(
                        "IDLE | Next funding: %s (%.0f min) | Sleeping %.0f min",
                        fi["next_funding"], fi["minutes_until"], sleep_min,
                    )
                    await asyncio.sleep(sleep_min * 60)

            elif current_state == "SCANNING":
                # Refresh watchlist if stale (>24h)
                if watchlist_age_hours() > 24:
                    logger.info("Full scan (watchlist stale)...")
                    all_opps = await scanner.scan()
                    watchlist = [o.symbol for o in all_opps[:WATCHLIST_SIZE]]
                    save_watchlist(watchlist)
                    logger.info("Watchlist updated: %d symbols", len(watchlist))
                else:
                    logger.info("Watchlist scan (%d symbols)...", len(load_watchlist()))
                    all_opps = await scanner.scan()

                # Filter tradeable (LONGS_PAY + above threshold + predicted rate still positive)
                tradeable = [
                    o for o in all_opps
                    if o.is_longs_pay
                    and o.abs_rate >= MIN_FUNDING_RATE
                    and o.abs_rate <= MAX_FUNDING_RATE
                    and o.predicted_rate >= 0  # Don't enter if next period flips negative
                ]

                if tradeable:
                    logger.info("Found %d tradeable opportunities:", len(tradeable))
                    for o in tradeable[:5]:
                        logger.info(
                            "  %s: %.4f%%/8h (%s)",
                            o.symbol, o.funding_rate * 100, o.base_asset,
                        )
                    pending_opp = tradeable[0]
                    current_state = "AWAITING_APPROVAL"
                else:
                    logger.info("No tradeable opportunities above %.2f%%", MIN_FUNDING_RATE * 100)
                    if minutes_until_next_funding() > 0:
                        logger.info("Re-scanning in %d min...", SCAN_INTERVAL_MIN)
                        await asyncio.sleep(SCAN_INTERVAL_MIN * 60)
                    else:
                        current_state = "IDLE"

            elif current_state == "AWAITING_APPROVAL":
                if AUTO_ENTER and pending_opp.abs_rate >= MIN_FUNDING_RATE_AUTO:
                    logger.info("AUTO-ENTER: %s at %.4f%%", pending_opp.symbol, pending_opp.funding_rate * 100)
                    approved = True
                else:
                    alert(f"Opportunity: {pending_opp.symbol} at {pending_opp.funding_rate:.4%}/8h")
                    approved = await get_approval(pending_opp)

                if approved:
                    current_state = "ENTERING"
                else:
                    logger.info("Skipped %s", pending_opp.symbol)
                    current_state = "SCANNING"

            elif current_state == "ENTERING":
                position = await executor.enter_position(pending_opp)
                if position and position.status == PositionStatus.ACTIVE:
                    save_state({
                        "state": "MONITORING",
                        "symbol": position.symbol,
                        "base_asset": position.base_asset,
                        "spot_quantity": position.spot_quantity,
                        "futures_quantity": position.futures_quantity,
                        "spot_entry_price": position.spot_entry_price,
                        "futures_entry_price": position.futures_entry_price,
                        "entry_funding_rate": position.entry_funding_rate,
                        "position_usd": position.position_usd,
                        "funding_collected": 0,
                        "funding_periods": 0,
                        "total_fees": position.total_fees,
                        "entry_time": datetime.now(timezone.utc).isoformat(),
                    })
                    current_state = "MONITORING"
                    logger.info("Entry complete — monitoring position")
                else:
                    logger.warning("Entry failed — returning to IDLE")
                    current_state = "IDLE"

            elif current_state == "MONITORING":
                # Check funding collection after timestamp
                if just_passed_funding(within_minutes=6):
                    logger.info("Funding timestamp just passed — checking collection...")
                    await asyncio.sleep(FUNDING_CONFIRM_DELAY_SEC)
                    collected = await executor.check_and_record_funding()
                    if collected > 0:
                        logger.info("Funding collected: $%.6f", collected)
                    # Update state file
                    if pm.active_position:
                        p = pm.active_position
                        save_state({
                            "state": "MONITORING",
                            "symbol": p.symbol,
                            "base_asset": p.base_asset,
                            "spot_quantity": p.spot_quantity,
                            "futures_quantity": p.futures_quantity,
                            "spot_entry_price": p.spot_entry_price,
                            "futures_entry_price": p.futures_entry_price,
                            "entry_funding_rate": p.entry_funding_rate,
                            "position_usd": p.position_usd,
                            "funding_collected": p.funding_collected,
                            "funding_periods": p.funding_periods,
                            "total_fees": p.total_fees,
                            "entry_time": datetime.fromtimestamp(
                                p.entry_time_ms / 1000, tz=timezone.utc
                            ).isoformat() if p.entry_time_ms else "",
                        })

                # Health check
                health = await executor.check_position_health()

                # Get current funding rate
                try:
                    rate_data = await futures.get_funding_rate(pm.active_position.symbol)
                    current_rate = float(rate_data.get("value", 0))
                except Exception:
                    current_rate = 0

                should_exit, reason = pm.should_exit(current_rate)

                # Dynamic 2-payment strategy (panel consensus)
                if pm.active_position:
                    periods = pm.active_position.funding_periods

                    # After 2 payments: exit UNLESS rate still strong
                    if not should_exit and periods >= 2 and current_rate < STAY_THRESHOLD:
                        should_exit = True
                        reason = f"2 payments collected, rate {current_rate:.4%} < stay threshold {STAY_THRESHOLD:.4%}"
                        logger.info("Dynamic exit: %s", reason)

                    # After 3 payments: always exit (diminishing returns)
                    if not should_exit and periods >= 3:
                        should_exit = True
                        reason = f"3 payments collected — diminishing returns"
                        logger.info("Dynamic exit: %s", reason)

                # Grace period near funding
                if should_exit and "normalized" in reason.lower():
                    if minutes_until_next_funding() <= EXIT_GRACE_MINUTES:
                        logger.info("Exit deferred — %d min to funding, collecting one more",
                                    int(minutes_until_next_funding()))
                        should_exit = False

                if should_exit:
                    exit_reason = reason
                    current_state = "EXITING"
                    logger.info("Exit triggered: %s", reason)
                else:
                    pos = pm.active_position
                    if pos:
                        logger.info(
                            "MONITORING %s | Rate: %.4f%% | Basis: %.4f%% | "
                            "Funding: $%.4f (%d periods) | Fees: $%.4f | "
                            "Next funding: %.0f min | Held: %.1fh",
                            pos.symbol, current_rate * 100,
                            (pos.current_basis or 0) * 100,
                            pos.funding_collected, pos.funding_periods,
                            pos.total_fees,
                            minutes_until_next_funding(),
                            pos.holding_hours,
                        )
                    await asyncio.sleep(MONITOR_INTERVAL_SEC)

            elif current_state == "EXITING":
                success = await executor.exit_position(exit_reason)
                if success:
                    # Write ledger
                    pos = pm.closed_positions[-1] if pm.closed_positions else None
                    if pos:
                        append_ledger({
                            "symbol": pos.symbol,
                            "base_asset": pos.base_asset,
                            "entry_rate": pos.entry_funding_rate,
                            "held_hours": round(pos.holding_hours, 1),
                            "funding_collected": round(pos.funding_collected, 6),
                            "total_fees": round(pos.total_fees, 6),
                            "net_pnl": round(pos.net_pnl, 6),
                            "funding_periods": pos.funding_periods,
                            "exit_reason": exit_reason,
                        })
                    clear_state()
                    current_state = "IDLE"
                else:
                    alert("EXIT FAILED — MANUAL INTERVENTION REQUIRED")
                    logger.critical("Exit failed — halting bot")
                    break

    except KeyboardInterrupt:
        logger.info("Shutting down...")
        if current_state == "MONITORING" and pm.active_position:
            alert(f"WARNING: Position still open on {pm.active_position.symbol}! "
                  f"Run again to manage it, or close manually.")
    except Exception as e:
        logger.critical("Unhandled error in %s: %s", current_state, e, exc_info=True)
        if current_state == "MONITORING":
            alert(f"ERROR while in position: {e}")
    finally:
        await spot.close()
        await futures.close()

        # Print summary
        stats = pm.stats()
        ledger = funding_arb_summary()
        print(f"\n{'='*60}")
        print(f"  FUNDING ARB SESSION SUMMARY")
        print(f"{'='*60}")
        print(f"  Entries: {stats['total_entries']}")
        print(f"  Exits:   {stats['total_exits']}")
        print(f"  Funding: ${stats['total_funding']:.6f}")
        print(f"  Fees:    ${stats['total_fees']:.6f}")
        print(f"  Net P&L: ${stats['net_pnl']:.6f}")
        if pm.active_position:
            print(f"\n  ⚠ POSITION STILL OPEN: {pm.active_position.symbol}")
        print(f"{'='*60}\n")


def funding_arb_summary() -> dict:
    """Read ledger and compute summary."""
    from funding_arb.state import read_ledger
    entries = read_ledger()
    if not entries:
        return {"trades": 0, "net_pnl": 0}
    return {
        "trades": len(entries),
        "net_pnl": sum(e.get("net_pnl", 0) for e in entries),
        "total_funding": sum(e.get("funding_collected", 0) for e in entries),
        "total_fees": sum(e.get("total_fees", 0) for e in entries),
    }


def main():
    """CLI entry point."""
    asyncio.run(run_main_loop())


if __name__ == "__main__":
    main()
