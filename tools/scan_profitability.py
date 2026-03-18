"""Quick diagnostic: scan live prices and show the best/worst triangle profits."""

import asyncio
import sys
sys.path.insert(0, ".")

from config.settings import Config
from core.calculator import ProfitCalculator
from core.models import Ticker
from core.scanner import TriangleScanner
from core.triangle import TriangleGraph
from exchange.binance_rest import BinanceREST
from exchange.binance_ws import BinanceWebSocket


async def main():
    config = Config()

    # Fetch pairs and build triangles
    rest = BinanceREST()
    pairs = await rest.get_all_pairs(quote_assets=config.scanner.quote_assets)
    await rest.close()

    graph = TriangleGraph()
    graph.load_pairs(pairs)
    triangles = graph.discover_triangles()
    symbols = graph.get_subscribed_symbols()

    print(f"Loaded {len(pairs)} pairs, {len(triangles)} triangles, {len(symbols)} symbols")

    calculator = ProfitCalculator(fee_rate=config.fees.effective_fee)
    # Accept everything — no threshold
    scanner = TriangleScanner(graph, calculator, min_profit=-1.0)

    # Collect tickers
    collected = {}
    done = asyncio.Event()

    def on_ticker(ticker: Ticker):
        collected[ticker.symbol] = ticker
        scanner.tickers[ticker.symbol] = ticker
        if len(collected) >= len(symbols) * 0.8:  # Wait for 80% of symbols
            done.set()

    ws = BinanceWebSocket(on_ticker=on_ticker)
    connect_task = asyncio.create_task(ws.listen_with_reconnect(symbols))

    print("Waiting for price data (10s)...")
    try:
        await asyncio.wait_for(done.wait(), timeout=15)
    except asyncio.TimeoutError:
        pass

    print(f"Got prices for {len(collected)}/{len(symbols)} symbols\n")

    # Calculate all triangle profits
    results = []
    for tri in triangles:
        fwd, rev, direction = calculator.triangle_profit(tri, scanner.tickers)
        best = max(fwd, rev)
        if best > -1:  # Valid calculation
            results.append({
                "triangle": " → ".join(tri.assets),
                "forward": fwd,
                "reverse": rev,
                "best": best,
                "direction": direction.value,
            })

    # Sort by best profit
    results.sort(key=lambda x: x["best"], reverse=True)

    # Print top 20 and bottom 5
    print(f"{'Triangle':<30} {'Best':>10} {'Forward':>10} {'Reverse':>10} {'Dir':<8}")
    print("─" * 72)

    print("\nTOP 20 (closest to profitable):")
    for r in results[:20]:
        color = "\033[32m" if r["best"] > 0 else "\033[33m" if r["best"] > -0.002 else "\033[0m"
        print(f"{color}{r['triangle']:<30} {r['best']:>9.4%} {r['forward']:>9.4%} {r['reverse']:>9.4%} {r['direction']:<8}\033[0m")

    print(f"\nBOTTOM 5 (most negative):")
    for r in results[-5:]:
        print(f"{r['triangle']:<30} {r['best']:>9.4%} {r['forward']:>9.4%} {r['reverse']:>9.4%} {r['direction']:<8}")

    # Distribution
    print(f"\n--- DISTRIBUTION (net of {config.fees.effective_fee*3:.3%} fees) ---")
    brackets = [
        ("> +0.1%", lambda x: x > 0.001),
        ("+0.05% to +0.1%", lambda x: 0.0005 < x <= 0.001),
        ("0 to +0.05%", lambda x: 0 < x <= 0.0005),
        ("-0.05% to 0", lambda x: -0.0005 < x <= 0),
        ("-0.1% to -0.05%", lambda x: -0.001 < x <= -0.0005),
        ("-0.225% to -0.1%", lambda x: -0.00225 < x <= -0.001),
        ("< -0.225%", lambda x: x <= -0.00225),
    ]
    for label, fn in brackets:
        count = sum(1 for r in results if fn(r["best"]))
        bar = "█" * count
        print(f"  {label:>20}: {count:>4}  {bar}")

    print(f"\n  Total valid triangles: {len(results)}")

    await ws.stop()
    connect_task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
