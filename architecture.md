# Crypto Triangular Arbitrage — Architecture Document

> Consensus output from a 10-expert panel review (discuss → brainstorm → debate → discuss).

---

## 1. Overview

A **triangular arbitrage** system for Binance that detects and exploits pricing inefficiencies across three related trading pairs on a single exchange.

**Example triangle:**
```
USDT → BTC → ETH → USDT
```
If the product of three exchange rates yields a net gain after fees, the system executes all three trades sequentially to capture the spread.

### Why Triangular Arbitrage?

| Property | Advantage |
|----------|-----------|
| Single exchange | No withdrawal fees, no transfer delays |
| No counterparty risk | Funds never leave Binance |
| Atomic opportunity | All legs execute on same platform |
| Scalable scanning | 2,000+ triangles on Binance |

---

## 2. System Architecture

### 2.1 High-Level Flow

```
┌─────────────┐     ┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│  Binance     │────▶│  Price       │────▶│  Triangle    │────▶│  Opportunity│
│  WebSocket   │     │  Cache       │     │  Scanner     │     │  Scorer     │
└─────────────┘     └─────────────┘     └──────────────┘     └──────┬──────┘
                                                                     │
                                                                     ▼
┌─────────────┐     ┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│  Dashboard   │◀────│  Trade       │◀────│  Risk        │◀────│  Executor   │
│  (CLI)       │     │  Logger (DB) │     │  Manager     │     │  (Sim/Live) │
└─────────────┘     └─────────────┘     └──────────────┘     └─────────────┘
```

### 2.2 Component Descriptions

| Component | Responsibility |
|-----------|---------------|
| **Binance WebSocket** | Subscribe to individual ticker streams for pairs in our triangle set |
| **Price Cache** | In-memory price state (bid/ask + top-5 order book depth) |
| **Triangle Scanner** | Pre-computed graph; on each tick, recalculate only affected triangles |
| **Opportunity Scorer** | Vectorized profit calculation (numpy); filters by min threshold |
| **Executor** | Sequential 3-leg execution with circuit breaker |
| **Risk Manager** | Position limits, daily loss limit, cooldowns, slippage guard |
| **Trade Logger** | SQLite — every opportunity (executed or skipped), full audit trail |
| **Dashboard** | Real-time CLI monitor (rich library) — P&L, scan rate, WebSocket health |

---

## 3. Core Algorithm

### 3.1 Triangle Discovery

At startup, fetch all Binance trading pairs and build a **directed graph**:
- Nodes = assets (BTC, ETH, USDT, BNB, ...)
- Edges = trading pairs with direction (base→quote for sell, quote→base for buy)

Enumerate all valid 3-node cycles. Store as a list of `Triangle` objects with pre-resolved pair symbols.

### 3.2 Profit Calculation

For a triangle path `A → B → C → A`:

```python
# Forward direction
forward_profit = (1 / ask_AB) * bid_BC * bid_CA - 1

# Reverse direction
reverse_profit = (1 / ask_CA) * (1 / ask_CB) * bid_AB - 1

# Net after fees (3 trades × fee_rate)
net_profit = max(forward_profit, reverse_profit) - (3 * fee_rate)
```

**Both directions** are checked on every update, because the opportunity may only exist in one direction.

### 3.3 Selective Update Strategy

When a price update arrives for pair `X`:
1. Look up all triangles containing `X` (pre-computed mapping)
2. Recalculate profit only for those triangles
3. Skip the remaining ~99% of triangles

This reduces per-tick computation from O(all_triangles) to O(affected_triangles).

### 3.4 Order Book Awareness

Use top-5 order book levels to calculate **executable profit** accounting for:
- Available liquidity at each level
- Slippage for the intended trade size
- Whether the full position can be filled

---

## 4. Execution Strategy

### 4.1 Sequential with Circuit Breaker

```
Leg 1: Execute → Verify fill → Check slippage
    ↓ (pass)
Leg 2: Execute → Verify fill → Check slippage
    ↓ (pass)
Leg 3: Execute → Verify fill → Log result
```

**Circuit breaker triggers:**
- If any leg's fill price deviates > `slippage_tolerance` from expected
- If a leg fails to fill within timeout (configurable, default 2s)
- On trigger: **abort remaining legs** and **hedge** (market sell back to starting asset)

### 4.2 Order Types

- **Market orders** for execution speed (arb is time-sensitive)
- Limit orders considered for future versions (lower fees but fill risk)

