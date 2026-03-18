"""Crypto Triangular Arbitrage — Entry Point & Trading Loop."""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env before anything else
load_dotenv()


def setup_logging(level: str = "INFO", dashboard: bool = False) -> None:
    if dashboard:
        # When dashboard is active, suppress log output (dashboard shows everything)
        logging.basicConfig(
            level=logging.WARNING,
            format="%(asctime)s │ %(levelname)-7s │ %(name)-20s │ %(message)s",
            datefmt="%H:%M:%S",
        )
    else:
        logging.basicConfig(
            level=getattr(logging, level.upper(), logging.INFO),
            format="%(asctime)s │ %(levelname)-7s │ %(name)-20s │ %(message)s",
            datefmt="%H:%M:%S",
        )
    # Quiet noisy libraries
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("ccxt").setLevel(logging.WARNING)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Crypto Triangular Arbitrage System for Binance"
    )
    parser.add_argument(
        "--mode",
        choices=["simulation", "live"],
        default="simulation",
        help="Trading mode (default: simulation)",
    )
    parser.add_argument(
        "--cross-exchange",
        action="store_true",
        help="Enable cross-exchange arbitrage simulation",
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Enable real-time CLI dashboard",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan for opportunities without executing trades",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=0,
        help="Run for N seconds then stop (0 = run forever)",
    )
    return parser.parse_args()


