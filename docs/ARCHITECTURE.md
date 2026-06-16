# InfiniteLoop — System Architecture & Rollout Plan
### The definitive technical reference for Claude Code across all phases

Read this document at the start of any phase. It explains how the three services fit together, what each one owns, how they communicate, and what the full rollout sequence looks like.

---

## 1. Project Philosophy

InfiniteLoop is three **completely independent services** that happen to share a database. They do not call each other. They do not share Python code. They do not share processes. The only thing that connects them is **PostgreSQL**.

This is intentional. It means:
- Layer 1 (Strategy Lab) can be completely offline and Layer 2 (Execution Agent) keeps trading
- You can redeploy Layer 2 without touching Layer 3
- You can run Layer 1 locally on a laptop while Layers 2 and 3 run on Railway in the cloud
- A bug in one layer cannot crash another layer

When building any layer, **never import code from another layer**. If you find yourself wanting to share a utility function, put it in the database or duplicate it — duplication is intentional here.

---

## 2. Repository Structure

```
infiniteLoopProject/                   ← git root
│
├── CLAUDE.md                          ← Coding conventions, risk rules, stack
├── PHASE1_BUILD_PLAN.md               ← Step-by-step build prompts for Phase 1
├── docs/
│   └── ARCHITECTURE.md               ← THIS FILE
│
├── strategy-lab/                      ← SERVICE 1 — runs LOCAL (not deployed)
│   ├── requirements.txt
│   ├── constants.py
│   ├── loop.py                        ← Entry point: python loop.py
│   ├── dashboard.py                   ← Rich terminal + HTML results dashboard
│   ├── data/
│   │   ├── market_data.py            ← yfinance + CBOE VIX + FRED fetcher (all free)
│   │   ├── loader.py                 ← Assemble per-day direction feature rows
│   │   ├── indicators.py             ← ORB, gap, delta proxy, VWAP (from SPY bars)
│   │   ├── options.py                ← Synthetic spread P&L via py_vollib + VIX
│   │   ├── events.py                 ← Economic event calendar (FOMC/CPI/NFP/PCE skip)
│   │   ├── vol_regime.py             ← VIX regime gate (skip extreme vol days)
│   │   ├── store.py                  ← SQLite cache
│   │   └── raw/                      ← Cached data files (gitignored)
│   │       ├── vix/                  ← VIX_History.csv from CBOE (free, manual download)
│   │       └── cache.db              ← SQLite cache for yfinance data
│   ├── backtest/
│   │   ├── engine.py                 ← Direction model backtest (VectorBT)
│   │   ├── spread_engine.py          ← Spread P&L backtest (options chain data)
│   │   ├── metrics.py                ← Scorecard: direction accuracy, Sharpe, PF, DD
│   │   └── validator.py              ← OOS + walk-forward validation
│   ├── hermes/
│   │   ├── client.py                 ← Ollama HTTP client
│   │   ├── prompts.py                ← Prompt templates (direction + spread optimization)
│   │   └── parser.py                 ← Single-variable enforcement
│   ├── strategy/
│   │   ├── base.py                   ← BaseStrategy abstract class
│   │   ├── registry.py               ← PostgreSQL strategy registry
│   │   ├── packager.py               ← Serialize/deserialize strategies
│   │   └── orb_direction.py          ← Seed strategy (Phase 1)
│   ├── tests/
│   └── logs/
│
├── execution-agent/                   ← SERVICE 2 — deployed on Railway
│   ├── requirements.txt
│   ├── Dockerfile
│   ├── railway.toml
│   ├── main.py
│   ├── constants.py                   ← Independent copy
│   ├── data/
│   │   ├── feed.py                   ← Webull MQTT subscriber (ES ticks)
│   │   └── normalizer.py             ← Ticks → 1-min OHLCV bars
│   ├── strategy/
│   │   └── loader.py                 ← Pull active strategy from DB
│   ├── signals/
│   │   └── classifier.py             ← Compute direction after 30–45 min RTH
│   ├── spreads/
│   │   ├── selector.py               ← Choose strikes and spread type
│   │   └── pricer.py                 ← Real-time spread bid/ask from Webull
│   ├── risk/
│   │   ├── manager.py                ← Position sizing (max loss ≤ 10% equity per defined-risk spread)
│   │   └── limits.py                 ← Hard-coded limits (not config)
│   ├── orders/
│   │   ├── manager.py                ← Webull HTTP multi-leg options orders
│   │   └── state.py                  ← Track open spreads
│   ├── logging/
│   │   └── trade_logger.py           ← Write fills to PostgreSQL
│   └── health/
│       └── watchdog.py               ← Dead man's switch + 3:45 PM forced exit
│
└── portfolio-manager/                 ← SERVICE 3 — deployed on Railway
    ├── requirements.txt
    ├── Dockerfile
    ├── railway.toml
    ├── main.py
    ├── constants.py                   ← Independent copy
    ├── tracking/
    │   ├── equity.py
    │   └── performance.py
    ├── scaling/
    │   └── engine.py
    ├── rotation/
    │   ├── decay_detector.py
    │   └── rotator.py
    ├── expansion/
    │   └── gate_checker.py
    └── notifications/
        └── alerts.py
```

