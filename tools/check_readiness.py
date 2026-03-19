"""Check if everything is ready for funding rate arb."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from exchange.kucoin_rest import KuCoinExchange
from funding_arb.kucoin_futures import KuCoinFutures
from funding_arb.timing import funding_info
from funding_arb.scanner import FundingScanner


async def main():
    spot = KuCoinExchange(
        os.getenv("KUCOIN_API_KEY", ""),
        os.getenv("KUCOIN_API_SECRET", ""),
        os.getenv("KUCOIN_PASSPHRASE", ""),
    )
    futures = KuCoinFutures(
        os.getenv("KUCOIN_API_KEY", ""),
        os.getenv("KUCOIN_API_SECRET", ""),
        os.getenv("KUCOIN_PASSPHRASE", ""),
    )

    # Balances
    spot_bal = await spot.get_all_balances()
    fut_bal = await futures.get_account_balance()
    spot_usdt = spot_bal.get("USDT", 0)
    fut_usdt = float(fut_bal.get("availableBalance", 0))

    print("KUCOIN BALANCES:")
    print(f"  Spot USDT:    ${spot_usdt:.2f}")
    print(f"  Futures USDT: ${fut_usdt:.2f}")
    for a, v in sorted(spot_bal.items()):
        if a != "USDT" and v > 0.001:
            print(f"  Spot {a}:  {v:.4f}")

    # Timing
    fi = funding_info()
    print(f"\nTIMING:")
    print(f"  Next funding:  {fi['next_funding']}")
    print(f"  Minutes until: {fi['minutes_until']:.0f}")
    print(f"  In entry window (T-2h): {fi['in_entry_window']}")

    # Scan
    scanner = FundingScanner(min_funding_rate=0.0005)
    opps = await scanner.scan()
    longs_pay = [o for o in opps if o.is_longs_pay and o.abs_rate >= 0.001]

    print(f"\nTOP OPPORTUNITIES (longs pay, >=0.10%):")
    if longs_pay:
        for o in longs_pay[:8]:
            be = 0.0024 / abs(o.funding_rate)
            print(f"  {o.symbol:<16} {o.funding_rate:.4%}/8h  break-even: {be:.1f} periods ({be*8:.0f}h)")
    else:
        print("  (none above threshold right now)")

    # Readiness
    print(f"\n--- READINESS ---")
    checks = []
    if spot_usdt >= 10:
        checks.append(("Spot USDT >= $10", True))
    else:
        checks.append(("Spot USDT >= $10", False))
    if fut_usdt >= 10:
        checks.append(("Futures USDT >= $10", True))
    else:
        checks.append(("Futures USDT >= $10", False))
    checks.append(("API keys configured", bool(os.getenv("KUCOIN_API_KEY"))))
    checks.append(("Opportunities exist", len(longs_pay) > 0))

    all_ok = True
    for label, ok in checks:
        status = "OK" if ok else "FAIL"
        print(f"  [{status}] {label}")
        if not ok:
            all_ok = False

    if all_ok:
        print(f"\n  ALL SYSTEMS GO! Run: python -m funding_arb.main_loop")
    elif fut_usdt < 10:
        print(f"\n  Transfer USDT to futures: KuCoin -> Assets -> Transfer -> Trading to Futures")

    await spot.close()
    await futures.close()


if __name__ == "__main__":
    asyncio.run(main())
