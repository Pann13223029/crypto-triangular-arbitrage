"""Live cross-exchange spread scanner — real prices from 3 exchanges."""

import asyncio
import sys
sys.path.insert(0, ".")

from config.settings import Config, FeeSchedule
from core.models import Ticker
from cross_exchange.scanner import CrossExchangeScanner
from exchange.binance_ws import BinanceWebSocket
from exchange.bybit_ws import BybitWebSocket
from exchange.okx_ws import OKXWebSocket


async def main():
    config = Config()
    symbols = config.cross_exchange.symbols
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 30

    fees = {
        "binance": FeeSchedule("binance", taker_fee=0.00075),
        "bybit": FeeSchedule("bybit", taker_fee=0.001),
        "okx": FeeSchedule("okx", taker_fee=0.001),
    }

    scanner = CrossExchangeScanner(
        symbols=symbols,
        fee_schedules=fees,
        min_net_spread=0.0,  # Log everything
        staleness_ms=2000,
        dedup_cooldown_ms=5000,
        max_spread_anomaly=config.cross_exchange.max_spread_anomaly,
    )

    opportunities = []

    def make_handler(ex_id):
        def handler(ticker):
            if ticker.symbol not in scanner.books:
                return
            opp = scanner.update(ex_id, ticker)
            if opp:
                opportunities.append(opp)
                net_flag = "PROFIT" if opp.net_spread > 0 else "loss"
                print(
                    f"  {opp.symbol:<12} BUY {opp.buy_exchange:<10} "
                    f"SELL {opp.sell_exchange:<10} "
                    f"gross: {opp.gross_spread:.4%}  net: {opp.net_spread:+.4%}  "
                    f"[{net_flag}]"
                )
        return handler

    bn = BinanceWebSocket(on_ticker=make_handler("binance"), use_book_ticker=True)
    by = BybitWebSocket(on_ticker=make_handler("bybit"))
    ok = OKXWebSocket(on_ticker=make_handler("okx"))

    sym_set = set(symbols)
    print(f"Scanning {len(symbols)} symbols across Binance + Bybit + OKX for {duration}s...")
    print(f"Anomaly filter: >{config.cross_exchange.max_spread_anomaly:.0%} spread rejected\n")

    await bn.connect(sym_set)
    await by.connect(sym_set)
    await ok.connect(sym_set)

    async def listen_all():
        await asyncio.gather(bn.listen(), by.listen(), ok.listen())

    try:
        await asyncio.wait_for(listen_all(), timeout=duration)
    except asyncio.TimeoutError:
        pass

    await bn.stop()
    await by.stop()
    await ok.stop()

    # Summary
    s = scanner.stats()
    print(f"\n{'='*70}")
    print(f"Updates: {s['total_updates']:,} | Opportunities: {s['total_opportunities']}")
    print(f"Deduped: {s['total_deduped']} | Preflight rejected: {s.get('preflight_rejected', 0)}")

    if opportunities:
        profitable = [o for o in opportunities if o.net_spread > 0]
        print(f"\nTotal opportunities:  {len(opportunities)}")
        print(f"Profitable (net>0):   {len(profitable)}")

        if profitable:
            nets = [o.net_spread for o in profitable]
            print(f"Best net spread:      {max(nets):.4%}")
            print(f"Avg net spread:       {sum(nets)/len(nets):.4%}")

            print(f"\nPROFITABLE OPPORTUNITIES:")
            for o in sorted(profitable, key=lambda x: -x.net_spread):
                print(
                    f"  {o.symbol:<12} BUY {o.buy_exchange:<10} "
                    f"SELL {o.sell_exchange:<10} "
                    f"net: {o.net_spread:+.4%}"
                )
    else:
        print("\nNo opportunities found.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
