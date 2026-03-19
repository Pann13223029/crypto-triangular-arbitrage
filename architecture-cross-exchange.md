# Cross-Exchange Arbitrage — Architecture Document

> Consensus from expert panel reviews (9-expert design panel + 8-expert pre-launch review).

> **Status: BUILT, DEPRIORITIZED.** System verified with real market data but discovered fundamental inventory risk: tokens drop 10-42% while holding, wiping arb profit. Spreads also rotate daily (BARD→SAHARA→gone). Strategy pivoted to **funding rate arbitrage** (delta-neutral, no inventory risk). Cross-exchange infrastructure remains functional for future use.

---

## 0. Current Implementation Status

```mermaid
graph LR
    subgraph Done["Implemented & Verified"]
        S["Scanner<br/>4 exchanges"]
        E["Executor<br/>simultaneous orders"]
        R["Risk Manager<br/>kill switch + limits"]
        B["Rebalancer<br/>threshold-based"]
        M["Metrics<br/>per-symbol P&L"]
    end

    subgraph Live["Live Verified"]
        KC["KuCoin<br/>REST + WS"]
        BN["Binance<br/>REST + WS + Trading"]
        OX["OKX<br/>WS price feed"]
    end

    subgraph Results["Real Market Results"]
        BARD["BARD: +4.72% best<br/>+1.23% avg<br/>20 opps/60s"]
    end

    Done --> Live --> Results

    style Done fill:#22c55e,stroke:#16a34a,color:#fff
    style Live fill:#3b82f6,stroke:#2563eb,color:#fff
    style Results fill:#f59e0b,stroke:#d97706,color:#fff
```

| Component | Status | Tests |
|-----------|--------|-------|
| CrossExchangeBook | Done | 8 |
| CrossExchangeScanner + pre-flight | Done | 4 |
| CrossExchangeExecutor + maker sell | Done | 5 |
| CrossExchangeRiskManager + imbalance | Done | 10 |
| RebalanceManager | Done | 11 |
| Binance (REST + WS + Live) | Done | — |
| KuCoin (REST + WS) | Done | — |
| OKX (REST + WS) | Done | — |
| Pipeline Metrics | Done | — |
| **Total** | **104 passing** | |

### Active Exchange Configuration

| Exchange | Role | API | WebSocket Stream |
|----------|------|-----|-----------------|
| **Binance** | Sell side | Full (signed) | bookTicker |
| **KuCoin** | Buy side | Full (signed) | orderbook.1 |
| **OKX** | Price feed only | Public | tickers |
| **Bybit** | Price feed only | Public | orderbook.1 |

### Verified Profitable Pairs

| Pair | Route | Avg Net Spread | Opportunities/min |
|------|-------|---------------|-------------------|
| BARDUSDT | KuCoin → Binance | +3.5% | ~20 |
| BARDUSDT | OKX → Binance | +2.5% | ~10 |
| SAHARAUSDT | OKX → Binance | +0.2% | ~3 |
| CFGUSDT | Binance → OKX | +0.07% | ~1 |

---

## 1. Overview

Cross-exchange arbitrage buys an asset cheap on one exchange and sells it higher on another. Unlike triangular arb (3 pairs, 1 exchange), this exploits **price fragmentation** across exchanges.

```mermaid
graph LR
    subgraph Binance
        B_ASK["ETH Ask: $3,450.20"]
    end
    subgraph Bybit
        BY_BID["ETH Bid: $3,453.80"]
    end

    B_ASK -->|"BUY @ $3,450.20"| PROFIT["Spread: $3.60<br/>(0.104%)"]
    BY_BID -->|"SELL @ $3,453.80"| PROFIT

    style Binance fill:#f59e0b,stroke:#d97706,color:#000
    style Bybit fill:#6366f1,stroke:#4f46e5,color:#fff
    style PROFIT fill:#22c55e,stroke:#16a34a,color:#fff
```

### Why Cross-Exchange?

| Property | Triangular (Current) | Cross-Exchange (New) |
|----------|---------------------|---------------------|
| Opportunity frequency | Rare (market very efficient) | 5-10x more frequent |
| Typical spread | 0.01-0.05% | 0.1-0.5% |
| Execution complexity | 3 legs, 1 exchange | 2 legs, 2 exchanges |
| Capital requirement | Single exchange | Split across exchanges |
| Main risk | Execution speed | Balance fragmentation, exchange counterparty |

