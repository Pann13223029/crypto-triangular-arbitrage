# Crypto Arbitrage Trading System

A Python-based arbitrage system supporting three strategies:

1. **Triangular Arbitrage** — 3-pair cycles on single exchange
2. **Cross-Exchange Arbitrage** — buy low on one exchange, sell high on another
3. **Funding Rate Arbitrage** — delta-neutral, collect funding payments (active strategy)

```mermaid
graph LR
    subgraph FundingArb["Funding Rate Arb (active)"]
        SPOT["Long Spot"]
        PERP["Short Perp"]
        FUND["Collect Funding<br/>Every 8 Hours"]
        SPOT --- PERP
        PERP -->|"delta neutral"| FUND
    end

    subgraph CrossArb["Cross-Exchange Arb"]
        KC["Exchange A<br/>Buy cheap"]
        BN["Exchange B<br/>Sell high"]
        KC -->|"spread"| BN
    end

    subgraph TriArb["Triangular Arb"]
        A["USDT"] --> B["BTC"] --> C["ETH"] --> A
    end

    style FundingArb fill:#22c55e,stroke:#16a34a,color:#fff
    style CrossArb fill:#1e293b,stroke:#334155,color:#fff
    style TriArb fill:#1e293b,stroke:#334155,color:#fff
```

## Strategy Evolution

| Strategy | Status | Result |
|----------|--------|--------|
| Triangular arb | Built & tested | Market too efficient for retail (0.008% spread vs 0.225% fees) |
| Cross-exchange arb | Built & tested | Inventory risk: tokens drop 10-42% while holding, wiping arb profit |
| **Funding rate arb** | **Active** | **Delta-neutral: no directional risk, collect funding every 8h** |

## Funding Rate Arbitrage

The active strategy. Earns income by exploiting funding rate differences on perpetual futures.

```mermaid
sequenceDiagram
    participant S as Scanner
    participant H as Human
    participant E as Executor
    participant KC as KuCoin

    S->>S: Scan 550 perps every 8h
    S->>H: "LRC at 0.37%/8h — Enter? [y/n]"
    H->>E: Approve (y)

    par Simultaneous entry
        E->>KC: Buy LRC spot
        E->>KC: Short LRCUSDTM perp
    end
    E->>KC: Set -15% stop-loss

    loop Every 8 hours
        KC-->>E: Funding payment received
        E->>E: Check: rate still > 0.05%?
    end

    Note over E: Rate drops below 0.05%
    par Exit
        E->>KC: Close short (buy to cover)
        E->>KC: Sell spot
    end
    E->>H: "Position closed. Net: +$0.27"
```

### How It Works

1. **Scanner** finds perpetual contracts with high funding rates (>0.10%/8h)
2. **Human approves** entry (or auto-enter on strong signals)
3. **Executor** simultaneously buys spot + shorts futures = delta-neutral
4. **Every 8 hours**, longs pay shorts → you collect funding
5. **Auto-exit** when funding drops below 0.05% or after 24h max hold

### Current Opportunities (live scan)

```bash
python -m funding_arb.cli scan
```

Example output:
```
LRCUSDTM          0.3680%/8h  break-even: 5h   *** YES ***
VANRYUSDTM        0.2754%/8h  break-even: 7h   *** YES ***
SUPRAUSDTM        0.2317%/8h  break-even: 8h   *** YES ***
```

### Safety

| Protection | Detail |
|-----------|--------|
| **Delta-neutral** | Long spot + short perp = zero price exposure |
| **Isolated margin** | Only position margin at risk, not whole account |
| **2x leverage max** | Survives 45% adverse move before liquidation |
| **Exchange stop-loss** | -15% on exchange (works even if bot crashes) |
| **Auto-exit** | Funding < 0.05%, or 24h max hold, or 1.5% basis divergence |
| **State persistence** | JSON state file survives crashes, reconciles on startup |
| **Orphan detection** | Alerts (never auto-closes) if one leg is missing |

## Quick Start

```bash
# Clone
git clone https://github.com/Pann13223029/crypto-triangular-arbitrage.git
cd crypto-triangular-arbitrage

# Setup
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with KuCoin API key + secret + passphrase
```

### Funding Rate Arb (recommended)

```bash
# Scan for opportunities
python -m funding_arb.cli scan

# Run the bot (scans, asks approval, trades, monitors)
python -m funding_arb.main_loop

# Check readiness (balances, timing, opportunities)
python tools/check_readiness.py
```

### Cross-Exchange Arb

```bash
# Live scan across 4 exchanges (read-only)
python main.py --live-scan --duration 60 --dry-run

# Simulated cross-exchange trading
python main.py --cross-exchange --duration 120
```

### Triangular Arb

```bash
python main.py --mode simulation --duration 120
```

## Project Structure

