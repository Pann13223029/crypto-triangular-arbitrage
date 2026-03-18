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

        # Print summary
        print("\n" + "=" * 60)
        print("SESSION SUMMARY")
        print("=" * 60)
        print(f"  Scanner:   {scanner.stats()}")
        print(f"  Executor:  {executor.stats()}")
        print(f"  Risk:      {risk_manager.stats()}")
        print(f"  Orders:    {order_manager.stats()}")
        print(f"  Exchange:  {sim_exchange.stats()}")
        print(f"  WebSocket: {ws.stats()}")
        print("=" * 60)


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

    await run_simulation(args)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nGraceful shutdown complete.")
        sys.exit(0)