---

## 5. Risk Management

### 5.1 Parameters

```yaml
min_profit_threshold:   0.1%     # Minimum net profit after fees to execute
max_position_size:      $500     # Maximum USD value per triangle execution
daily_loss_limit:       $50      # Kill switch — halt trading for the day
max_open_triangles:     1        # Only one triangle executing at a time (v1)
slippage_tolerance:     0.05%    # Abort if fill deviates beyond this
cooldown_after_loss:    60s      # Pause after a losing trade
max_consecutive_losses: 3        # Halt after 3 losses in a row
```

### 5.2 Kill Switch

The system halts all trading immediately when:
- Daily loss limit is breached
- WebSocket connection drops (stale prices = danger)
- API rate limit is hit (429 response)
- Any unhandled exception in the execution path

### 5.3 Balance Management

- Track balances for all assets in active triangles
- Pre-check sufficient balance before execution
- Reserve a safety margin (don't use 100% of available balance)

---

## 6. Exchange Abstraction

### 6.1 Interface Pattern

```python
class ExchangeBase(ABC):
    async def get_ticker(self, symbol: str) -> Ticker
    async def get_order_book(self, symbol: str, depth: int) -> OrderBook
    async def place_order(self, symbol: str, side: str, quantity: float) -> Order
    async def get_balance(self, asset: str) -> float
    async def get_all_pairs(self) -> list[TradingPair]
```

### 6.2 Implementations

| Class | Purpose |
|-------|---------|
| `SimulatedExchange` | Paper trading — virtual balances, configurable slippage, fee simulation |
| `LiveExchange` | Real Binance API calls via `ccxt` (data) + direct `aiohttp` (orders) |

Switching between modes is a **configuration change**, not a code change.

### 6.3 Simulated Exchange Features

- Virtual balance tracking per asset
- Configurable fee rates (match Binance tiers)
- Configurable slippage model (fixed, random, depth-based)
- Fill simulation using real order book snapshots
- Latency injection for realistic timing

---

## 7. Data & Logging

### 7.1 In-Memory State

- Current bid/ask prices per pair (`dict[str, Ticker]`)
- Top-5 order book per pair (`dict[str, OrderBook]`)
- Active balances per asset
- Triangle graph and pre-computed mappings

### 7.2 SQLite Database

**Tables:**

```sql
-- Every opportunity detected (executed or not)
opportunities (
    id, timestamp_ms, triangle_path, direction,
    theoretical_profit, executable_profit, book_depth,
    executed (bool), skip_reason
)

-- Every trade executed
trades (
    id, opportunity_id, leg_number, symbol, side,
    expected_price, actual_price, quantity, fee,
    slippage, status, timestamp_ms
)

-- Aggregated P&L per session
sessions (
    id, start_time, end_time, mode,
    total_opportunities, total_trades,
    gross_pnl, net_pnl, fees_paid
)
```

### 7.3 Logging Strategy

- **Structured JSON logs** for machine parsing
- Log every opportunity seen (even skipped) — critical for tuning
- **Never log API keys or secrets**
- Log levels: DEBUG (price ticks), INFO (opportunities), WARNING (slippage), ERROR (failures)

---

## 8. Binance API Strategy

### 8.1 WebSocket

- Subscribe to **individual pair streams** (`symbol@ticker`) for low latency
- Subscribe to **top-5 depth** (`symbol@depth5@100ms`) for order book
- Only subscribe to pairs that are part of valid triangles (~100-200 pairs)
- Implement automatic reconnection with exponential backoff
- **Health check**: if no message received in 5s, trigger reconnect

### 8.2 REST API

- Used for: placing orders, fetching balances, getting exchange info
- Rate limits: 10 orders/sec, 1200 request weight/min
- Track rate limit headers and throttle proactively

### 8.3 Fee Structure

| Tier | Maker | Taker | With BNB |
|------|-------|-------|----------|
| Regular | 0.10% | 0.10% | 0.075% |
| VIP 1 | 0.09% | 0.10% | 0.0675% |

**Break-even per triangle** = 3 × fee_rate (e.g., 3 × 0.075% = 0.225%)

---

## 9. Project Structure

```
crypto-triangular-arbitrage/
├── main.py                      # Entry point, arg parsing, mode selection
├── config/
│   ├── settings.py              # Dataclass-based config (fees, thresholds)
│   └── .env.example             # Template for API keys
├── core/
│   ├── models.py                # Data models (Opportunity, Trade, Triangle)
│   ├── triangle.py              # Triangle discovery & graph builder
│   ├── scanner.py               # Real-time opportunity detection (vectorized)
│   └── calculator.py            # Profit calculation with fees & slippage
├── exchange/
│   ├── base.py                  # Abstract exchange interface
│   ├── binance_ws.py            # WebSocket price/orderbook feeds
│   ├── binance_rest.py          # REST API (orders, balances)
│   └── simulator.py             # Simulated exchange for paper trading
├── execution/
│   ├── executor.py              # Trade executor with circuit breaker
│   ├── risk_manager.py          # Position limits, loss limits, cooldowns
│   └── order_manager.py         # Order tracking, partial fill handling
├── data/
│   ├── db.py                    # SQLite setup, trade/opportunity logging
│   └── price_cache.py           # In-memory price state
├── dashboard/
│   └── cli_monitor.py           # Real-time CLI dashboard (rich library)
├── backtest/
│   ├── data_recorder.py         # Record live data for replay
│   └── replayer.py              # Replay historical data through scanner
├── tests/
│   ├── test_triangle.py
│   ├── test_calculator.py
│   ├── test_executor.py
│   └── test_risk_manager.py
├── architecture.md              # This document
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

---

## 10. Technology Stack

| Component | Technology | Reason |
|-----------|-----------|--------|
| Language | Python 3.11+ | Async support, numpy, rich ecosystem |
| Async | asyncio + aiohttp | Non-blocking I/O for WebSocket + REST |
| Exchange API | ccxt (data) + direct aiohttp (orders) | Unified interface + low-latency execution |
| Math | numpy | Vectorized profit calculation across all triangles |
| Database | SQLite (aiosqlite) | Lightweight, zero-config, sufficient for single-exchange |
| Dashboard | rich | Beautiful CLI tables, live updating |
| Config | python-dotenv + dataclasses | Type-safe config, secure key management |
| Testing | pytest + pytest-asyncio | Async test support |
| Linting | ruff | Fast, comprehensive Python linter |

---

## 11. Development Phases

### Phase 1 — Foundation (Current)
- [ ] Project scaffolding and config
- [ ] Data models and triangle discovery
- [ ] Profit calculator with fee accounting
- [ ] Unit tests for core math

### Phase 2 — Simulation Engine
- [ ] Simulated exchange with virtual balances
- [ ] WebSocket price feed integration
- [ ] Scanner + scorer pipeline
- [ ] Paper trading loop

### Phase 3 — Execution & Risk
- [ ] Sequential executor with circuit breaker
- [ ] Risk manager (limits, kill switch, cooldowns)
- [ ] Order tracking and partial fill handling

### Phase 4 — Monitoring & Analysis
- [ ] CLI dashboard
- [ ] Opportunity logging (all seen, not just executed)
- [ ] P&L reporting and session summaries

### Phase 5 — Live Trading
- [ ] Binance testnet validation
- [ ] Live exchange implementation
- [ ] IP whitelist and security hardening
- [ ] Gradual rollout with small position sizes

### Phase 6 — Optimization (Future)
- [ ] Backtesting with recorded data
- [ ] Cross-exchange arbitrage expansion
- [ ] Advanced slippage models
- [ ] Performance profiling and latency reduction

---

## 12. Security Checklist

- [ ] API keys in `.env`, never committed to git
- [ ] `.gitignore` covers `.env`, `*.db`, `__pycache__`, logs
- [ ] Binance API key: **IP whitelisted**, **no withdrawal permission**
- [ ] Log redaction — no secrets in logs, even partially
- [ ] Rate limit tracking to avoid API bans
- [ ] Kill switch accessible via CLI signal (Ctrl+C graceful shutdown)

---

## 13. Compliance Notes

- Maintain full audit trail of all trades and decisions
- Implement reasonable throttling to avoid wash-trading detection flags
- Keep records suitable for tax reporting (all trades with timestamps, amounts, fees)
- Respect Binance Terms of Service — API trading is permitted

---

*Document generated from expert panel consensus — Dr. Wei Chen (Quant), Sarah Kovacs (Exchange), Raj Patel (Architect), Elena Rossi (Risk), Marcus Thompson (Python), Dr. Yuki Tanaka (Microstructure), Nina Okafor (DevOps), James Liu (Data), Anika Sharma (Security), Carlos Mendez (Compliance)*