**Key rule:** Each service has its own `constants.py`. Never import between services. If a constant is needed by two services, define it independently in both.

---

## 3. System Topology

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         YOUR LOCAL MACHINE                              │
│                                                                         │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │                   SERVICE 1 — Strategy Lab                        │  │
│  │                                                                   │  │
│  │  yfinance → SPX daily OHLCV + SPY hourly bars (free, no key)     │  │
│  │  CBOE VIX CSV → daily VIX (free download, IV proxy for B-S)     │  │
│  │  FRED → risk-free rate (free, no key)                            │  │
│  │       ↓                                                           │  │
│  │  loader.py + indicators.py → Morning features per day            │  │
│  │  events.py → Skip FOMC / CPI / NFP / PCE days (hard rule)       │  │
│  │  vol_regime.py → Skip if VIX > 32 or IV rank low                │  │
│  │       ↓ (only favorable-regime, non-event days proceed)          │  │
│  │  TIERED BACKTEST: Tier 1 (6mo) → Tier 2 (2yr) → Tier 3 (4yr)   │  │
│  │  Spread P&L: py_vollib B-S pricing using VIX as IV proxy        │  │
│  │  dashboard.py → rich terminal scorecard after each tier          │  │
│  │       ↓                                                           │  │
│  │  Hermes 3 (via Ollama :11434) → one param change at a time       │  │
│  │       ↓                                                           │  │
│  │  Best strategy → OOS + Walk-Forward Validation                   │  │
│  │       ↓                                                           │  │
│  │  Validated strategy → PostgreSQL strategies table ───────────────┼──┼──┐
│  └───────────────────────────────────────────────────────────────────┘  │  │
│                                                                         │  │
│  ┌─────────────────────┐                                                │  │
│  │  Ollama (local AI)  │  ← hermes3 model, ~4-5 GB                     │  │
│  └─────────────────────┘                                                │  │
└─────────────────────────────────────────────────────────────────────────┘  │
                                                                             │
┌────────────────────────────────────────────────────────────────────────────┼──┐
│                              RAILWAY CLOUD                                 │  │
│                                                                            │  │
│  ┌──────────────────────────────────────────────────────────────────────┐ │  │
│  │                 PostgreSQL (shared database)                          │ │  │
│  │  tables: strategies | trades | equity_snapshots | portfolio_events   │◄┼──┘
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                                                            │
│  ┌───────────────────────────────────────────┐                            │
│  │         SERVICE 2 — Execution Agent        │                            │
│  │                           (always running) │                            │
│  │                                            │                            │
│  │  Webull MQTT (ES ticks) → normalizer       │                            │
│  │       ↓                                    │                            │
│  │  Morning features → classifier             │                            │
│  │  (after 30–45 min RTH)                     │                            │
│  │       ↓                                    │                            │
│  │  Direction: UP / DOWN / NEUTRAL            │                            │
│  │       ↓                                    │                            │
│  │  spread selector → pricer                  │                            │
│  │       ↓                                    │                            │
│  │  Risk manager (max loss ≤ 10% equity)      │                            │
│  │       ↓                                    │                            │
│  │  Order manager → Webull HTTP API           │                            │
│  │  (multi-leg SPX options order)             │                            │
│  │       ↓                                    │                            │
│  │  Monitor P&L → close at target/stop        │                            │
│  │  Watchdog: forced exit at 3:45 PM ET       │                            │
│  │       ↓                                    │                            │
│  │  Trade logger → PostgreSQL trades          │                            │
│  └───────────────────────────────────────────┘                            │
│                                                                            │
│  ┌───────────────────────────────────────────┐                            │
│  │       SERVICE 3 — Portfolio Manager        │                            │
│  │                        (scheduled cadence) │                            │
│  │  Equity Tracker → snapshots               │                            │
│  │  Performance Monitor → rolling metrics    │                            │
│  │  Decay Detector → trigger re-discovery    │                            │
│  │  Spread/Contract Scaler → governs size    │                            │
│  │  Expansion Gate → new instruments         │                            │
│  └───────────────────────────────────────────┘                            │
└────────────────────────────────────────────────────────────────────────────┘

                    ┌─────────────────────┐
                    │   Webull Platform   │
                    │  MQTT: ES tick data │
                    │  HTTP: options orders│
                    │  gRPC: fill events  │
                    └─────────────────────┘

                    ┌─────────────────────┐
                    │  Free Data Sources  │
                    │  yfinance (SPX/SPY) │
                    │  CBOE VIX CSV       │
                    │  FRED (risk-free)   │
                    └─────────────────────┘