---

## 2. System Architecture

### 2.1 High-Level Design

```mermaid
flowchart TB
    subgraph Feeds["📡 WebSocket Feeds"]
        BN_WS["Binance WS"]
        BY_WS["Bybit WS"]
        OKX_WS["OKX WS"]
    end

    subgraph Aggregation["📊 Aggregation Layer"]
        CXB["CrossExchangeBook<br/>(per symbol)"]
        BT["BalanceTracker<br/>(all exchanges)"]
    end

    subgraph Scanning["⚙️ Scanners"]
        TRI["Triangular Scanner<br/>(per exchange)"]
        CXS["CrossExchange Scanner<br/>(across exchanges)"]
    end

    subgraph Execution["🎯 Execution"]
        TRI_EX["Triangular Executor<br/>(sequential, existing)"]
        CX_EX["CrossExchange Executor<br/>(simultaneous limit orders)"]
    end

    subgraph Risk["🛡️ Risk & Rebalancing"]
        RM["CrossExchange<br/>RiskManager"]
        HM["Exchange Health<br/>Monitor"]
        REB["Rebalance<br/>Manager"]
    end

    subgraph Output["📊 Output"]
        DB["SQLite Logger<br/>(extended)"]
        DASH["Dashboard"]
    end

    BN_WS & BY_WS & OKX_WS --> CXB
    BN_WS & BY_WS & OKX_WS --> TRI
    CXB --> CXS
    TRI --> TRI_EX
    CXS --> CX_EX
    TRI_EX & CX_EX --> RM
    RM --> DB --> DASH
    HM --> RM
    REB --> BT

    style Feeds fill:#1e293b,stroke:#334155,color:#fff
    style Aggregation fill:#1e293b,stroke:#334155,color:#fff
    style Scanning fill:#1e293b,stroke:#334155,color:#fff
    style Execution fill:#1e293b,stroke:#334155,color:#fff
    style Risk fill:#1e293b,stroke:#334155,color:#fff
    style Output fill:#1e293b,stroke:#334155,color:#fff
```

### 2.2 Interaction Sequence

```mermaid
sequenceDiagram
    participant BN as Binance WS
    participant BY as Bybit WS
    participant CXB as CrossExchangeBook
    participant SC as CrossExchange Scanner
    participant RM as Risk Manager
    participant EX as CrossExchange Executor

    BN->>CXB: ETH ask $3,450.20
    BY->>CXB: ETH bid $3,453.80
    CXB->>CXB: Spread = 0.104%
    CXB->>SC: Opportunity detected

    SC->>SC: Net spread after fees = 0.02%
    SC->>RM: Submit opportunity

    RM->>RM: Check limits, health, exposure
    RM->>EX: Approved

    par Simultaneous execution
        EX->>BN: Limit BUY 1 ETH @ $3,450.50
        EX->>BY: Limit SELL 1 ETH @ $3,453.50
    end

    BN-->>EX: FILLED @ $3,450.30
    BY-->>EX: FILLED @ $3,453.60

    EX->>EX: Profit: $3.30 - fees
    Note over EX: Update balances on both exchanges
```

---

## 3. Balance Model: Pre-Funded

```mermaid
flowchart LR
    subgraph Capital["Total Capital: $30,000"]
        direction TB
        BN_BAL["Binance<br/>$10,000 USDT<br/>+ 3 ETH"]
        BY_BAL["Bybit<br/>$10,000 USDT<br/>+ 3 ETH"]
        OKX_BAL["OKX<br/>$10,000 USDT<br/>+ 3 ETH"]
    end

    subgraph Trading["Trading (no transfers)"]
        BUY["BUY ETH on Binance<br/>(spend USDT)"]
        SELL["SELL ETH on Bybit<br/>(receive USDT)"]
    end

    subgraph Drift["After 50 trades"]
        BN_D["Binance<br/>$7,200 USDT<br/>+ 4.5 ETH"]
        BY_D["Bybit<br/>$12,800 USDT<br/>+ 1.5 ETH"]
    end

    subgraph Rebal["Rebalance (periodic)"]
        XFER["Transfer $2,800 USDT<br/>Bybit → Binance<br/>(via TRC-20, ~3s)"]
    end

    Capital --> Trading --> Drift --> Rebal
    Rebal -->|"Restored"| Capital

    style Capital fill:#1e293b,stroke:#334155,color:#fff
    style Trading fill:#22c55e,stroke:#16a34a,color:#fff
    style Drift fill:#f59e0b,stroke:#d97706,color:#fff
    style Rebal fill:#3b82f6,stroke:#2563eb,color:#fff
```

