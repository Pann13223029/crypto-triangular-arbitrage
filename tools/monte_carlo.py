"""Monte Carlo simulation for funding rate arbitrage strategy."""

import random
import statistics
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def simulate_funding_arb(
    capital: float = 30.0,
    reserve_pct: float = 0.20,
    leverage: int = 2,
    months: int = 6,
    simulations: int = 10000,
    # Funding rate distribution (per 8h period)
    avg_entry_rate: float = 0.002,  # 0.20% average entry rate
    rate_std: float = 0.001,  # Standard deviation
    rate_decay: float = 0.7,  # Rate decays to 70% of entry rate each period
    min_rate: float = 0.0005,  # Exit when rate drops below this
    # Trade parameters
    avg_hold_periods: int = 6,  # ~48h average hold (6 × 8h)
    trades_per_month: float = 4,  # Average trades per month
    # Costs
    entry_exit_fee_pct: float = 0.0032,  # 0.32% round trip
    # Risk
    liquidation_prob_per_trade: float = 0.02,  # 2% chance per trade
    liquidation_loss_pct: float = 0.50,  # Lose 50% of position margin on liquidation
    stop_loss_prob: float = 0.05,  # 5% chance stop-loss triggers (not full liquidation)
    stop_loss_pct: float = 0.15,  # 15% loss on stop-loss
):
    """
    Run Monte Carlo simulation of funding rate arbitrage.

    Returns distribution of outcomes after N months.
    """
    deployable = capital * (1 - reserve_pct)
    margin_per_trade = deployable / 2  # Half for spot, half for futures margin
    notional = margin_per_trade * leverage

    results = []
    monthly_paths = []

    for sim in range(simulations):
        balance = capital
        monthly_pnl = []

        for month in range(months):
            month_pnl = 0.0

            # Number of trades this month (Poisson-distributed)
            n_trades = max(1, int(random.gauss(trades_per_month, 1.5)))

            for trade in range(n_trades):
                if balance <= capital * 0.10:  # Stop if < 10% capital left
                    break

                current_deployable = balance * (1 - reserve_pct)
                current_margin = current_deployable / 2
                current_notional = current_margin * leverage

                # Entry rate (log-normal to stay positive)
                entry_rate = max(0.0005, random.gauss(avg_entry_rate, rate_std))

                # Simulate hold periods
                hold_periods = max(1, int(random.gauss(avg_hold_periods, 2)))
                trade_funding = 0.0
                current_rate = entry_rate

                for period in range(hold_periods):
                    # Rate decays + noise
                    current_rate = current_rate * rate_decay + random.gauss(0, rate_std * 0.3)
                    current_rate = max(-0.002, current_rate)  # Can go slightly negative

                    if current_rate < min_rate and period > 0:
                        break  # Exit — rate too low

                    # Funding received (or paid if negative)
                    payment = current_notional * current_rate
                    trade_funding += payment

                # Entry/exit fees
                fees = current_notional * entry_exit_fee_pct

                # Risk events
                risk_roll = random.random()

                if risk_roll < liquidation_prob_per_trade:
                    # Liquidation — lose margin on futures side
                    loss = current_margin * liquidation_loss_pct
                    trade_pnl = -loss
                elif risk_roll < liquidation_prob_per_trade + stop_loss_prob:
                    # Stop-loss triggered — controlled loss
                    loss = current_notional * stop_loss_pct * 0.3  # Partial loss (hedged)
                    trade_pnl = trade_funding - fees - loss
                else:
                    # Normal trade
                    trade_pnl = trade_funding - fees

                balance += trade_pnl
                month_pnl += trade_pnl

            monthly_pnl.append(month_pnl)

        results.append(balance - capital)  # Total P&L
        monthly_paths.append(monthly_pnl)

    return results, monthly_paths, capital