```

---

## 4. Database Schema (The Inter-Layer Contract)

PostgreSQL is the **only** communication channel between the three services.

```sql
-- ─────────────────────────────────────────────────────────────
-- Written by: Layer 1 (strategy-lab)
-- Read by:    Layer 2, Layer 3
-- ─────────────────────────────────────────────────────────────
CREATE TABLE strategies (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    description     TEXT,
    params          JSONB NOT NULL,
    -- params includes both direction params and spread params, e.g.:
    -- {
    --   "gap_threshold_pct": 0.3,
    --   "orb_breakout_pct": 0.15,
    --   "delta_bias_threshold": 200,
    --   "neutral_band_pct": 0.2,
    --   "entry_hour": 10,
    --   "short_delta": 20,
    --   "spread_width_usd": 5,
    --   "profit_target_pct": 50,
    --   "stop_loss_pct": 200,
    --   "forced_exit_hour": 15
    -- }
    scorecard       JSONB NOT NULL,
    -- scorecard includes: direction_accuracy, sharpe, profit_factor, win_rate,
    --                     max_drawdown_pct, expectancy_dollars, total_trades
    status          TEXT NOT NULL DEFAULT 'candidate',
                    -- candidate | active | retired
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    validated_at    TIMESTAMPTZ,
    UNIQUE(name, version)
);