> **Key decision:** Pre-funded balances on all exchanges. No real-time blockchain transfers during trades. Rebalance periodically via stablecoins on fast chains.

### Rebalancing Triggers

| Parameter | Value |
|-----------|-------|
| Deviation threshold | 25-30% from target allocation |
| Minimum rebalance amount | $500 |
| Cooldown between rebalances | 2 hours |
| Preferred chain | TRC-20 (USDT) or Solana (USDC) |
| Max concurrent rebalances | 1 |

---

## 4. Exchange Selection

> **Updated:** Original plan was Binance→Bybit→OKX. Due to geo-restrictions (OKX banned in Thailand, Bybit IP restricted), the active configuration is **Binance + KuCoin** for trading, with OKX/Bybit as price-feed-only sources.

```mermaid
graph LR
    P1["Active<br/>Binance + KuCoin<br/>(trading)"] --> P2["Price Feeds<br/>+ OKX + Bybit<br/>(read-only)"]

    style P1 fill:#22c55e,stroke:#16a34a,color:#fff
    style P2 fill:#3b82f6,stroke:#2563eb,color:#fff
```

| Exchange | API Quality | WS Stability | Taker Fee | Role | Status |
|----------|-------------|-------------|-----------|------|--------|
| **Binance** | 9/10 | 9/10 | 0.075% (BNB) | Sell side | Active (API key) |
| **KuCoin** | 7/10 | 7/10 | 0.10% | Buy side | Active (API key) |
| **OKX** | 8/10 | 8/10 | 0.10% | Price feed | Banned in Thailand |
| **Bybit** | 8/10 | 3/10 (spot) | 0.10% | Price feed | IP restricted |

### Fee Structure

| Exchange | Default Taker | Maker | Break-even (taker/taker) |
|----------|--------------|-------|--------------------------|
| Binance | 0.075% (BNB) | 0.075% | — |
| KuCoin | 0.100% | 0.100% | — |
| **Binance + KuCoin** | — | — | **0.175%** |
| **With maker sell** | — | — | **0.095%** |

### Why KuCoin Works Better Than Expected

KuCoin was originally rated "optional" but turned out to have the **widest spreads** on mid-cap pairs:
- BARD KuCoin→Binance: +4.72% net (vs OKX→Binance: +2.70%)
- 923 USDT pairs, all 9 target pairs available
- Higher spreads compensate for slightly lower API reliability

---

## 5. Exchange Abstraction

### 5.1 Extended Interface (Hybrid Approach)

```mermaid
classDiagram
    class ExchangeBase {
        <<abstract>>
        +exchange_id: str
        +fee_schedule: FeeSchedule
        +get_ticker(symbol) Ticker
        +get_order_book(symbol, depth) OrderBook
        +place_order(symbol, side, qty, price) Order
        +get_balance(asset) float
        +get_all_balances() dict
        +get_withdrawal_fee(asset, chain) float
        +withdraw(asset, amount, address, chain) str
        +get_deposit_address(asset, chain) str
        +close()
    }

    class BinanceExchange {
        -ws: BinanceWebSocket
        -rest: aiohttp
        +connect_ws(symbols)
    }

    class BybitExchange {
        -ws: BybitWebSocket
        -rest: aiohttp
        +connect_ws(symbols)
    }

    class OKXExchange {
        -ws: OKXWebSocket
        -rest: aiohttp
        +connect_ws(symbols)
    }

    class SimulatedExchange {
        -balances: dict
        -price_offsets: dict
        +inject_ticker(ticker)
    }

    class MultiExchangeSimulator {
        -exchanges: dict~str, SimulatedExchange~
        -spread_model: OrnsteinUhlenbeck
        +generate_divergent_prices()
    }

    ExchangeBase <|-- BinanceExchange
    ExchangeBase <|-- BybitExchange
    ExchangeBase <|-- OKXExchange
    ExchangeBase <|-- SimulatedExchange
    MultiExchangeSimulator *-- SimulatedExchange

    class FeeSchedule {
        +taker_fee: float
        +maker_fee: float
        +withdrawal_fees: dict
        +round_trip_cost(buy_role, sell_role) float
    }

    ExchangeBase ..> FeeSchedule
```