def analyze_results(results, monthly_paths, capital, months):
    """Analyze and display simulation results."""
    n = len(results)
    results_sorted = sorted(results)

    # Basic stats
    mean_pnl = statistics.mean(results)
    median_pnl = statistics.median(results)
    std_pnl = statistics.stdev(results) if len(results) > 1 else 0

    # Percentiles
    p5 = results_sorted[int(n * 0.05)]
    p25 = results_sorted[int(n * 0.25)]
    p50 = results_sorted[int(n * 0.50)]
    p75 = results_sorted[int(n * 0.75)]
    p95 = results_sorted[int(n * 0.95)]

    # Win rate
    profitable = sum(1 for r in results if r > 0)
    win_rate = profitable / n

    # Worst/best
    worst = min(results)
    best = max(results)

    # Risk of ruin (losing > 50% of capital)
    ruin = sum(1 for r in results if r < -capital * 0.5) / n

    # Monthly stats
    all_monthly = [pnl for path in monthly_paths for pnl in path]
    avg_monthly = statistics.mean(all_monthly) if all_monthly else 0

    print(f"\n{'='*70}")
    print(f"  MONTE CARLO SIMULATION — FUNDING RATE ARBITRAGE")
    print(f"{'='*70}")
    print(f"  Capital: ${capital:.0f} | Simulations: {n:,} | Timeframe: {months} months")
    print(f"{'='*70}")

    print(f"\n  OUTCOME DISTRIBUTION (after {months} months)")
    print(f"  {'─'*50}")
    print(f"  5th percentile (worst realistic):  ${p5:>+10.2f}")
    print(f"  25th percentile:                   ${p25:>+10.2f}")
    print(f"  MEDIAN (50th):                     ${p50:>+10.2f}")
    print(f"  75th percentile:                   ${p75:>+10.2f}")
    print(f"  95th percentile (best realistic):  ${p95:>+10.2f}")

    print(f"\n  STATISTICS")
    print(f"  {'─'*50}")
    print(f"  Mean P&L:          ${mean_pnl:>+10.2f}")
    print(f"  Median P&L:        ${median_pnl:>+10.2f}")
    print(f"  Std deviation:     ${std_pnl:>10.2f}")
    print(f"  Best outcome:      ${best:>+10.2f}")
    print(f"  Worst outcome:     ${worst:>+10.2f}")

    print(f"\n  PROBABILITIES")
    print(f"  {'─'*50}")
    print(f"  Profitable:        {win_rate:>10.1%}")
    print(f"  Break-even or up:  {sum(1 for r in results if r >= 0)/n:>10.1%}")
    print(f"  Lose > 25%:        {sum(1 for r in results if r < -capital*0.25)/n:>10.1%}")
    print(f"  Lose > 50% (ruin): {ruin:>10.1%}")

    print(f"\n  MONTHLY")
    print(f"  {'─'*50}")
    print(f"  Avg monthly P&L:   ${avg_monthly:>+10.2f}")
    print(f"  Avg monthly ROI:   {avg_monthly/capital*100:>+10.1f}%")
    print(f"  Annualized ROI:    {avg_monthly/capital*12*100:>+10.1f}%")

    # Histogram
    print(f"\n  P&L DISTRIBUTION")
    print(f"  {'─'*50}")
    buckets = [
        ("  Loss > $10", lambda x: x < -10),
        ("  Loss $5-10", lambda x: -10 <= x < -5),
        ("  Loss $2-5", lambda x: -5 <= x < -2),
        ("  Loss $0-2", lambda x: -2 <= x < 0),
        ("  Gain $0-2", lambda x: 0 <= x < 2),
        ("  Gain $2-5", lambda x: 2 <= x < 5),
        ("  Gain $5-10", lambda x: 5 <= x < 10),
        ("  Gain > $10", lambda x: x >= 10),
    ]

    for label, fn in buckets:
        count = sum(1 for r in results if fn(r))
        pct = count / n * 100
        bar = "█" * int(pct / 2)
        print(f"  {label:<16} {pct:>5.1f}%  {bar}")

    print(f"\n{'='*70}")

    # Scaling analysis
    print(f"\n  SCALING PROJECTION (same strategy, more capital)")
    print(f"  {'─'*50}")
    for scale_capital in [30, 100, 500, 1000, 3000]:
        scale = scale_capital / capital
        scaled_median = p50 * scale
        scaled_monthly = avg_monthly * scale
        print(
            f"  ${scale_capital:>5}  →  median {months}mo: ${scaled_median:>+8.2f}"
            f"  |  avg/mo: ${scaled_monthly:>+8.2f}"
            f"  |  APY: {scaled_monthly/scale_capital*12*100:>+6.1f}%"
        )

    print(f"\n{'='*70}\n")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Monte Carlo — Funding Rate Arb")
    parser.add_argument("--capital", type=float, default=30, help="Starting capital")
    parser.add_argument("--months", type=int, default=6, help="Simulation months")
    parser.add_argument("--sims", type=int, default=10000, help="Number of simulations")
    parser.add_argument("--avg-rate", type=float, default=0.002, help="Average entry funding rate")
    parser.add_argument("--trades", type=float, default=4, help="Average trades per month")
    parser.add_argument("--hold", type=int, default=6, help="Average hold periods (x8h)")
    args = parser.parse_args()

    results, paths, capital = simulate_funding_arb(
        capital=args.capital,
        months=args.months,
        simulations=args.sims,
        avg_entry_rate=args.avg_rate,
        trades_per_month=args.trades,
        avg_hold_periods=args.hold,
    )

    analyze_results(results, paths, capital, args.months)


if __name__ == "__main__":
    main()
