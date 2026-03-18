# Crypto Triangular Arbitrage

A Python-based triangular arbitrage detection and execution system for Binance.

## What is Triangular Arbitrage?

Triangular arbitrage exploits price inefficiencies between three trading pairs on the same exchange. If the product of the three exchange rates yields a net gain after trading fees, all three trades are executed to capture the spread.

```mermaid
graph LR
    USDT -->|"1. Buy BTC"| BTC
    BTC -->|"2. Buy ETH"| ETH
    ETH -->|"3. Sell for USDT"| USDT

    style USDT fill:#22c55e,stroke:#16a34a,color:#fff
    style BTC fill:#f59e0b,stroke:#d97706,color:#fff
    style ETH fill:#6366f1,stroke:#4f46e5,color:#fff
```

> All trades happen on **one exchange** — no transfers, no withdrawal risk, no counterparty exposure.

## System Overview

```mermaid
flowchart LR
    subgraph Input["Data"]
        WS["Binance<br/>WebSocket"]
    end

    subgraph Engine["Engine"]
        PC["Price<br/>Cache"]
        SC["Triangle<br/>Scanner"]
        OS["Opportunity<br/>Scorer"]
    end

    subgraph Trade["Trading"]
        RM["Risk<br/>Manager"]
        EX["Executor"]
    end

    subgraph Monitor["Monitor"]
        DB["SQLite<br/>Logger"]
        DASH["CLI<br/>Dashboard"]
    end

    WS --> PC --> SC --> OS --> RM --> EX --> DB --> DASH

    style Input fill:#1e293b,stroke:#334155,color:#fff
    style Engine fill:#1e293b,stroke:#334155,color:#fff
    style Trade fill:#1e293b,stroke:#334155,color:#fff
    style Monitor fill:#1e293b,stroke:#334155,color:#fff
```

## Features

- **Real-time scanning** — WebSocket price feeds with selective triangle updates
- **Vectorized calculation** — numpy-powered profit detection across 2,000+ triangles
- **Simulation mode** — Paper trading with virtual balances and configurable slippage
- **Risk management** — Position limits, daily loss limits, circuit breakers, kill switch
- **Full audit trail** — Every opportunity logged (executed or skipped) in SQLite
- **CLI dashboard** — Real-time P&L, scan rate, and system health monitoring

## Quick Start

```bash
# Clone the repo
git clone https://github.com/Pann13223029/crypto-triangular-arbitrage.git
cd crypto-triangular-arbitrage

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure (simulation mode by default)
cp .env.example .env

# Run in simulation mode
python main.py --mode simulation
```

## Project Structure

```
├── main.py              # Entry point
├── config/              # Settings, environment config
├── core/                # Triangle discovery, scanner, calculator
├── exchange/            # Binance API (WebSocket + REST) & simulator
├── execution/           # Trade executor, risk manager, order tracking
├── data/                # SQLite logging, in-memory price cache
├── dashboard/           # CLI real-time monitor
├── backtest/            # Data recorder & historical replayer
└── tests/               # Unit & integration tests
```

## How It Works

```mermaid
sequenceDiagram
    participant B as Binance
    participant S as Scanner
    participant R as Risk Manager
    participant E as Executor

    B->>S: Price tick
    S->>S: Recalc affected triangles
    Note over S: Profit > 0.1%?

    S->>R: Opportunity found
    R->>R: Check limits & balances
    R->>E: Approved

    E->>B: Leg 1 — Buy BTC
    B-->>E: Filled
    E->>B: Leg 2 — Buy ETH
    B-->>E: Filled
    E->>B: Leg 3 — Sell ETH
    B-->>E: Filled

    Note over E: Profit captured!
```

## Development Phases

```mermaid
graph LR
    P1["Phase 1<br/>Foundation"] --> P2["Phase 2<br/>Simulation"]
    P2 --> P3["Phase 3<br/>Execution<br/>& Risk"]
    P3 --> P4["Phase 4<br/>Monitoring"]
    P4 --> P5["Phase 5<br/>Live Trading"]
    P5 --> P6["Phase 6<br/>Optimization"]

    style P1 fill:#22c55e,stroke:#16a34a,color:#fff
    style P2 fill:#3b82f6,stroke:#2563eb,color:#fff
    style P3 fill:#f59e0b,stroke:#d97706,color:#fff
    style P4 fill:#8b5cf6,stroke:#7c3aed,color:#fff
    style P5 fill:#ef4444,stroke:#dc2626,color:#fff
    style P6 fill:#6b7280,stroke:#4b5563,color:#fff
```

1. **Foundation** — Models, triangle discovery, profit calculator
2. **Simulation** — Paper trading with real price feeds
3. **Execution & Risk** — Circuit breakers, position management
4. **Monitoring** — CLI dashboard, opportunity logging
5. **Live Trading** — Testnet validation → gradual live rollout
6. **Optimization** — Backtesting, cross-exchange, performance tuning

## Architecture

See [architecture.md](architecture.md) for the full system design, produced from a 10-expert panel review covering:

- Core algorithm (triangle graph, selective updates, order book awareness)
- Execution strategy (sequential with circuit breaker)
- Risk management (kill switch, position limits, slippage guards)
- Exchange abstraction (simulation ↔ live swap via config)
- Database schema (opportunities, trades, sessions)
- Binance API strategy (WebSocket streams, rate limits, fees)

## Security

- API keys stored in `.env` (never committed)
- Binance keys should have **IP whitelist** and **no withdrawal permission**
- All secrets redacted from logs
- Full audit trail for compliance

## Disclaimer

This software is for educational and research purposes. Cryptocurrency trading involves significant risk. Use at your own risk.

## License

MIT
