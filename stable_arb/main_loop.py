"""Stablecoin depeg monitor — continuous monitoring loop."""

import asyncio
import logging

from config.settings import WebSocketConfig
from exchange.binance_ws import BinanceWebSocket
from exchange.kucoin_ws import KuCoinWebSocket
from stable_arb.alert_manager import AlertManager
from stable_arb.detector import DepegDetector
from stable_arb.models import DepegSeverity
from stable_arb.price_aggregator import StablePriceAggregator

logger = logging.getLogger(__name__)


async def run_depeg_monitor(duration: int = 0):
    """
    Continuous stablecoin depeg monitor.

    Connects to KuCoin + Binance WebSocket for real-time stable pair prices.
    Detects depegs via threshold + confirmation. Alerts on detection.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s │ %(levelname)-7s │ %(name)-22s │ %(message)s",
        datefmt="%H:%M:%S",
    )

    logger.info("=== Stablecoin Depeg Monitor ===")
    logger.info("Monitoring: USDT, USDC, DAI, FDUSD, TUSD")
    logger.info("Alert: >0.3%% | Execute: >0.5%% | Crisis: >5%%")

    detector = DepegDetector()
    alert_mgr = AlertManager(cooldown_sec=300)

    def on_price(price):
        event = detector.update(price)
        if event and event.severity != DepegSeverity.NORMAL:
            alert_mgr.alert(event)

    aggregator = StablePriceAggregator(on_price=on_price)

    # WebSocket handlers
    def kc_handler(ticker):
        aggregator.handle_ticker("kucoin", ticker)

    def bn_handler(ticker):
        aggregator.handle_ticker("binance", ticker)

    # Connect to exchanges
    ws_symbols = StablePriceAggregator.get_ws_symbols()
    logger.info("Subscribing to %d stable pairs on KuCoin + Binance", len(ws_symbols))

    kc_ws = KuCoinWebSocket(on_ticker=kc_handler)
    bn_ws = BinanceWebSocket(
        config=WebSocketConfig(),
        on_ticker=bn_handler,
        use_book_ticker=True,
    )

    await kc_ws.connect(ws_symbols)
    await bn_ws.connect(ws_symbols)

    logger.info("Connected. Monitoring stable prices...")

    # Periodic status
    async def status_loop():
        while True:
            await asyncio.sleep(60)
            status = detector.get_status()
            stats = detector.stats()
            parts = []
            for stable, info in sorted(status.items()):
                parts.append(
                    f"{stable}=${info['price']:.4f}({info['deviation']:.3f}%)"
                )
            logger.info(
                "Stable prices: %s | Updates: %d | Alerts: %d",
                " | ".join(parts) if parts else "waiting...",
                stats["total_updates"], stats["total_alerts"],
            )

    # Duration watchdog
    async def watchdog():
        if duration > 0:
            await asyncio.sleep(duration)
            logger.info("Duration limit reached")
            await kc_ws.stop()
            await bn_ws.stop()

    status_task = asyncio.create_task(status_loop())
    watchdog_task = asyncio.create_task(watchdog()) if duration > 0 else None

    try:
        await asyncio.gather(
            kc_ws.listen(),
            bn_ws.listen(),
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        logger.info("Shutting down depeg monitor...")
        status_task.cancel()
        if watchdog_task:
            watchdog_task.cancel()
        await kc_ws.stop()
        await bn_ws.stop()

        # Summary
        status = detector.get_status()
        stats = detector.stats()
        print(f"\n{'='*60}")
        print(f"  DEPEG MONITOR SUMMARY")
        print(f"{'='*60}")
        print(f"  Updates processed: {stats['total_updates']:,}")
        print(f"  Alerts fired:      {stats['total_alerts']}")
        print(f"  Stables monitored: {', '.join(stats['monitored_stables'])}")
        if status:
            print(f"\n  FINAL PRICES:")
            for stable, info in sorted(status.items()):
                flag = " ⚠" if info["deviation"] > 0.3 else ""
                print(f"    {stable:<6} ${info['price']:.4f}  dev: {info['deviation']:.4f}%  [{info['severity']}]{flag}")
        print(f"{'='*60}\n")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Stablecoin Depeg Monitor")
    parser.add_argument("--duration", type=int, default=0, help="Seconds (0=forever)")
    args = parser.parse_args()

    asyncio.run(run_depeg_monitor(args.duration))


if __name__ == "__main__":
    main()