> **Key decision:** Direct implementations per exchange (no ccxt on hot path). Maximum performance, full control over WebSocket handling, exchange-specific optimizations.

---

## 6. Opportunity Detection

### 6.1 CrossExchangeBook

```mermaid
flowchart TD
    subgraph Updates["Price Updates (event-driven)"]
        BN_T["Binance: ETH ask $3,450.20"]
        BY_T["Bybit: ETH bid $3,453.80"]
        OKX_T["OKX: ETH bid $3,452.10"]
    end

    subgraph Book["CrossExchangeBook (ETH/USDT)"]
        AGG["Best buy: Binance $3,450.20<br/>Best sell: Bybit $3,453.80<br/>Spread: 0.104%"]
    end

    subgraph Filter["Filters"]
        STALE{"Staleness<br/>< 1 second?"}
        FEE{"Net spread<br/>> min threshold?"}
        SAME{"Different<br/>exchanges?"}
    end

    Updates --> Book --> STALE
    STALE -->|Yes| FEE
    STALE -->|No| DISCARD["Discard"]
    FEE -->|Yes| SAME
    FEE -->|No| DISCARD
    SAME -->|Yes| OPP["Opportunity!"]
    SAME -->|No| DISCARD

    style OPP fill:#22c55e,stroke:#16a34a,color:#fff
    style DISCARD fill:#6b7280,stroke:#4b5563,color:#fff
```

### 6.2 Dual Scanner Architecture

```mermaid
flowchart LR
    subgraph PerExchange["Per-Exchange (existing)"]
        BN_TRI["Binance<br/>Triangular Scanner"]
        BY_TRI["Bybit<br/>Triangular Scanner"]
    end

    subgraph Cross["Cross-Exchange (new)"]
        CX_SC["CrossExchange<br/>Scanner"]
    end

    BN_TRI --> Q["Opportunity Queue"]
    BY_TRI --> Q
    CX_SC --> Q

    Q --> EXEC["Executor<br/>(routes to correct type)"]

    style PerExchange fill:#1e293b,stroke:#334155,color:#fff
    style Cross fill:#1e293b,stroke:#334155,color:#fff
```

> Triangular arb *within* each exchange + cross-exchange arb *between* them. Multiple profit sources from the same infrastructure.

---

## 7. Execution Strategy

### 7.1 Simultaneous Limit Orders

```mermaid
flowchart TD
    START["Opportunity Approved"] --> SEND

    subgraph SEND["Simultaneous Order Placement"]
        direction LR
        BUY["Limit BUY on Binance<br/>@ ask + small buffer"]
        SELL["Limit SELL on Bybit<br/>@ bid - small buffer"]
    end

    SEND --> WAIT["await asyncio.gather()"]

    WAIT --> CHECK{"Both filled?"}
    CHECK -->|"Both filled"| SUCCESS["Log profit ✓"]
    CHECK -->|"One filled, one not"| PARTIAL["Partial fill handling"]
    CHECK -->|"Both unfilled"| CANCEL["Cancel both — no exposure"]
    CHECK -->|"One filled, one failed"| EMERGENCY["Emergency hedge"]

    PARTIAL --> HEDGE_P["Market order to close<br/>remaining exposure"]
    EMERGENCY --> HEDGE_E["Market sell on buy exchange<br/>(close unhedged position)"]

    HEDGE_E --> TRIP{"Emergency<br/>count > 3?"}
    TRIP -->|Yes| KILL["🛑 KILL SWITCH"]
    TRIP -->|No| LOG["Log and continue"]

    style SUCCESS fill:#22c55e,stroke:#16a34a,color:#fff
    style CANCEL fill:#6b7280,stroke:#4b5563,color:#fff
    style EMERGENCY fill:#ef4444,stroke:#dc2626,color:#fff
    style KILL fill:#ef4444,stroke:#dc2626,color:#fff
```

