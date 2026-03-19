"""Funding rate arb CLI — scan, monitor, and manage positions."""

import asyncio
import logging
import sys

from funding_arb.scanner import FundingScanner
from funding_arb.models import FundingDirection

logger = logging.getLogger(__name__)


async def run_funding_scan():
    """One-shot scan: find all funding rate opportunities."""
    scanner = FundingScanner(min_funding_rate=0.0005)  # Show from 0.05%

    print("Scanning KuCoin funding rates...\n")
    opportunities = await scanner.scan()

    if not opportunities:
        print("No funding rate opportunities found above threshold.")
        return

    # Separate by direction
    longs_pay = [o for o in opportunities if o.is_longs_pay]
    shorts_pay = [o for o in opportunities if not o.is_longs_pay]

    # Header
    print(f"{'Symbol':<16} {'Rate/8h':>10} {'Rate/Day':>10} {'APY':>8} {'Direction':>12} {'Tradeable':>10}")
    print("-" * 72)

    for o in opportunities[:30]:
        tradeable = "YES" if o.is_longs_pay and o.abs_rate >= 0.001 else "monitor"
        if o.is_longs_pay and o.abs_rate >= 0.001:
            tradeable = "*** YES ***"

        print(
            f"{o.symbol:<16} {o.funding_rate:>9.4%} {o.daily_rate:>9.4%} "
            f"{o.annualized * 100:>7.0f}% "
            f"{'LONGS PAY' if o.is_longs_pay else 'SHORTS PAY':>12} "
            f"{tradeable:>10}"
        )

    print(f"\nTotal above threshold: {len(opportunities)}")
    print(f"LONGS_PAY (tradeable in v1): {len(longs_pay)}")
    print(f"SHORTS_PAY (need margin short): {len(shorts_pay)}")

    # Best tradeable
    tradeable = [o for o in longs_pay if o.abs_rate >= 0.001]
    if tradeable:
        best = tradeable[0]
        daily_profit = 24 * best.funding_rate * 3  # $24 position, rate*3/day
        print(f"\n{'='*60}")
        print(f"  BEST OPPORTUNITY: {best.symbol}")
        print(f"  Rate: {best.funding_rate:.4%}/8h = {best.daily_rate:.4%}/day")
        print(f"  Strategy: Long {best.base_asset} spot + Short {best.symbol} perp")
        fee_cost = 0.0024  # 0.24% total entry+exit as decimal
        be_periods = fee_cost / abs(best.funding_rate)
        print(f"  Est. daily income on $24: ${24 * abs(best.daily_rate):.4f}")
        print(f"  Break-even (fees): {be_periods:.1f} periods ({be_periods * 8:.0f}h)")
        print(f"{'='*60}")


async def run_funding_monitor(duration: int = 0):
    """Continuous monitor: scan every 8 hours, alert on opportunities."""
    scanner = FundingScanner(min_funding_rate=0.001)

    print("Funding rate monitor started. Scanning every 10 minutes...")
    print("(Funding timestamps: 00:00, 08:00, 16:00 UTC)\n")

    scan_interval = 600  # 10 minutes for monitoring
    elapsed = 0

    while True:
        opportunities = await scanner.scan()
        tradeable = [o for o in opportunities if o.is_longs_pay]

        if tradeable:
            best = tradeable[0]
            print(f"\n🔔 {len(tradeable)} tradeable opportunities!")
            for o in tradeable[:5]:
                print(
                    f"  {o.symbol:<16} {o.funding_rate:>9.4%}/8h  "
                    f"${24 * abs(o.daily_rate):.4f}/day on $24"
                )

            # Sound alert
            try:
                import subprocess
                if sys.platform == "darwin":
                    subprocess.Popen(
                        ["afplay", "/System/Library/Sounds/Glass.aiff"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
            except Exception:
                print("\a", end="", flush=True)
        else:
            print(f"  No opportunities above 0.10% threshold ({len(opportunities)} total scanned)")

        elapsed += scan_interval
        if duration > 0 and elapsed >= duration:
            break

        await asyncio.sleep(scan_interval)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Funding Rate Arbitrage")
    parser.add_argument("command", choices=["scan", "monitor"],
                        help="scan = one-shot, monitor = continuous")
    parser.add_argument("--duration", type=int, default=0,
                        help="Monitor duration in seconds (0 = forever)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s │ %(levelname)-7s │ %(name)-20s │ %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.command == "scan":
        asyncio.run(run_funding_scan())
    elif args.command == "monitor":
        asyncio.run(run_funding_monitor(args.duration))


if __name__ == "__main__":
    main()
