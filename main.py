"""Crypto Triangular Arbitrage — Entry Point"""

import argparse
import asyncio
import sys


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
        help="Enable CLI dashboard",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan for opportunities without executing",
    )
    return parser.parse_args()


async def main():
    args = parse_args()
    print(f"Starting in {args.mode} mode...")
    # TODO: Initialize components and start trading loop


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nGraceful shutdown...")
        sys.exit(0)