### 7.2 Execution State Machine

```mermaid
stateDiagram-v2
    [*] --> PENDING
    PENDING --> ORDERS_SENT: Send buy + sell
    ORDERS_SENT --> BOTH_FILLED: Both filled
    ORDERS_SENT --> BUY_ONLY: Buy filled, sell unfilled
    ORDERS_SENT --> SELL_ONLY: Sell filled, buy unfilled
    ORDERS_SENT --> NEITHER: Both unfilled/cancelled
    ORDERS_SENT --> PARTIAL: Partial fills

    BOTH_FILLED --> COMPLETED: Log profit
    NEITHER --> COMPLETED: No exposure

    BUY_ONLY --> HEDGING: Emergency market sell
    SELL_ONLY --> HEDGING: Emergency market buy
    PARTIAL --> HEDGING: Close net exposure

    HEDGING --> COMPLETED: Hedge filled
    HEDGING --> FAILED: Hedge failed

    COMPLETED --> [*]
    FAILED --> [*]
```

### 7.3 Failure Handling Matrix

| Buy Result | Sell Result | Action |
|-----------|-------------|--------|
| Filled | Filled | Log profit |
| Filled | Partial | Market-sell remainder on sell exchange |
| Filled | Failed | **Emergency:** market-sell on buy exchange |
| Partial | Filled | Market-buy remainder on buy exchange |
| Partial | Partial | Close net exposure on whichever exchange |
| Failed | Any | Cancel sell if pending. No exposure. |

---

## 8. Position Sizing

### 8.1 Book-Walking Algorithm

```mermaid
flowchart TD
    START["Target spread: 0.10%"] --> WALK["Walk both order books<br/>simultaneously"]
    WALK --> FILL["Fill at each level<br/>until spread narrows<br/>below target"]
    FILL --> SIZE["Optimal size from<br/>book depth"]
    SIZE --> CAP1{"Balance cap<br/>(available on both)"}
    CAP1 --> CAP2{"Risk cap<br/>(2% of total capital)"}
    CAP2 --> CAP3{"Spread confidence<br/>scaling"}
    CAP3 --> FINAL["Final position size"]

    style FINAL fill:#22c55e,stroke:#16a34a,color:#fff
```

### 8.2 Spread-Based Confidence Scaling

| Net Spread (after fees) | Position Size (% of max) |
|------------------------|--------------------------|
| 0.05 - 0.10% | 25% (marginal) |
| 0.10 - 0.20% | 50% |
| 0.20 - 0.50% | 75% |
| > 0.50% | 100% (but verify — may be stale prices) |

> Spreads > 0.5% on liquid pairs trigger an anomaly check before execution.

---

## 9. Risk Management

### 9.1 Enhanced Risk Decision Tree

```mermaid
flowchart TD
    OPP["Cross-Exchange<br/>Opportunity"] --> K{"Kill switch<br/>active?"}
    K -->|Yes| HALT["🛑 HALT"]
    K -->|No| DL{"Daily loss<br/>limit OK?"}

    DL -->|No| HALT
    DL -->|Yes| EH{"Exchange health<br/>(both exchanges)?"}

    EH -->|"Unhealthy"| ISOLATE["Isolate exchange<br/>from scanning"]
    EH -->|"Healthy"| UE{"Unhedged exposure<br/>< max?"}

    UE -->|No| WAIT["Wait for positions<br/>to close"]
    UE -->|Yes| CA{"Concurrent arbs<br/>< max (3)?"}

    CA -->|No| WAIT
    CA -->|Yes| STALE{"Prices fresh<br/>(< 1s)?"}

    STALE -->|No| SKIP["Skip — stale"]
    STALE -->|Yes| APPROVE["✅ Approve"]

    style HALT fill:#ef4444,stroke:#dc2626,color:#fff
    style APPROVE fill:#22c55e,stroke:#16a34a,color:#fff
    style ISOLATE fill:#f59e0b,stroke:#d97706,color:#fff
```