```
├── funding_arb/             # Funding rate arbitrage (active strategy)
│   ├── scanner.py           # Scans 550 KuCoin perps for funding spikes
│   ├── executor.py          # Enters/exits spot+futures positions
│   ├── position_manager.py  # Entry/exit logic, risk controls
│   ├── kucoin_futures.py    # KuCoin Futures API client
│   ├── main_loop.py         # State machine (IDLE→SCAN→ENTER→MONITOR→EXIT)
│   ├── timing.py            # Funding timestamp utilities
│   ├── state.py             # JSON state persistence + JSONL ledger
│   ├── models.py            # FundingOpportunity, FundingPosition
│   └── cli.py               # CLI: scan, monitor commands
│
├── cross_exchange/          # Cross-exchange arbitrage
│   ├── book.py              # Aggregated order book across exchanges
│   ├── scanner.py           # Spread detection with pre-flight filter
│   ├── executor.py          # Simultaneous orders + emergency hedge
│   ├── risk_manager.py      # Kill switch, imbalance filtering
│   ├── balance_tracker.py   # Multi-exchange balance aggregation
│   ├── pair_manager.py      # Adaptive pair selection (1 active + 4 on-deck)
│   ├── pair_discovery.py    # Full pair scan across exchanges
│   └── models.py            # CrossExchangeOpportunity, etc.
│
├── core/                    # Triangular arbitrage engine
│   ├── triangle.py          # Graph-based triangle discovery
│   ├── scanner.py           # Vectorized opportunity detection
│   ├── calculator.py        # Numpy profit calculation
│   └── models.py            # Ticker, OrderBook, Order, etc.
│
├── exchange/                # Exchange adapters (6 exchanges)
│   ├── binance_th.py        # Binance Thailand (live trading)
│   ├── binance_live.py      # Binance Global (live trading)
│   ├── binance_ws.py        # Binance WebSocket (bookTicker)
│   ├── binance_rest.py      # Binance REST
│   ├── kucoin_rest.py       # KuCoin REST (spot)
│   ├── kucoin_ws.py         # KuCoin WebSocket
│   ├── okx_rest.py          # OKX REST
│   ├── okx_ws.py            # OKX WebSocket
│   ├── bybit_rest.py        # Bybit REST
│   ├── bybit_ws.py          # Bybit WebSocket
│   ├── simulator.py         # Paper trading simulator
│   ├── multi_sim.py         # Multi-exchange O-U simulator
│   └── base.py              # Abstract ExchangeBase interface
│
├── execution/               # Triangular arb execution
├── rebalancing/             # Threshold-based + opportunity-aware
├── monitoring/              # Pipeline metrics, per-symbol P&L
├── dashboard/               # Rich CLI monitor
├── data/                    # SQLite logging, price cache
├── backtest/                # Data recorder & replayer
├── tools/                   # Diagnostic scripts
│   ├── check_readiness.py   # Pre-trade readiness check
│   ├── scan_cross_exchange.py
│   └── scan_profitability.py
├── tests/                   # 147 tests
└── config/                  # Dataclass-based configuration
```

## Architecture Documents

- [architecture.md](architecture.md) — Triangular arb design (10-expert panel)
- [architecture-cross-exchange.md](architecture-cross-exchange.md) — Cross-exchange design (9-expert panel)

## Exchange Support

| Exchange | Spot | Futures | WebSocket | Status |
|----------|------|---------|-----------|--------|
| **KuCoin** | REST + WS | REST (futures) | ticker, orderbook | **Active** (funding arb) |
| **Binance TH** | REST | — | bookTicker | Active (cross-exchange sell) |
| **Binance Global** | REST | — | bookTicker | Price feed |
| **OKX** | REST + WS | — | tickers | Price feed only |
| **Bybit** | REST + WS | — | orderbook.1 | Price feed only |

## Configuration

Key parameters in `config/settings.py` and `funding_arb/main_loop.py`:

```yaml
# Funding Rate Arb
leverage:           2x (isolated margin)
stop_loss:          -15% on exchange
min_funding_rate:   0.10% per 8h to enter
exit_funding_rate:  0.05% per 8h to exit
max_hold:           24 hours
basis_stop_loss:    1.5% divergence
entry_window:       T-2h before funding (00:00, 08:00, 16:00 UTC)
approval:           human (5min timeout)

# Cross-Exchange Arb
max_position:       $10
daily_loss_limit:   $5
min_net_spread:     1.0%
anomaly_filter:     >5% rejected
```

## Tests

```bash
python -m pytest tests/ -v
# 147 tests passing
```

## Disclaimer

This software is for educational and research purposes. Cryptocurrency trading involves significant risk, including the risk of total loss. Use at your own risk. Never trade with money you cannot afford to lose.

## License

MIT
