"""Crypto Triangular Arbitrage — Entry Point & Trading Loop."""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from time import time_ns

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
        "--live-scan",
        action="store_true",
        help="Live cross-exchange scan with real prices from 3 exchanges",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Enable LIVE order execution (requires API keys in .env)",
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
    from monitoring.metrics import PipelineMetrics, TradeMetric
    from rebalancing.manager import RebalanceManager
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

    # 3. Balance tracker (needed by scanner for pre-flight checks)
    balance_tracker = BalanceTracker(multi_sim.exchanges)
    await balance_tracker.refresh_all()

    # 4. Cross-exchange scanner (with pre-flight balance filter)
    fee_schedules = multi_sim.get_fee_schedules()
    cx_scanner = CrossExchangeScanner(
        symbols=config.cross_exchange.symbols,
        fee_schedules=fee_schedules,
        min_net_spread=config.cross_exchange.min_net_spread,
        staleness_ms=config.cross_exchange.staleness_threshold_ms,
        dedup_cooldown_ms=config.cross_exchange.dedup_cooldown_ms,
        balance_tracker=balance_tracker,
        min_trade_usd=10.0,
    )

    # 5. Executor + Risk Manager
    cx_risk = CrossExchangeRiskManager(config.trading, config.cross_exchange)
    cx_executor = CrossExchangeExecutor(
        exchanges=multi_sim.exchanges,
        trading_config=config.trading,
        cx_config=config.cross_exchange,
    )

    # 6. Rebalancer
    rebalancer = RebalanceManager(balance_tracker, config.rebalance)
    rebalancer.set_targets(config.multi_sim.exchange_ids)

    # 7. Pipeline metrics
    metrics = PipelineMetrics()

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

    ws = BinanceWebSocket(
        config=config.websocket,
        on_ticker=on_ticker,
        use_book_ticker=True,  # Faster: best bid/ask only, updates on every book change
    )

    # 8. Opportunity processor — executes trades
    trade_results = []

    async def process_opportunities():
        while True:
            opp = await opp_queue.get()
            if opp is None:
                break

            tm = TradeMetric(
                opportunity_detected_ms=opp.timestamp_ms,
                symbol=opp.symbol,
            )

            # Risk check
            approved, reason = cx_risk.check(opp)
            tm.risk_check_ms = time_ns() // 1_000_000

            if not approved:
                opp.skip_reason = reason
                await db.log_cross_opportunity(opp)
                tm.aborted = True
                metrics.record(tm)
                continue

            if args.dry_run:
                opp.skip_reason = "dry-run"
                await db.log_cross_opportunity(opp)
                tm.aborted = True
                metrics.record(tm)
                continue

            # Execute
            cx_risk.on_arb_start()
            opp.executed = True
            opp_id = await db.log_cross_opportunity(opp)

            tm.execution_start_ms = time_ns() // 1_000_000
            result = await cx_executor.execute(opp)
            tm.execution_end_ms = time_ns() // 1_000_000
            tm.net_pnl = result.net_pnl

            trade_results.append(result)
            metrics.record(tm)

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

    # 10. Periodic stats + rebalancing check
    async def stats_and_rebalance_loop():
        rebalance_counter = 0
        while True:
            await asyncio.sleep(10)
            rebalance_counter += 1

            s = cx_scanner.stats()
            e = cx_executor.stats()
            r = cx_risk.stats()
            rb = rebalancer.stats()
            logger.info(
                "Cross | Opps: %d | Exec: %d | P&L: $%.4f | "
                "Hedges: %d | Rebal: %d | Killed: %s",
                s["total_opportunities"], e["total_executions"],
                e["net_pnl"], e["emergency_hedges"],
                rb["total_transfers"], r["killed"],
            )

            # Check rebalancing every N intervals
            check_every = max(1, int(config.rebalance.check_interval_sec / 10))
            if config.rebalance.enabled and rebalance_counter % check_every == 0:
                await balance_tracker.refresh_all()

                # Feed deviation data to risk manager for imbalance filtering
                dev_report = rebalancer.get_deviation_report()
                dev_fracs = {}
                for ex_id, info in dev_report.items():
                    target = info["target_usdt"]
                    current = info["current_usdt"]
                    dev_fracs[ex_id] = (current - target) / target if target > 0 else 0
                cx_risk.update_deviations(dev_fracs)

                decision = rebalancer.check_rebalance_needed()
                if decision is not None:
                    completed = await rebalancer.execute_rebalance(decision)
                    for transfer in completed:
                        await db.log_transfer(transfer)

                    for ex_id, info in dev_report.items():
                        logger.info(
                            "  Balance: %s USDT $%.0f (target $%.0f, dev %s)",
                            ex_id, info["current_usdt"],
                            info["target_usdt"], info["deviation"],
                        )

    stats_task = asyncio.create_task(stats_and_rebalance_loop())

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
        rb = rebalancer.stats()
        dev = rebalancer.get_deviation_report()

        print("\n" + "=" * 60)
        print("  CROSS-EXCHANGE SESSION SUMMARY")
        print("=" * 60)

        print(f"\n  SCANNER")
        print(f"    Symbols tracked:    {s['tracked_symbols']:>10}")
        print(f"    Total updates:      {s['total_updates']:>10,}")
        print(f"    Opportunities:      {s['total_opportunities']:>10}")
        print(f"    Deduped:            {s['total_deduped']:>10}")
        print(f"    Preflight rejected: {s.get('preflight_rejected', 0):>10}")

        print(f"\n  EXECUTION")
        print(f"    Total executions:   {e['total_executions']:>10}")
        print(f"    Both filled:        {e['both_filled']:>10}")
        print(f"    Aborts:             {e['aborts']:>10}")
        print(f"    Emergency hedges:   {e['emergency_hedges']:>10}")
        print(f"    Maker sells:        {e.get('maker_sells', 0):>10}")
        print(f"    Maker timeouts:     {e.get('maker_timeouts', 0):>10}")
        print(f"    Win rate:           {e['win_rate']:>10}")

        print(f"\n  P&L (USD)")
        pnl = e['net_pnl']
        print(f"    Net P&L:          {'+'if pnl>=0 else ''}${pnl:>11.4f}")
        print(f"    Gross profit:      ${e['total_profit']:>11.4f}")
        print(f"    Gross loss:       -${e['total_loss']:>11.4f}")
        total_fees = sum(tr.total_fees for tr in trade_results)
        print(f"    Total fees:        ${total_fees:>11.4f}")

        print(f"\n  REBALANCING")
        print(f"    Transfers done:     {rb['total_transfers']:>10}")
        print(f"    Amount moved:      ${rb['total_transferred_usd']:>11.2f}")
        print(f"    Transfer fees:     ${rb['total_transfer_fees']:>11.2f}")

        print(f"\n  RISK")
        print(f"    Daily P&L:        {'+'if r['daily_pnl']>=0 else ''}${r['daily_pnl']:>11.4f}")
        print(f"    Consec. losses:     {r['consecutive_losses']:>10}")
        print(f"    Emergency hedges:   {r['emergency_hedges']:>10}")
        print(f"    Kill switch:        {'ACTIVE — '+r['kill_reason'] if r['killed'] else 'OFF':>10}")
        print(f"    Approved/Rejected:  {r['approved']}/{r['rejected']}")
        print(f"    Imbalance blocked:  {r.get('imbalance_rejected', 0):>10}")

        print(f"\n  BALANCES PER EXCHANGE")
        for ex_id in sorted(dev.keys()):
            info = dev[ex_id]
            bals = b.get("per_exchange", {}).get(ex_id, {})
            usdt = bals.get("USDT", 0)
            flag = " ⚠" if info["needs_rebalance"] else ""
            print(
                f"    {ex_id:<14}  USDT: ${usdt:>10.2f}  "
                f"(target: ${info['target_usdt']:>.0f}, dev: {info['deviation']}){flag}"
            )

        # Pipeline metrics
        m = metrics.stats()
        print(f"\n  PIPELINE TIMING")
        print(f"    Avg pipeline:       {m['avg_pipeline_ms']:>8}ms")
        print(f"    Avg execution:      {m['avg_execution_ms']:>8}ms")
        print(f"    Avg opp age:        {m['avg_opportunity_age_ms']:>8}ms")
        print(f"    Max opp age:        {m['max_opportunity_age_ms']:>8}ms")
        print(f"    Abort rate:         {m['abort_rate']:>10}")

        # Per-symbol P&L
        sym_report = metrics.symbol_report()
        if sym_report:
            print(f"\n  P&L BY SYMBOL")
            for sr in sym_report[:10]:
                pnl_sign = "+" if sr["pnl"] >= 0 else ""
                print(
                    f"    {sr['symbol']:<10} {pnl_sign}${sr['pnl']:>8.4f}  "
                    f"({sr['trades']} trades, avg ${sr['avg_pnl']:.4f})"
                )

        print(f"\n  WEBSOCKET")
        print(f"    Messages received:  {ws.stats()['total_messages']:>10,}")

        print("\n" + "=" * 60)