### 9.2 New Risk Dimensions

| Risk Type | Mitigation |
|-----------|-----------|
| **Execution risk** (2 exchanges, 2 networks) | State machine executor, emergency hedge |
| **Counterparty risk** (N exchanges) | Per-exchange cap (max 33% of capital), PoR monitoring |
| **Balance fragmentation** | Threshold-based rebalancing + opportunity-aware bias |
| **Stale price risk** (N feeds) | Max-age filter (1s), staleness check before execution |
| **Exchange degradation** | Exchange health monitor, automatic isolation |
| **Emergency hedge cascade** | 3 emergency hedges/hour → kill switch |

### 9.3 Exchange Health Monitor

```mermaid
stateDiagram-v2
    [*] --> Healthy
    Healthy --> Degraded: API errors > 5/min
    Healthy --> Degraded: WS silent > 10s
    Degraded --> Healthy: Errors clear, WS resumes
    Degraded --> Isolated: Errors persist > 2min
    Degraded --> Isolated: Withdrawal suspended
    Isolated --> Degraded: Manual review
    Isolated --> Healthy: All clear

    note right of Isolated: No arb trades involving\nthis exchange
```

---

## 10. Data & Logging (Extended)

### 10.1 New Database Tables

```mermaid
erDiagram
    SESSIONS ||--o{ CROSS_OPPORTUNITIES : contains
    CROSS_OPPORTUNITIES ||--o{ CROSS_TRADES : executes
    SESSIONS ||--o{ TRANSFERS : records
    SESSIONS ||--o{ EXCHANGE_HEALTH_LOG : tracks

    CROSS_OPPORTUNITIES {
        int id PK
        int session_id FK
        int timestamp_ms
        string symbol
        string buy_exchange
        string sell_exchange
        float buy_price
        float sell_price
        float gross_spread
        float net_spread
        float position_size
        bool executed
        string skip_reason
    }

    CROSS_TRADES {
        int id PK
        int opportunity_id FK
        string exchange_id
        string side
        float expected_price
        float actual_price
        float quantity
        float fee
        string status
        int timestamp_ms
    }

    TRANSFERS {
        int id PK
        int session_id FK
        string from_exchange
        string to_exchange
        string asset
        string chain
        float amount
        float fee
        string tx_hash
        string status
        int initiated_ms
        int confirmed_ms
    }

    EXCHANGE_HEALTH_LOG {
        int id PK
        string exchange_id
        string status
        string reason
        int timestamp_ms
    }
```

---

## 11. Project Structure (New Components)

```
crypto-triangular-arbitrage/
├── ... (existing modules unchanged)
│
├── exchange/
│   ├── base.py              # Extended with cross-exchange methods
│   ├── binance_exchange.py  # Binance direct implementation
│   ├── bybit_exchange.py    # NEW: Bybit V5 API
│   ├── okx_exchange.py      # NEW: OKX API (Phase 2)
│   ├── simulator.py         # Extended for multi-exchange sim
│   └── multi_sim.py         # NEW: Multi-exchange simulator
│
├── cross_exchange/          # NEW MODULE
│   ├── book.py              # CrossExchangeBook (aggregated per symbol)
│   ├── scanner.py           # CrossExchangeScanner
│   ├── executor.py          # State machine executor (simultaneous)
│   ├── models.py            # CrossExchangeOpportunity, etc.
│   └── balance_tracker.py   # Real-time balance tracking
│
├── rebalancing/             # NEW MODULE
│   ├── manager.py           # RebalanceManager (threshold + bias)
│   ├── transfer.py          # TransferManager (chain selection, monitoring)
│   └── models.py            # Transfer, RebalanceDecision
│
├── monitoring/              # NEW MODULE
│   ├── exchange_health.py   # ExchangeHealthMonitor
│   └── fee_manager.py       # Dynamic fee schedules
│
└── tests/
    ├── test_cross_book.py
    ├── test_cross_executor.py
    ├── test_rebalancing.py
    └── test_exchange_health.py
```

---

## 12. Development Roadmap