async def run_simulation(args):
    """Main simulation loop — real prices, virtual trades."""
    from config.settings import Config
    from core.calculator import ProfitCalculator
    from core.scanner import TriangleScanner
    from core.triangle import TriangleGraph
    from dashboard.cli_monitor import Dashboard
    from data.db import Database
    from data.price_cache import PriceCache
    from exchange.binance_rest import BinanceREST
    from exchange.binance_ws import BinanceWebSocket
    from exchange.simulator import SimulatedExchange
    from execution.executor import Executor
    from execution.order_manager import OrderManager
    from execution.risk_manager import RiskManager

    logger = logging.getLogger("main")
    config = Config()

    # --- Initialize components ---

    logger.info("=== Crypto Triangular Arbitrage ===")
    logger.info("Mode: %s | Dry-run: %s | Dashboard: %s", args.mode, args.dry_run, args.dashboard)

    # 1. Fetch trading pairs from Binance
    rest = BinanceREST()
    logger.info("Fetching trading pairs from Binance...")
    pairs = await rest.get_all_pairs(
        quote_assets=config.scanner.quote_assets,
    )
    await rest.close()
    logger.info("Loaded %d pairs", len(pairs))

    # 2. Build triangle graph
    graph = TriangleGraph()
    graph.load_pairs(pairs)
    triangles = graph.discover_triangles(max_triangles=config.scanner.max_triangles)
    logger.info(
        "Discovered %d triangles across %d assets",
        len(triangles), graph.stats()["total_assets"],
    )

    if not triangles:
        logger.error("No triangles found — check pair filters")
        return

    # 3. Setup components
    symbols = graph.get_subscribed_symbols()
    logger.info("Subscribing to %d symbols", len(symbols))

    calculator = ProfitCalculator(fee_rate=config.fees.effective_fee)
    scanner = TriangleScanner(graph, calculator, min_profit=config.trading.min_profit_threshold)
    price_cache = PriceCache()
    risk_manager = RiskManager(config.trading)
    order_manager = OrderManager()

    # Simulated exchange
    sim_exchange = SimulatedExchange(config.fees, config.simulation)
    sim_exchange.load_pairs(pairs)

    executor = Executor(sim_exchange, risk_manager, config.trading, config.fees)

    # Database
    db = Database(config.database)
    await db.connect()
    session_id = await db.start_session(args.mode)

    # 4. WebSocket callbacks — event-driven scanning
    ticker_queue: asyncio.Queue = asyncio.Queue()

    def on_ticker(ticker):
        price_cache.update_ticker(ticker)
        scanner.tickers[ticker.symbol] = ticker
        sim_exchange.inject_ticker(ticker)
        try:
            ticker_queue.put_nowait(ticker)
        except asyncio.QueueFull:
            pass  # Drop if backlogged

    def on_order_book(book):
        price_cache.update_order_book(book)
        sim_exchange.inject_order_book(book)

    ws = BinanceWebSocket(
        config=config.websocket,
        on_ticker=on_ticker,
        on_order_book=on_order_book,
    )

    # 5. Dashboard (optional)
    dashboard = None
    if args.dashboard:
        dashboard = Dashboard(
            scanner=scanner,
            price_cache=price_cache,
            ws=ws,
            exchange=sim_exchange,
            executor=executor,
            risk_manager=risk_manager,
            order_manager=order_manager,
            mode=args.mode,
        )

    # 6. Opportunity processing task
    opportunity_queue: asyncio.Queue = asyncio.Queue()

    async def process_opportunities():
        """Consumer: execute profitable opportunities."""
        while True:
            opp = await opportunity_queue.get()
            if opp is None:
                break

            path = " → ".join(opp.triangle.assets)

            # Risk check
            approved, reason = risk_manager.check(opp, ws_healthy=ws.is_healthy)

            if not approved:
                opp.skip_reason = reason
                await db.log_opportunity(opp)
                if dashboard:
                    dashboard.record_opportunity(path, opp.theoretical_profit, False, reason)
                continue

            if args.dry_run:
                logger.info(
                    "DRY-RUN: Would execute %s (%.4f%%)",
                    path, opp.theoretical_profit * 100,
                )
                opp.skip_reason = "dry-run"
                await db.log_opportunity(opp)
                if dashboard:
                    dashboard.record_opportunity(path, opp.theoretical_profit, False, "dry-run")
                continue

            # Execute!
            opp.executed = True
            opp_id = await db.log_opportunity(opp)

            result = await executor.execute(opp)
            order_manager.record_result(result)

            # Log each trade leg
            for i, order in enumerate(result.orders):
                await db.log_trade(opp_id, i + 1, order)

            if dashboard:
                pnl = result.net_pnl if not result.aborted else 0.0
                dashboard.record_opportunity(path, pnl, not result.aborted,
                                             result.abort_reason if result.aborted else "")

    # 7. Scanning task — event-driven from ticker queue
    scan_count = 0

    async def scan_loop():
        nonlocal scan_count
        while True:
            ticker = await ticker_queue.get()
            if ticker is None:
                break

            opportunities = scanner.update_ticker(ticker)
            for opp in opportunities:
                await opportunity_queue.put(opp)

            scan_count += 1

            # Periodic stats (only when dashboard is off)
            if not args.dashboard and scan_count % 5000 == 0:
                s = scanner.stats()
                r = risk_manager.stats()
                e = executor.stats()
                logger.info(
                    "Stats | Ticks: %d | Scans: %d | Opps: %d | "
                    "Trades: %d | P&L: $%.4f | Killed: %s",
                    s["total_ticks"], s["total_triangle_scans"],
                    s["total_opportunities"], e["total_executions"],
                    e["net_pnl"], r["killed"],
                )

    # 8. Start everything
    logger.info("Starting WebSocket connection...")

    # Create tasks
    processor_task = asyncio.create_task(process_opportunities())
    scanner_task = asyncio.create_task(scan_loop())
    dashboard_task = None
    if dashboard:
        dashboard_task = asyncio.create_task(dashboard.run())

    # Duration limit
    async def duration_watchdog():
        if args.duration > 0:
            await asyncio.sleep(args.duration)
            logger.info("Duration limit reached (%ds)", args.duration)
            await ws.stop()

    watchdog_task = asyncio.create_task(duration_watchdog()) if args.duration > 0 else None

    try:
        await ws.listen_with_reconnect(symbols)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        logger.info("Shutting down...")

        # Stop tasks
        await ticker_queue.put(None)  # Signal scanner to stop
        await scanner_task
        if dashboard_task:
            dashboard_task.cancel()
        await opportunity_queue.put(None)  # Signal processor to stop
        await processor_task
        if watchdog_task:
            watchdog_task.cancel()

        # Final stats
        await ws.stop()
        await db.end_session(
            gross_pnl=executor.total_profit - executor.total_loss,
            net_pnl=executor.total_profit - executor.total_loss,
            fees_paid=order_manager.total_fees,
        )
        await db.close()
        await sim_exchange.close()

        # Print formatted summary
        s = scanner.stats()
        e = executor.stats()
        r = risk_manager.stats()
        o = order_manager.stats()
        x = sim_exchange.stats()
        w = ws.stats()
        bal = x.get("balances", {})

        print("\n" + "=" * 60)
        print("  SESSION SUMMARY")
        print("=" * 60)

        print("\n  SCANNER")
        print(f"    Ticks processed:    {s['total_ticks']:>12,}")
        print(f"    Triangle scans:     {s['total_triangle_scans']:>12,}")
        print(f"    Opportunities:      {s['total_opportunities']:>12}")
        print(f"    Hit rate:           {s['hit_rate']:>12}")
        print(f"    Tracked symbols:    {s['tracked_symbols']:>12}")

        print("\n  EXECUTION")
        print(f"    Trades executed:    {e['total_executions']:>12}")
        print(f"    Aborted:            {e['total_aborts']:>12}")
        print(f"    Win rate:           {o['win_rate']:>12}")

        print("\n  P&L (USD)")
        pnl = e['net_pnl']
        pnl_sign = "+" if pnl >= 0 else ""
        print(f"    Net P&L:          {pnl_sign}${pnl:>11.4f}")
        print(f"    Gross profit:      ${e['total_profit']:>11.4f}")
        print(f"    Gross loss:       -${e['total_loss']:>11.4f}")
        print(f"    Total fees:        ${o['total_fees']:>11.4f}")

        print("\n  RISK")
        print(f"    Daily P&L:        {'+' if r['daily_pnl'] >= 0 else ''}${r['daily_pnl']:>11.4f}")
        print(f"    Consec. losses:     {r['consecutive_losses']:>12}")
        print(f"    Kill switch:        {'ACTIVE — ' + r['kill_reason'] if r['killed'] else 'OFF':>12}")
        print(f"    Approved/Rejected:  {r['total_approved']}/{r['total_rejected']}")

        print("\n  BALANCES")
        for asset, amount in sorted(bal.items()):
            print(f"    {asset:<6}  {amount:>18.8f}")

        print("\n  WEBSOCKET")
        print(f"    Messages received:  {w['total_messages']:>12,}")
        print(f"    Reconnects:         {w['total_reconnects']:>12}")

        print("\n" + "=" * 60)