async def run_live_cross_exchange(args):
    """Live cross-exchange scanning + optional execution with REAL prices."""
    from config.settings import Config, FeeSchedule
    from cross_exchange.scanner import CrossExchangeScanner
    from cross_exchange.executor import CrossExchangeExecutor
    from cross_exchange.risk_manager import CrossExchangeRiskManager
    from cross_exchange.balance_tracker import BalanceTracker
    from cross_exchange.models import CrossExchangeOpportunity
    from monitoring.metrics import PipelineMetrics, TradeMetric
    from data.db import Database
    from exchange.binance_ws import BinanceWebSocket
    from exchange.bybit_ws import BybitWebSocket
    from exchange.okx_ws import OKXWebSocket
    from exchange.kucoin_ws import KuCoinWebSocket

    logger = logging.getLogger("main.live")
    config = Config()
    symbols = config.cross_exchange.symbols

    execute_mode = args.execute and not args.dry_run
    mode_str = "LIVE EXECUTION" if execute_mode else "SCAN ONLY (dry-run)"

    logger.info("=== LIVE Cross-Exchange Scanner ===")
    logger.info("Exchanges: Binance + Bybit + OKX")
    logger.info("Symbols: %s", ", ".join(symbols))
    logger.info("Mode: %s", mode_str)

    # --- Live exchange setup (for execution) ---
    live_exchanges: dict[str, object] = {}
    balance_tracker = None
    cx_executor = None
    cx_risk = None
    trade_results = []

    if execute_mode:
        bn_key = os.getenv("BINANCE_API_KEY", "")
        bn_secret = os.getenv("BINANCE_API_SECRET", "")
        by_key = os.getenv("BYBIT_API_KEY", "")
        by_secret = os.getenv("BYBIT_API_SECRET", "")
        kc_key = os.getenv("KUCOIN_API_KEY", "")
        kc_secret = os.getenv("KUCOIN_API_SECRET", "")
        kc_pass = os.getenv("KUCOIN_PASSPHRASE", "")
        ok_key = os.getenv("OKX_API_KEY", "")
        ok_secret = os.getenv("OKX_API_SECRET", "")
        ok_pass = os.getenv("OKX_PASSPHRASE", "")

        # Create live exchange instances for available keys
        if bn_key and bn_secret:
            from exchange.binance_th import BinanceTHExchange
            live_exchanges["binance"] = BinanceTHExchange(bn_key, bn_secret)
            logger.info("Binance: LIVE exchange connected")

        if kc_key and kc_secret:
            from exchange.kucoin_rest import KuCoinExchange
            live_exchanges["kucoin"] = KuCoinExchange(kc_key, kc_secret, kc_pass)
            logger.info("KuCoin: LIVE exchange connected")

        if by_key and by_secret:
            from exchange.bybit_rest import BybitExchange
            live_exchanges["bybit"] = BybitExchange(by_key, by_secret)
            logger.info("Bybit: LIVE exchange connected")

        if ok_key and ok_secret:
            from exchange.okx_rest import OKXExchange
            live_exchanges["okx"] = OKXExchange(ok_key, ok_secret, ok_pass)
            logger.info("OKX: LIVE exchange connected")

        if len(live_exchanges) < 2:
            logger.warning("Only %d exchange(s) with API keys. Need 2+ for execution.", len(live_exchanges))

        if len(live_exchanges) < 2:
            logger.error("Need at least 2 exchange API keys for execution. Got %d.", len(live_exchanges))
            execute_mode = False
        else:
            # Load pairs on all live exchanges
            for ex in live_exchanges.values():
                await ex.get_all_pairs()

            balance_tracker = BalanceTracker(live_exchanges)
            await balance_tracker.refresh_all()
            logger.info("Balances loaded: %s", balance_tracker.stats())

            cx_risk = CrossExchangeRiskManager(config.trading, config.cross_exchange)
            cx_executor = CrossExchangeExecutor(
                exchanges=live_exchanges,
                trading_config=config.trading,
                cx_config=config.cross_exchange,
            )

            # Safety confirmation
            print("\n" + "=" * 60)
            print("  ⚠  LIVE EXECUTION MODE")
            print("=" * 60)
            print(f"  Exchanges: {', '.join(live_exchanges.keys())}")
            print(f"  Max position: ${config.cross_exchange.max_position_size_usd}")
            print(f"  Daily loss limit: ${config.trading.daily_loss_limit_usd}")
            print(f"  Symbols: {len(symbols)}")
            print("=" * 60)
            confirm = input("  Type 'YES' to confirm live execution: ")
            if confirm.strip() != "YES":
                logger.info("Execution cancelled by user")
                execute_mode = False
                for ex in live_exchanges.values():
                    await ex.close()
                live_exchanges = {}

    if not execute_mode:
        logger.info("Running in SCAN-ONLY mode")

    # Fee schedules per exchange
    fee_schedules = {
        "binance": FeeSchedule("binance", taker_fee=0.00075, maker_fee=0.00075),
        "kucoin": FeeSchedule("kucoin", taker_fee=0.001, maker_fee=0.001),
        "bybit": FeeSchedule("bybit", taker_fee=0.001, maker_fee=0.001),
        "okx": FeeSchedule("okx", taker_fee=0.001, maker_fee=0.0008),
    }

    # Scanner
    cx_scanner = CrossExchangeScanner(
        symbols=symbols,
        fee_schedules=fee_schedules,
        min_net_spread=config.cross_exchange.min_net_spread,
        staleness_ms=config.cross_exchange.staleness_threshold_ms,
        dedup_cooldown_ms=config.cross_exchange.dedup_cooldown_ms,
        max_spread_anomaly=config.cross_exchange.max_spread_anomaly,
    )

    metrics = PipelineMetrics()

    # Database
    db = Database(config.database)
    await db.connect()
    session_id = await db.start_session("live_cross_exchange")

    # Track all opportunities
    all_opportunities: list[CrossExchangeOpportunity] = []
    opp_queue: asyncio.Queue = asyncio.Queue() if execute_mode else None

    # WebSocket callbacks — one per exchange, all feed into same scanner
    def make_handler(ex_id):
        def handler(ticker):
            if ticker.symbol not in cx_scanner.books:
                return
            opp = cx_scanner.update(ex_id, ticker)
            if opp is not None:
                all_opportunities.append(opp)
                net_flag = "PROFIT" if opp.net_spread > 0 else "loss"
                logger.info(
                    "OPP %s %s BUY %s @ %.6f → SELL %s @ %.6f "
                    "(gross: %.4f%%, net: %+.4f%%) [%s]",
                    opp.symbol, net_flag,
                    opp.buy_exchange, opp.buy_price,
                    opp.sell_exchange, opp.sell_price,
                    opp.gross_spread * 100, opp.net_spread * 100,
                    net_flag,
                )

                # Queue for execution if in live mode
                if execute_mode and opp.net_spread > 0 and opp_queue is not None:
                    try:
                        opp_queue.put_nowait(opp)
                    except asyncio.QueueFull:
                        pass
                else:
                    tm = TradeMetric(
                        opportunity_detected_ms=opp.timestamp_ms,
                        symbol=opp.symbol,
                        net_pnl=opp.net_spread,
                        aborted=True,
                    )
                    metrics.record(tm)
        return handler

    # Execution processor (only runs in execute mode)
    async def execute_opportunities():
        if not execute_mode or opp_queue is None:
            return
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

            # Check both exchanges are available
            if opp.buy_exchange not in live_exchanges or opp.sell_exchange not in live_exchanges:
                opp.skip_reason = f"Exchange not connected: {opp.buy_exchange} or {opp.sell_exchange}"
                await db.log_cross_opportunity(opp)
                continue

            # Execute
            cx_risk.on_arb_start()
            opp.executed = True
            opp_id = await db.log_cross_opportunity(opp)

            tm = TradeMetric(
                opportunity_detected_ms=opp.timestamp_ms,
                symbol=opp.symbol,
                execution_start_ms=time_ns() // 1_000_000,
            )

            result = await cx_executor.execute(opp)
            tm.execution_end_ms = time_ns() // 1_000_000
            tm.net_pnl = result.net_pnl
            metrics.record(tm)
            trade_results.append(result)

            if result.buy_order:
                await db.log_cross_trade(opp_id, opp.buy_exchange, result.buy_order)
            if result.sell_order:
                await db.log_cross_trade(opp_id, opp.sell_exchange, result.sell_order)

            cx_risk.record_trade_result(
                result.net_pnl,
                had_emergency_hedge=result.hedge_order is not None,
            )
            cx_risk.on_arb_end()

            if balance_tracker:
                await balance_tracker.refresh_all()

    # Create WebSocket connections for available exchanges
    bn_ws = BinanceWebSocket(
        config=config.websocket,
        on_ticker=make_handler("binance"),
        use_book_ticker=True,
    )
    kc_ws = KuCoinWebSocket(on_ticker=make_handler("kucoin"))
    by_ws = BybitWebSocket(on_ticker=make_handler("bybit"))
    ok_ws = OKXWebSocket(on_ticker=make_handler("okx"))

    sym_set = set(symbols)

    # Duration watchdog
    async def duration_watchdog():
        if args.duration > 0:
            await asyncio.sleep(args.duration)
            logger.info("Duration limit reached (%ds)", args.duration)
            await bn_ws.stop()
            await by_ws.stop()
            await ok_ws.stop()

    # Periodic stats
    async def stats_loop():
        while True:
            await asyncio.sleep(10)
            s = cx_scanner.stats()
            profitable = [o for o in all_opportunities if o.net_spread > 0]
            logger.info(
                "Live | Updates: %d | Opps: %d | Profitable: %d | "
                "BN: %d | KC: %d | BY: %d | OKX: %d",
                s["total_updates"], s["total_opportunities"],
                len(profitable),
                bn_ws.total_messages, kc_ws.total_messages,
                by_ws.total_messages, ok_ws.total_messages,
            )

    # REST price poller for Binance TH (WS is global Binance, not TH)
    bn_th_poller = None
    if execute_mode and "binance" in live_exchanges:
        async def poll_binance_th():
            """Poll Binance TH REST prices every 2s for active symbols."""
            bn_ex = live_exchanges["binance"]
            while True:
                for symbol in list(sym_set):
                    try:
                        ticker = await bn_ex.get_ticker(symbol)
                        if ticker.bid > 0 and ticker.ask > 0:
                            handler = make_handler("binance")
                            handler(ticker)
                    except Exception:
                        pass
                await asyncio.sleep(2)

        bn_th_poller = poll_binance_th

    logger.info("Connecting to exchanges...")

    await bn_ws.connect(sym_set)
    await kc_ws.connect(sym_set)
    await by_ws.connect(sym_set)
    await ok_ws.connect(sym_set)

    logger.info("All exchanges connected. Scanning...")

    watchdog_task = asyncio.create_task(duration_watchdog()) if args.duration > 0 else None
    stats_task = asyncio.create_task(stats_loop())
    executor_task = asyncio.create_task(execute_opportunities()) if execute_mode else None
    poller_task = asyncio.create_task(bn_th_poller()) if bn_th_poller else None

    try:
        await asyncio.gather(
            bn_ws.listen(),
            kc_ws.listen(),
            by_ws.listen(),
            ok_ws.listen(),
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        logger.info("Shutting down...")
        stats_task.cancel()
        if watchdog_task:
            watchdog_task.cancel()
        if poller_task:
            poller_task.cancel()
        if executor_task and opp_queue:
            await opp_queue.put(None)
            await executor_task
        await bn_ws.stop()
        await kc_ws.stop()
        await by_ws.stop()
        await ok_ws.stop()

        # Close live exchanges
        for ex in live_exchanges.values():
            await ex.close()

        # Log scan-only opportunities to DB (executed ones already logged)
        for opp in all_opportunities:
            if not opp.executed:
                await db.log_cross_opportunity(opp)

        if execute_mode and cx_executor:
            await db.end_session(
                gross_pnl=cx_executor.total_profit - cx_executor.total_loss,
                net_pnl=cx_executor.total_profit - cx_executor.total_loss,
                fees_paid=sum(r.total_fees for r in trade_results),
            )
        else:
            await db.end_session()
        await db.close()

        # Summary
        s = cx_scanner.stats()
        profitable = [o for o in all_opportunities if o.net_spread > 0]
        unprofitable = [o for o in all_opportunities if o.net_spread <= 0]

        print("\n" + "=" * 70)
        print(f"  LIVE CROSS-EXCHANGE {'EXECUTION' if execute_mode else 'SCAN'} SUMMARY")
        print("=" * 70)

        if execute_mode and cx_executor:
            e = cx_executor.stats()
            print(f"\n  EXECUTION")
            print(f"    Total trades:       {e['total_executions']:>12}")
            print(f"    Both filled:        {e['both_filled']:>12}")
            print(f"    Aborts:             {e['aborts']:>12}")
            print(f"    Emergency hedges:   {e['emergency_hedges']:>12}")
            print(f"    Maker sells:        {e.get('maker_sells', 0):>12}")
            print(f"    Win rate:           {e['win_rate']:>12}")
            pnl = e['net_pnl']
            print(f"\n  P&L (USD)")
            print(f"    Net P&L:          {'+'if pnl>=0 else ''}${pnl:>11.4f}")
            print(f"    Gross profit:      ${e['total_profit']:>11.4f}")
            print(f"    Gross loss:       -${e['total_loss']:>11.4f}")
            total_fees = sum(r.total_fees for r in trade_results)
            print(f"    Total fees:        ${total_fees:>11.4f}")

        total_msgs = bn_ws.total_messages + kc_ws.total_messages + by_ws.total_messages + ok_ws.total_messages
        print(f"\n  EXCHANGES")
        print(f"    Binance msgs:       {bn_ws.total_messages:>12,}")
        print(f"    KuCoin msgs:        {kc_ws.total_messages:>12,}")
        print(f"    Bybit msgs:         {by_ws.total_messages:>12,}")
        print(f"    OKX msgs:           {ok_ws.total_messages:>12,}")
        print(f"    Total:              {total_msgs:>12,}")

        print(f"\n  SCANNER")
        print(f"    Symbols tracked:    {s['tracked_symbols']:>12}")
        print(f"    Total updates:      {s['total_updates']:>12,}")
        print(f"    Opportunities:      {s['total_opportunities']:>12}")
        print(f"    Deduped:            {s['total_deduped']:>12}")

        print(f"\n  OPPORTUNITIES")
        print(f"    Total found:        {len(all_opportunities):>12}")
        print(f"    Profitable (net>0): {len(profitable):>12}")
        print(f"    Unprofitable:       {len(unprofitable):>12}")

        if profitable:
            nets = [o.net_spread for o in profitable]
            print(f"    Best net spread:    {max(nets):>11.4%}")
            print(f"    Avg net spread:     {sum(nets)/len(nets):>11.4%}")

            # Group by symbol
            by_symbol: dict[str, list] = {}
            for o in profitable:
                by_symbol.setdefault(o.symbol, []).append(o)

            print(f"\n  PROFITABLE BY SYMBOL")
            for sym in sorted(by_symbol, key=lambda s: -len(by_symbol[s])):
                opps = by_symbol[sym]
                avg_net = sum(o.net_spread for o in opps) / len(opps)
                best_net = max(o.net_spread for o in opps)
                routes = set(f"{o.buy_exchange}→{o.sell_exchange}" for o in opps)
                print(
                    f"    {sym:<14} {len(opps):>3}x  "
                    f"avg: {avg_net:+.4%}  best: {best_net:+.4%}  "
                    f"routes: {', '.join(routes)}"
                )

        if all_opportunities:
            print(f"\n  LAST 10 OPPORTUNITIES")
            for o in all_opportunities[-10:]:
                flag = "+" if o.net_spread > 0 else " "
                print(
                    f"    {o.symbol:<14} BUY {o.buy_exchange:<10} "
                    f"SELL {o.sell_exchange:<10} "
                    f"net: {flag}{o.net_spread:.4%}"
                )

        print("\n" + "=" * 70)


async def main():
    args = parse_args()
    setup_logging(args.log_level, dashboard=args.dashboard)

    if args.live_scan:
        await run_live_cross_exchange(args)
    elif args.cross_exchange:
        await run_cross_exchange_simulation(args)
    elif args.mode == "live":
        print("Live triangular mode not yet implemented — use --live-scan for cross-exchange")
        sys.exit(1)
    else:
        await run_simulation(args)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nGraceful shutdown complete.")
        sys.exit(0)