```mermaid
gantt
    title Cross-Exchange Development Roadmap
    dateFormat  YYYY-MM-DD
    axisFormat  %b %d

    section Phase 1 — Foundation
    Extend ExchangeBase interface       :p1a, 2026-03-19, 1d
    Implement BybitExchange (REST+WS)   :p1b, after p1a, 3d
    CrossExchangeBook                   :p1c, after p1a, 1d
    CrossExchangeScanner                :p1d, after p1c, 1d
    MultiExchangeSimulator              :p1e, after p1b, 2d
    BalanceTracker                      :p1f, after p1a, 1d
    Extend SQLite schema                :p1g, after p1a, 1d

    section Phase 2 — Execution & Risk
    CrossExchange Executor (state machine) :p2a, after p1e, 3d
    Extended RiskManager                   :p2b, after p2a, 2d
    ExchangeHealthMonitor                  :p2c, after p2b, 1d
    FeeManager                             :p2d, after p2b, 1d
    Paper trading full simulation          :p2e, after p2d, 3d

    section Phase 3 — Rebalancing & Production
    RebalanceManager                    :p3a, after p2e, 2d
    TransferManager                     :p3b, after p3a, 2d
    OKXExchange implementation          :p3c, after p2e, 2d
    Deploy to AWS Tokyo                 :p3d, after p3c, 1d
    Live testing ($100-500)             :p3e, after p3d, 5d

    section Phase 4 — Optimization
    Fee tier optimization               :p4a, after p3e, 2d
    orjson + latency tuning             :p4b, after p3e, 1d
    KuCoin (optional)                   :p4c, after p4a, 2d
    Advanced rebalancing                :p4d, after p4b, 2d
    Multi-exchange dashboard            :p4e, after p4d, 3d
```

---

## 13. Key Architecture Decisions Summary

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **Balance model** | Pre-funded on all exchanges | Instant execution, no blockchain delays |
| **API approach** | Direct implementations per exchange | Max performance, debuggability |
| **Opportunity detection** | Centralized aggregated book, event-driven | Minimum detection latency |
| **Execution** | Simultaneous limit orders | Minimizes timing risk; unfilled = no exposure |
| **Failure handling** | State machine + emergency hedge | Covers all failure scenarios |
| **Rebalancing** | Threshold (25-30%) + opportunity bias | Balances efficiency vs transfer costs |
| **Position sizing** | Book-walking + risk cap (2% capital) | Respects actual liquidity |
| **Fees** | Dynamic per-exchange, queried at startup | Fees vary by tier/token |
| **Risk management** | Extended existing + exchange isolation | Preserves working controls |
| **Exchanges** | Binance → Bybit → OKX → KuCoin | Best APIs, liquidity, PoR |
| **Deployment** | AWS Tokyo (ap-northeast-1) | Optimal latency to all targets |
| **Testing** | Multi-exchange simulator (O-U spreads) | Realistic testing before live |
| **JSON parsing** | orjson (3-10x faster) | Thousands of WS messages/sec |

---

## 14. Coexistence with Triangular Arb

```mermaid
flowchart TB
    subgraph WS["WebSocket Feeds"]
        BN["Binance"]
        BY["Bybit"]
    end

    subgraph Triangular["Triangular Arb (existing)"]
        BN_TRI["Binance Scanner<br/>192 triangles"]
        BY_TRI["Bybit Scanner<br/>~200 triangles"]
    end

    subgraph CrossEx["Cross-Exchange Arb (new)"]
        CX["Cross-Exchange Scanner<br/>~500 pairs × 2 exchanges"]
    end

    BN --> BN_TRI & CX
    BY --> BY_TRI & CX

    BN_TRI & BY_TRI --> EXEC1["Triangular Executor"]
    CX --> EXEC2["Cross-Exchange Executor"]

    EXEC1 & EXEC2 --> RM["Shared Risk Manager"]

    style Triangular fill:#0f172a,stroke:#334155,color:#fff
    style CrossEx fill:#0f172a,stroke:#334155,color:#fff
```

> Both strategies run concurrently, share risk limits, and multiply profit sources.

---

*Document generated from expert panel consensus — Dr. Elena Vasquez, Marcus Chen, Aisha Patel, Tomasz Kowalski, Dr. Yuki Tanaka, James Okafor, Sofia Reyes, Dr. Raj Mehta, Lena Hoffmann*