async def run_cross_exchange_simulation(args):
    """Cross-exchange simulation — real base prices, simulated divergence, live execution."""
    from config.settings import Config
    from cross_exchange.scanner import CrossExchangeScanner
    from cross_exchange.executor import CrossExchangeExecutor
    from cross_exchange.risk_manager import CrossExchangeRiskManager
    from cross_exchange.balance_tracker import BalanceTracker
    from cross_exchange.models import CrossExchangeOpportunity
    from data.db import Database
    from data.price_cache import PriceCache
    from exchange.binance_rest import BinanceREST
    from exchange.binance_ws import BinanceWebSocket
    from exchange.multi_sim import MultiExchangeSimulator

    logger = logging.getLogger("main.cross")
    config = Config()

    logger.info("=== Cross-Exchange Arbitrage Simulation ===")
    logger.info("Exchanges: %s", ", ".join(config.multi_sim.exchange_ids))
    logger.info("Symbols: %s", ", ".join(config.cross_exchange.symbols))

    # 1. Fetch pairs from Binance (used as base)
    rest = BinanceREST()
    pairs = await rest.get_all_pairs(quote_assets=["USDT"])
    await rest.close()
    logger.info("Loaded %d USDT pairs", len(pairs))

    # 2. Multi-exchange simulator
    multi_sim = MultiExchangeSimulator(config.multi_sim)
    multi_sim.load_pairs(pairs)

    # 3. Cross-exchange scanner
    fee_schedules = multi_sim.get_fee_schedules()
    cx_scanner = CrossExchangeScanner(
        symbols=config.cross_exchange.symbols,
        fee_schedules=fee_schedules,
        min_net_spread=config.cross_exchange.min_net_spread,
        staleness_ms=config.cross_exchange.staleness_threshold_ms,
        dedup_cooldown_ms=config.cross_exchange.dedup_cooldown_ms,
    )

    # 4. Executor + Risk Manager
    cx_risk = CrossExchangeRiskManager(config.trading, config.cross_exchange)
    cx_executor = CrossExchangeExecutor(
        exchanges=multi_sim.exchanges,
        trading_config=config.trading,
        cx_config=config.cross_exchange,
    )

    # 5. Balance tracker
    balance_tracker = BalanceTracker(multi_sim.exchanges)
    await balance_tracker.refresh_all()

    # 6. Database
    db = Database(config.database)
    await db.connect()
    session_id = await db.start_session("cross_exchange_sim")

    # 7. Opportunity queue for async execution
    opp_queue: asyncio.Queue = asyncio.Queue()
    price_cache = PriceCache()

    # WebSocket callback — inject base price, generate divergent, scan
    def on_ticker(ticker):
        price_cache.update_ticker(ticker)

        if ticker.symbol not in cx_scanner.books:
            return

        divergent = multi_sim.inject_base_ticker(ticker)

        for ex_id, ex_ticker in divergent.items():
            opp = cx_scanner.update(ex_id, ex_ticker)
            if opp is not None:
                try:
                    opp_queue.put_nowait(opp)
                except asyncio.QueueFull:
                    pass

    ws = BinanceWebSocket(config=config.websocket, on_ticker=on_ticker)

    # 8. Opportunity processor — executes trades
    trade_results = []

    async def process_opportunities():
        while True:
            opp = await opp_queue.get()
            if opp is None:
                break

            # Risk check
            approved, reason = cx_risk.check(opp)
            if not approved:
                opp.skip_reason = reason
                await db.log_cross_opportunity(opp)
                continue

            if args.dry_run:
                logger.info(
                    "DRY-RUN: %s BUY %s SELL %s (net: %.4f%%)",
                    opp.symbol, opp.buy_exchange, opp.sell_exchange,
                    opp.net_spread * 100,
                )
                opp.skip_reason = "dry-run"
                await db.log_cross_opportunity(opp)
                continue

            # Execute
            cx_risk.on_arb_start()
            opp.executed = True
            opp_id = await db.log_cross_opportunity(opp)

            result = await cx_executor.execute(opp)
            trade_results.append(result)

            # Log trades
            if result.buy_order:
                await db.log_cross_trade(opp_id, opp.buy_exchange, result.buy_order)
            if result.sell_order:
                await db.log_cross_trade(opp_id, opp.sell_exchange, result.sell_order)
            if result.hedge_order:
                await db.log_cross_trade(opp_id, "hedge", result.hedge_order)

            cx_risk.record_trade_result(
                result.net_pnl,
                had_emergency_hedge=result.hedge_order is not None,
            )
            cx_risk.on_arb_end()

            # Refresh balances after trade
            await balance_tracker.refresh_all()

    processor_task = asyncio.create_task(process_opportunities())

    # 9. Duration watchdog
    async def duration_watchdog():
        if args.duration > 0:
            await asyncio.sleep(args.duration)
            logger.info("Duration limit reached (%ds)", args.duration)
            await ws.stop()

    watchdog_task = asyncio.create_task(duration_watchdog()) if args.duration > 0 else None

    # 10. Periodic stats
    async def stats_loop():
        while True:
            await asyncio.sleep(10)
            s = cx_scanner.stats()
            e = cx_executor.stats()
            r = cx_risk.stats()
            logger.info(
                "Cross | Opps: %d | Exec: %d | P&L: $%.4f | "
                "Hedges: %d | Killed: %s",
                s["total_opportunities"], e["total_executions"],
                e["net_pnl"], e["emergency_hedges"], r["killed"],
            )

    stats_task = asyncio.create_task(stats_loop())

    symbols_to_subscribe = set(config.cross_exchange.symbols)
    logger.info("Subscribing to %d symbols...", len(symbols_to_subscribe))

    try:
        await ws.listen_with_reconnect(symbols_to_subscribe)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        logger.info("Shutting down...")
        stats_task.cancel()
        await opp_queue.put(None)
        await processor_task
        if watchdog_task:
            watchdog_task.cancel()
        await ws.stop()
        await db.end_session(
            gross_pnl=cx_executor.total_profit - cx_executor.total_loss,
            net_pnl=cx_executor.total_profit - cx_executor.total_loss,
            fees_paid=sum(r.total_fees for r in trade_results),
        )
        await db.close()

        # Summary
        s = cx_scanner.stats()
        e = cx_executor.stats()
        r = cx_risk.stats()
        b = balance_tracker.stats()

        print("\n" + "=" * 60)
        print("  CROSS-EXCHANGE SESSION SUMMARY")
        print("=" * 60)

        print(f"\n  SCANNER")
        print(f"    Symbols tracked:    {s['tracked_symbols']:>10}")
        print(f"    Total updates:      {s['total_updates']:>10,}")
        print(f"    Opportunities:      {s['total_opportunities']:>10}")
        print(f"    Deduped:            {s['total_deduped']:>10}")

        print(f"\n  EXECUTION")
        print(f"    Total executions:   {e['total_executions']:>10}")
        print(f"    Both filled:        {e['both_filled']:>10}")
        print(f"    Aborts:             {e['aborts']:>10}")
        print(f"    Emergency hedges:   {e['emergency_hedges']:>10}")
        print(f"    Win rate:           {e['win_rate']:>10}")

        print(f"\n  P&L (USD)")
        pnl = e['net_pnl']
        print(f"    Net P&L:          {'+'if pnl>=0 else ''}${pnl:>11.4f}")
        print(f"    Gross profit:      ${e['total_profit']:>11.4f}")
        print(f"    Gross loss:       -${e['total_loss']:>11.4f}")
        total_fees = sum(tr.total_fees for tr in trade_results)
        print(f"    Total fees:        ${total_fees:>11.4f}")

        print(f"\n  RISK")
        print(f"    Daily P&L:        {'+'if r['daily_pnl']>=0 else ''}${r['daily_pnl']:>11.4f}")
        print(f"    Consec. losses:     {r['consecutive_losses']:>10}")
        print(f"    Emergency hedges:   {r['emergency_hedges']:>10}")
        print(f"    Kill switch:        {'ACTIVE — '+r['kill_reason'] if r['killed'] else 'OFF':>10}")
        print(f"    Approved/Rejected:  {r['approved']}/{r['rejected']}")

        print(f"\n  BALANCES PER EXCHANGE")
        for ex_id, bals in b.get("per_exchange", {}).items():
            parts = [f"{k}: {v:.4f}" for k, v in sorted(bals.items()) if v > 0.0001]
            print(f"    {ex_id:<14}  {', '.join(parts)}")

        print(f"\n  WEBSOCKET")
        print(f"    Messages received:  {ws.stats()['total_messages']:>10,}")

        print("\n" + "=" * 60)


async def main():
    args = parse_args()
    setup_logging(args.log_level, dashboard=args.dashboard)

    if args.mode == "live":
        key = os.getenv("BINANCE_API_KEY", "")
        if not key:
            print("ERROR: BINANCE_API_KEY not set in .env")
            sys.exit(1)
        print("Live mode not yet implemented — use simulation")
        sys.exit(1)

    if args.cross_exchange:
        await run_cross_exchange_simulation(args)
    else:
        await run_simulation(args)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nGraceful shutdown complete.")
        sys.exit(0)