-- ─────────────────────────────────────────────────────────────
-- Written by: Layer 2, after confirmed fill
-- Read by:    Layer 3
-- ─────────────────────────────────────────────────────────────
CREATE TABLE trades (
    id                  SERIAL PRIMARY KEY,
    strategy_id         INTEGER REFERENCES strategies(id),
    symbol              TEXT NOT NULL,           -- 'SPX' or 'SPY'
    trade_type          TEXT NOT NULL,           -- 'bull_put_spread' | 'bear_call_spread' | 'iron_condor' | 'skipped'
    direction_signal    TEXT NOT NULL,           -- 'UP' | 'DOWN' | 'NEUTRAL'
    direction_correct   BOOLEAN,                 -- set at close time
    short_strike        REAL,
    long_strike         REAL,
    call_short_strike   REAL,                    -- iron condor call side
    call_long_strike    REAL,
    expiry_date         DATE NOT NULL,
    credit_received     REAL,                    -- total credit per spread (dollars)
    spread_width        REAL,                    -- width in dollars
    contracts           INTEGER NOT NULL DEFAULT 1,
    entry_time          TIMESTAMPTZ,
    exit_time           TIMESTAMPTZ,
    entry_spread_price  REAL,                    -- credit collected at entry
    exit_spread_price   REAL,                    -- cost to close at exit
    pnl_dollars         REAL,                    -- (credit - exit_cost) × 100 × contracts
    exit_reason         TEXT,                    -- 'profit_target' | 'stop_loss' | 'forced_exit' | 'skipped'
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────
-- Written by: Layer 3 (daily)
-- Read by:    Layer 3
-- ─────────────────────────────────────────────────────────────
CREATE TABLE equity_snapshots (
    id              SERIAL PRIMARY KEY,
    snapshot_date   DATE NOT NULL UNIQUE,
    equity          REAL NOT NULL,
    daily_pnl       REAL,
    trades_today    INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────
-- Written by: Layer 3
-- Read by:    Layer 3, Layer 1 (re-discovery trigger)
-- ─────────────────────────────────────────────────────────────
CREATE TABLE portfolio_events (
    id              SERIAL PRIMARY KEY,
    event_type      TEXT NOT NULL,
    -- event types: 'strategy_promoted' | 'strategy_retired' | 'spread_width_scaled'
    --              | 'contracts_scaled' | 'decay_detected' | 'rediscovery_triggered'
    --              | 'expansion_gate_checked' | 'daily_halt' | 'system_alert'
    description     TEXT,
    metadata        JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
```

**Rules for all database access:**
- Always use parameterized queries — never string-interpolate into SQL
- Layer 2 writes trades **only after a confirmed fill** — never on signal generation
- Use transactions for any multi-table write
- Layer 1 uses `psycopg2` (sync). Layers 2 and 3 use `asyncpg` (async).

---

## 5. Historical Data Sources (Layer 1 Only)

Layer 1 is the only service that fetches historical data. All sources are **free — no paid API required**. Data is cached locally in SQLite to avoid redundant fetches on each backtest run.

### Data Inventory

| Data | Source | Cost | Module | Notes |
|------|--------|------|--------|-------|
| SPX daily OHLCV | `yfinance` (`^GSPC`) | Free | `market_data.py` | 20+ years of daily history, no key |
| SPY hourly bars | `yfinance` (`SPY`, interval=1h) | Free | `market_data.py` | 2 years — ORB, VWAP, delta proxy |
| SPY 1-min bars | `yfinance` (`SPY`, interval=1m) | Free | `market_data.py` | Last 60 days — recent live checks |
| VIX daily history | CBOE free CSV | Free | `vol_regime.py` | One-time manual download; used as IV proxy |
| Risk-free rate | FRED via `pandas-datareader` | Free | `options.py` | 3-month T-bill (DTB3); no key needed |
| Spread P&L | `py_vollib` Black-Scholes | Free (local) | `options.py` | Synthetic pricing — VIX as sigma input |

**One manual step:** Download VIX history from `https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv` and save as `data/raw/vix/VIX_History.csv`. Everything else is fetched automatically by `market_data.py` and cached to SQLite on first run.

### How Synthetic Spread P&L Works

Historical options chain data (real bid/ask at every strike on past dates) requires a paid data provider. Instead, spread P&L is modeled using Black-Scholes with free inputs:

```python
# py_vollib Black-Scholes spread pricing
# For a bull put spread on an UP day:

S = spx_price_at_entry        # from yfinance daily open (approximated)
K_short = strike_at_target_delta(S, target_delta=15, sigma, t, r)
K_long  = K_short - spread_width   # e.g., 5 points lower

short_put = black_scholes('p', S, K_short, t, r, sigma)
long_put  = black_scholes('p', S, K_long,  t, r, sigma)
credit    = short_put - long_put

# sigma = VIX / 100 adjusted for 0DTE time
# t = remaining hours / 8760 (fraction of year)
# r = FRED 3-month T-bill rate
```

**Why this is a reasonable approximation:**
- VIX is literally the market's 30-day implied vol for SPX — it IS the IV input
- For 0DTE, IV is typically 1.0–1.3× the VIX-implied daily vol; a conservative scaling factor handles this
- Spread P&L at defined-risk strikes is dominated by delta and theta, both of which B-S captures well
- The direction accuracy metric (primary edge) doesn't use options pricing at all

**Known limitation:** Real bid/ask spreads, slippage, and intraday IV moves are not captured. Paper trading in Phase 2 measures the gap between synthetic and real fill prices.

### yfinance Data Limits

| Interval | Lookback | Used for |
|----------|----------|---------|
| 1-day | 20+ years | Gap %, day outcome labels, VIX alignment |
| 1-hour | 730 days (2 years) | ORB, VWAP, delta proxy — Tier 1 & 2 backtests |
| 1-min | 60 days | Recent live validation only |

The tiered backtest system is designed around these limits: Tier 1 (6 months) and Tier 2 (2 years) both fit within the 2-year hourly window.

---

## 6. Webull API Integration (Layer 2 Only)

Layer 2 is the only service that talks to Webull. It uses three Webull protocols:

| Protocol | Purpose | Library |
|---|---|---|
| **MQTT** | Subscribe to live ES tick data for direction classification | `paho-mqtt` or Webull SDK |
| **HTTP** | Place, modify, cancel multi-leg options orders | `requests` |
| **gRPC** | Receive order fill events | Webull-provided gRPC stubs |

### MQTT (ES Tick Data for Direction)
- Subscribe to the ES/MES tick feed (same as original futures design)
- Normalize to 1-min bars
- After 30–45 min of RTH, compute features and classify direction
- This is the only use of live market data — we are NOT streaming SPX options ticks

### HTTP (Options Order Placement)
- Multi-leg order for vertical spread: 2 legs (sell short strike, buy long strike)
- Multi-leg order for iron condor: 4 legs
- Always use `WEBULL_TRADING_MODE` env var to route to paper vs. live endpoint
- The mode check is in `orders/manager.py` — never rely on config files for this
- Never place a live order without confirming `WEBULL_TRADING_MODE == 'live'`

### gRPC (Fill Events)
- Receive fill confirmations asynchronously
- Only confirmed fills get logged to the `trades` table

### SPX vs. SPY Fallback
Attempt SPX first. If Webull's options API does not support SPX (Mini-SPX), fall back to SPY:
- SPY tracks SPX at 1/10th the price — similar economics to SPX
- SPY options are American-style (assignment risk), SPX is European-style (no assignment)
- This fallback decision is made during Phase 2 setup, not at runtime

---

## 7. Deployment Architecture

### Layer 1 — Local Only
- Runs on development machine, not deployed
- Needs: Python 3.11+, Ollama, PostgreSQL access (Railway remote URL), internet access for yfinance/FRED
- Run: `python strategy-lab/loop.py`
- SQLite cache: `strategy-lab/data/cache.db`

### Layer 2 — Railway (Execution Agent)
- Persistent service (restarts automatically)
- Dockerfile in `execution-agent/Dockerfile`
- On startup: reconnect to MQTT, reload active strategy from DB
- Environment variables set in Railway dashboard

### Layer 3 — Railway (Portfolio Manager)
- Cron-style service, runs daily at market close (~16:30 ET)
- Single pass: collect metrics → run checks → write events → send alerts → exit

### railway.toml (one per service)
```toml
[build]
builder = "dockerfile"
dockerfilePath = "Dockerfile"

[deploy]
startCommand = "python main.py"
restartPolicyType = "always"
```

### Dockerfile pattern
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "main.py"]
```

---

## 8. Risk Rules — Implementation Guide

| Rule | Where enforced | How |
|------|---------------|-----|
| Per-trade max loss (10%) | `risk/manager.py` | `(spread_width - credit) × 100 × contracts / equity ≤ 0.10` — SPX contracts are large; 1 contract of a $5-wide spread is ~7% of $5k, which is acceptable for defined-risk. Reduce to skip if max loss > 10%. |
| 5% daily loss halt | `risk/limits.py` + `health/watchdog.py` | Check `sum(trades.pnl_dollars today) / equity ≤ -0.05` before every new classification |
| Defined risk only | `orders/manager.py` | Only multi-leg spread orders allowed — single-leg orders raise an exception |
| No live without paper | `orders/manager.py` | Check `WEBULL_TRADING_MODE == 'live'` env var — if not, never route to live endpoint |
| Forced exit 3:45 PM ET | `health/watchdog.py` | Time-based check every minute — if time ≥ 15:45 ET and open position exists: close immediately |
| Dead man's switch | `health/watchdog.py` | On MQTT disconnect, exception in order flow, or daily limit hit: close all positions, halt, log CRITICAL |
| OOS validation mandatory | `strategy/loader.py` | Only load strategies with `status='active'` — 'candidate' strategies never execute |
| 200+ trade minimum | `backtest/metrics.py` (Layer 1) | Checked during scoring — strategy never reaches 'candidate' without it |

---

## 9. Layer-to-Layer Data Flows

### Flow 1 — Phase 1 → Phase 2 (Strategy handoff)
```
Layer 1 writes:
  INSERT INTO strategies (name, version, params, scorecard, status)
  VALUES ('orb_direction_v1', 1,
    '{"gap_threshold_pct": 0.25, "orb_breakout_pct": 0.12,
      "entry_hour": 10, "short_delta": 20, "spread_width_usd": 5,
      "profit_target_pct": 50, "stop_loss_pct": 200, "forced_exit_hour": 15}',
    '{"direction_accuracy": 0.61, "sharpe": 1.2, "profit_factor": 1.8, ...}',
    'active')

Layer 2 reads on startup:
  SELECT * FROM strategies WHERE status = 'active' ORDER BY validated_at DESC LIMIT 1

Layer 2 reconstructs the strategy and runs it live.
```

### Flow 2 — Phase 2 → Phase 3 (Trade logging)
```
Layer 2 writes after confirmed fill:
  INSERT INTO trades
    (strategy_id, symbol, trade_type, direction_signal, short_strike, long_strike,
     expiry_date, credit_received, spread_width, contracts,
     entry_time, entry_spread_price)
  VALUES
    (1, 'SPX', 'bull_put_spread', 'UP', 5350.0, 5345.0,
     '2026-06-06', 1.85, 5.0, 1,
     '2026-06-06 10:15:00-04', 1.85)

Layer 2 updates on close:
  UPDATE trades SET exit_time=..., exit_spread_price=..., pnl_dollars=...,
    exit_reason='profit_target', direction_correct=true
  WHERE id = ...
```

### Flow 3 — Phase 3 → Phase 4 (Performance monitoring)
```
Layer 3 reads daily:
  SELECT pnl_dollars, direction_signal, direction_correct, trade_type
  FROM trades WHERE strategy_id = 1 ORDER BY entry_time DESC LIMIT 30

Layer 3 writes:
  INSERT INTO equity_snapshots (snapshot_date, equity, daily_pnl, trades_today)
  VALUES ('2026-06-06', 512.50, 12.50, 1)
```

### Flow 4 — Phase 4 → Phase 1 (Re-discovery trigger)
```
Layer 3 writes when decay detected:
  INSERT INTO portfolio_events (event_type, description, metadata)
  VALUES ('rediscovery_triggered',
          'Direction accuracy fell to 47% (backtest: 61%) over last 30 days',
          '{"live_accuracy": 0.47, "backtest_accuracy": 0.61}')

Layer 1 loop.py polls on startup:
  SELECT * FROM portfolio_events
  WHERE event_type = 'rediscovery_triggered'
  AND created_at > NOW() - INTERVAL '7 days'
  ORDER BY created_at DESC LIMIT 1

If found → begin a new Hermes discovery loop.
```

---

## 10. Environment Variables Reference

```bash
# ── Shared by all services ────────────────────────────────────
DATABASE_URL=postgresql://user:pass@host:5432/dbname

# ── Layer 1 (strategy-lab) only ──────────────────────────────
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=hermes3
# No paid API key required — all historical data uses free sources (yfinance, CBOE, FRED)

# ── Layer 2 (execution-agent) only ───────────────────────────
WEBULL_APP_KEY=
WEBULL_APP_SECRET=
WEBULL_TRADE_TOKEN=
WEBULL_ACCOUNT_ID=
WEBULL_TRADING_MODE=paper                # MUST be 'paper' until Phase 3 approved

# Risk defaults (hard limits are in code — these are advisory)
MAX_DAILY_LOSS_PCT=0.05
MAX_RISK_PER_TRADE_PCT=0.10

# ── Layer 3 (portfolio-manager) only ─────────────────────────
ALERT_EMAIL=kirkpalmer67@gmail.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
```

---

## 11. Key Design Decisions

**1. Monorepo, independent services.**
All three services in one git repo, zero shared Python code. Communication through PostgreSQL only.

**2. PostgreSQL as the inter-layer bus.**
No message queues, no REST APIs between layers. Right tradeoff for a small account.

**3. VectorBT for backtesting.**
Direction model: fast signal-based backtest. Spread P&L: synthetic Black-Scholes pricing via py_vollib with VIX as the IV proxy. All data is free — yfinance (SPX/SPY), CBOE VIX CSV, FRED risk-free rate.

**4. Single parameter change per Hermes iteration.**
Scientific method — cannot know what caused improvement if multiple things change at once. Parser enforces this, not just the prompt.

**5. Hermes never touches risk parameters.**
`forced_exit_hour`, `max_loss_pct`, `daily_halt_pct` are owned by the risk manager. Hermes may only change direction thresholds and spread structure parameters.

**6. SPX as primary instrument.**
Mini-SPX (1/10th notional), European-style, cash-settled, 0DTE available every day. No assignment risk. SPY is the fallback if Webull's API doesn't support SPX options.

**7. Direction-first architecture.**
The ES futures MQTT feed (already live) drives the direction classifier. The options trade is the output of that classification. We are not streaming options ticks — we are classifying futures orderflow and expressing the view through options.

**8. Webull as broker.**
PDT rule eliminated June 4, 2026. $500 account trades freely. Revisit when portfolio exceeds $10k.

**9. Railway for hosting.**
Simple, Docker-based. No Kubernetes. Revisit at $10k+ portfolio.

**10. No expiration holds.**
0DTE gamma risk near expiration is extreme. 3:45 PM forced exit is hard-coded in the watchdog, not configurable by strategy or env var.

---

## 12. Constants Reference (strategy-lab/constants.py)

```python
# SPX options constants
SPX_MULTIPLIER = 100          # $100 per point ($5-wide spread = $500 max loss per contract)
SPX_TICK_SIZE = 0.05          # minimum price increment for liquid SPX strikes
SPX_STARTING_EQUITY = 5000.0  # starting capital in dollars

# Backtesting
OOS_SPLIT = 0.80              # first 80% = training, last 20% = OOS
WALK_FORWARD_FOLDS = 5
MIN_TRADE_COUNT = 200         # minimum trades for strategy consideration

# Tiered Promotion System — fail fast, only run full history when promising
# Most Hermes iterations stay in Tier 1 (seconds per run). Only strong candidates earn Tier 3.
TIER1_MONTHS = 6              # Tier 1: 6-month quick screen
TIER2_YEARS = 2               # Tier 2: 2-year medium validation
TIER3_YEARS = 4               # Tier 3: 4-year full run + OOS (last 20%)
OOS_FRACTION = 0.20           # Last 20% of data = OOS — never touched during optimization

TIER1_MIN_ACCURACY = 0.50     # Tier 1 thresholds (fast filter)
TIER1_MIN_PF = 1.20

TIER2_MIN_ACCURACY = 0.55     # Tier 2 thresholds (medium validation)
TIER2_MIN_PF = 1.50
TIER2_MIN_SHARPE = 0.80
TIER2_MAX_DD = 0.25

TIER3_OOS_TOLERANCE = 0.20    # OOS accuracy must be within 20% of in-sample

# Direction model thresholds (initial defaults — Hermes optimizes these)
GAP_THRESHOLD_PCT = 0.25      # gap > 0.25% triggers directional bias
ORB_BREAKOUT_PCT = 0.12       # ORB breakout > 0.12% of price is significant
NEUTRAL_BAND_PCT = 0.20       # gap within ±0.20% defaults to NEUTRAL
ORB_MINUTES = 30              # first 30 min of RTH defines the opening range

# Volatility regime gate (initial defaults — Hermes optimizes these)
DEFAULT_MIN_IV_RANK_PCT = 25.0  # skip if IV rank < 25% (not enough premium)
DEFAULT_MAX_VIX = 32.0          # skip if VIX > 32 (realized vol too high)

# Spread defaults (initial defaults — Hermes optimizes these)
DEFAULT_SHORT_DELTA = 20        # sell the ~20-delta strike (~40-50 points OTM on SPX)
DEFAULT_SPREAD_WIDTH_USD = 5    # $5-wide spread
DEFAULT_PROFIT_TARGET_PCT = 65  # close at 65% of max credit (from Kirk's real trading)
DEFAULT_STOP_LOSS_PCT = 150     # close if loss = 1.5× credit received

# Risk rules (HARD LIMITS — never change, enforced in code not config)
MAX_RISK_PER_TRADE_PCT = 0.10   # 10% of equity max loss per defined-risk spread
MAX_DAILY_LOSS_PCT = 0.05       # 5% of equity daily halt
FORCED_EXIT_HOUR = 15           # 3 PM ET — strategy-level close signal
FORCED_EXIT_MINUTE = 45         # watchdog hard-closes at 3:45 PM ET — non-negotiable

# Full validation acceptance thresholds (Tier 3 + OOS)
MIN_DIRECTION_ACCURACY = 0.55   # direction must be right > 55% of the time
MIN_SHARPE = 0.8
MIN_PROFIT_FACTOR = 1.5
MAX_DRAWDOWN_PCT = 0.20

# Event calendar — days where the system skips trading regardless of signal
# Hard-coded in data/events.py. Updated once per year from:
#   FOMC: federalreserve.gov/monetarypolicy/fomccalendars.htm
#   CPI/NFP: bls.gov/schedule
#   PCE: bea.gov/news/schedule
```

---

*Last updated: June 2026 | InfiniteLoop Trading System — 0DTE SPX Options | Starting capital: $5,000*
