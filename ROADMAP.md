# InfiniteLoop — Project Roadmap

**Last updated:** June 2026
**Starting capital:** $5,000 (0DTE SPX options)
**Target:** Self-funding, compounding autonomous trading system

---

## Vision

Build a three-agent system that:
1. **Discovers** high-probability 0DTE options strategies using AI (Hermes 3)
2. **Executes** those strategies live via Webull API on Railway — selling premium daily on SPX
3. **Manages** a growing portfolio — scaling spread width and contracts, rotating strategies, expanding instruments

The system is designed to compound: as profits grow, spread sizes and contract counts grow, and as the system matures, new 0DTE instruments are added systematically.

**Why 0DTE now:** The PDT (Pattern Day Trader) rule was eliminated June 4, 2026. A $500 account can now trade 0DTE options every day with no restriction.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    LOCAL MACHINE                         │
│                                                         │
│  ┌─────────────────────────────────────────────────┐   │
│  │           LAYER 1: STRATEGY LAB                 │   │
│  │                                                 │   │
│  │  yfinance/CBOE/FRED → SPX Features → Direction   │   │
│  │  Classifier → Hermes 3 (Ollama) →               │   │
│  │  VectorBT Backtest → Spread P&L Engine →        │   │
│  │  Evaluate → Loop → Package Winning Strategy     │   │
│  └──────────────────────┬──────────────────────────┘   │
└─────────────────────────┼───────────────────────────────┘
                          │ Deploy via shared DB
                          ▼
┌─────────────────────────────────────────────────────────┐
│                   RAILWAY (CLOUD)                        │
│                                                         │
│  ┌─────────────────────────────────────────────────┐   │
│  │         LAYER 2: EXECUTION AGENT                │   │
│  │                                                 │   │
│  │  Webull MQTT (ES ticks) → Direction Classifier  │   │
│  │  → Spread Selector → Risk Manager →             │   │
│  │  Options Order Manager → Trade Log              │   │
│  └──────────────────────┬──────────────────────────┘   │
│                          │                              │
│  ┌──────────────────────▼──────────────────────────┐   │
│  │        LAYER 3: PORTFOLIO MANAGER               │   │
│  │                                                 │   │
│  │  Performance Tracker → Scaling Engine →         │   │
│  │  Strategy Rotator → Instrument Expansion        │   │
│  └─────────────────────────────────────────────────┘   │
│                                                         │
│  ┌─────────────────────────────────────────────────┐   │
│  │         SHARED: PostgreSQL Database             │   │
│  │  strategies | trades | equity | performance     │   │
│  └─────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

---

## Technology Stack

| Component | Technology | Notes |
|---|---|---|
| Strategy AI | Hermes 3 (NousResearch) via Ollama | Runs locally, free, private |
| Backtesting | VectorBT (Python) | Direction model: signal-based. Spread P&L: synthetic Black-Scholes via py_vollib + VIX. |
| Historical data | yfinance + CBOE VIX CSV + FRED | SPX/SPY OHLCV, VIX daily, risk-free rate — all free, no API key |
| Execution language | Python 3.11+ | All layers |
| Broker API | Webull OpenAPI (HTTP + MQTT + gRPC) | Options-enabled, has paper trading mode |
| Cloud hosting | Railway | Always-on, easy deploys, cheap |
| Shared database | PostgreSQL (Railway-hosted) | Central state for all agents |
| Containerization | Docker | Railway deploys from Dockerfile |

---

## Phase 1 — Strategy Lab (Local)
**Goal:** Build the AI-powered strategy discovery loop and produce one validated 0DTE spread strategy.

**Duration estimate:** 3–4 weeks
**Status:** 🔲 Not started

### Milestone 1.1 — Data Pipeline (Free Stack)
- [ ] Download VIX_History.csv from CBOE (one-time manual step)
- [ ] Build `market_data.py`: yfinance SPX/SPY daily + hourly fetcher, CBOE VIX loader, FRED risk-free rate
- [ ] Build `loader.py`: assemble per-day feature rows from SPY hourly bars (ORB, VWAP, delta proxy, gap)
- [ ] Build `indicators.py`: ORB (opening range high/low), gap %, cumulative delta proxy, VWAP, overnight range features
- [ ] Build `options.py`: synthetic spread pricing via py_vollib Black-Scholes (VIX as sigma, FRED as risk-free rate)
- [ ] Store cleaned data in local SQLite cache for fast repeat runs

### Milestone 1.2 — Direction Model + Spread Backtest Engine
- [ ] Install and configure VectorBT
- [ ] Build `BaseStrategy` interface: `classify_direction(data)`, `get_spread_params()`, `get_params()`
- [ ] Build `engine.py`: direction model backtest — given a full day's features, did the classifier predict correctly?
- [ ] Build `spread_engine.py`: given correct/incorrect direction and spread params, compute P&L using actual options chain data
- [ ] Define evaluation scorecard: direction accuracy %, Sharpe ratio, profit factor, win rate, max drawdown, expectancy, # trades

### Milestone 1.3 — Hermes Integration
- [ ] Install Ollama locally, pull Hermes 3 model (`ollama pull hermes3`)
- [ ] Build `client.py`: Ollama HTTP client with retry logic and JSON response parsing
- [ ] Build `prompts.py`: prompt templates for direction optimization and spread parameter tuning
- [ ] Build `parser.py`: single-variable enforcement — Hermes proposes change → system validates exactly one variable → applies it
- [ ] Wire up full loop: Hermes → backtest → evaluate → Hermes → repeat

### Milestone 1.4 — Strategy Validation & Packaging
- [ ] Implement OOS + walk-forward validation
- [ ] Define acceptance thresholds: direction accuracy > 55%, profit factor > 1.5, Sharpe > 0.8, max DD < 20%, ≥ 200 trades
- [ ] Build `packager.py`: serialize strategy to JSON config
- [ ] Build `registry.py`: store all tested strategies in PostgreSQL with full metrics

**Phase 1 Done When:** One strategy passes in-sample + out-of-sample validation and is packaged in the registry.

---

## Phase 2 — Execution Agent (Railway, Paper Trading)
**Goal:** Deploy the strategy to Railway, connect to Webull, prove the live pipeline works.

**Duration estimate:** 2–3 weeks
**Status:** 🔲 Not started

### Milestone 2.1 — Infrastructure Setup
- [ ] Set up Railway project, PostgreSQL instance
- [ ] Migrate strategy registry to PostgreSQL
- [ ] Set up Webull developer account, obtain API credentials
- [ ] Configure paper trading mode in Webull API
- [ ] Verify Webull API supports multi-leg SPX options orders (confirm SPX support; SPY as fallback)

### Milestone 2.2 — Live Direction Signal
- [ ] Build `feed.py`: Webull MQTT subscriber for live ES tick data
- [ ] Build `normalizer.py`: normalize ES ticks to 1-min OHLCV bars
- [ ] Build `classifier.py`: compute morning features (gap, ORB, delta) after 30–45 min → classify UP/DOWN/NEUTRAL
- [ ] Test: run classifier live for 5 days, log predicted direction and actual outcome

### Milestone 2.3 — Spread Selection & Risk Engine
- [ ] Build `selector.py`: given direction + strategy params, select SPX strike prices and spread type
- [ ] Build `pricer.py`: get real-time spread bid/ask from Webull options quote API
- [ ] Build `manager.py` (risk): calculate max loss per spread, enforce ≤ 10% of equity per trade (defined-risk spreads cap loss structurally)
- [ ] Build `limits.py`: hard-coded 5% daily halt, 3:45 PM forced exit — in code, not config

### Milestone 2.4 — Options Order Management
- [ ] Build `manager.py` (orders): place multi-leg options orders via Webull HTTP API
- [ ] Handle order states: pending fill, filled, partial, rejected
- [ ] Build position monitoring loop: check spread P&L vs. profit target and stop loss every minute
- [ ] Log every fill to PostgreSQL `trades` table
- [ ] Build watchdog: dead man's switch + 3:45 PM forced close of all open spreads

### Milestone 2.5 — Paper Trading Validation
- [ ] Run paper trading for minimum 30 days
- [ ] Compare live direction accuracy and P&L to backtest expectations
- [ ] Acceptable: live results within 20% of backtested results
- [ ] Zero critical errors (missed exits, disconnections causing open positions, unhandled exceptions)

**Phase 2 Done When:** 30 days of clean paper trading with results reasonably matching backtest.

---

## Phase 3 — Go Live
**Goal:** Switch to live trading with $500, maximum caution.

**Duration estimate:** Ongoing (30-day observation minimum before Phase 4)
**Status:** 🔲 Not started

### Milestone 3.1 — Live Launch
- [ ] Fund Webull account with $500
- [ ] Switch Execution Agent from paper to live mode (single env var: `WEBULL_TRADING_MODE=live`)
- [ ] Confirm hard limits are in code: max 1 SPX spread per day, max 10% per-trade risk (defined-risk spread), max 5% daily loss
- [ ] Enable daily email/SMS summary of P&L and system health

### Milestone 3.2 — 30-Day Observation
- [ ] Trade live for 30 days without changes
- [ ] Track: actual vs. expected P&L, direction accuracy, win rate, max drawdown
- [ ] Keep a manual trade journal (gut check on system behavior)
- [ ] Do not adjust strategy during this period — observe only

### Phase 3 Acceptance Criteria
- System profitable or within acceptable loss range of expectation
- No runaway losses or system failures
- Confidence in execution infrastructure

---

## Phase 4 — Portfolio Manager
**Goal:** Add intelligence around scaling, strategy rotation, and instrument expansion.

**Duration estimate:** 3–4 weeks (after Phase 3 validates)
**Status:** 🔲 Not started

### Milestone 4.1 — Performance Tracking
- [ ] Build daily equity curve tracker (equity snapshots)
- [ ] Build per-strategy performance dashboard
- [ ] Define strategy decay: rolling 30-day performance vs. backtest baseline

### Milestone 4.2 — Scaling Engine
- [ ] Define scaling rules (spread width and contract count grow with equity):
  - $5,000–$9,999: 1 contract, $5-wide SPX spread
  - $10,000–$19,999: 2 contracts, $5-wide SPX spread
  - $20,000–$34,999: 1 contract, $10-wide SPX spread (or 3 contracts $5-wide)
  - $35,000+: review and decide (larger spreads, multiple instruments)
- [ ] Automatic contract/width adjustment (weekly review cadence)
- [ ] Notify before any scaling event (human approval to start)

### Milestone 4.3 — Strategy Rotation
- [ ] Trigger Layer 1 automatically when current strategy decays
- [ ] Implement strategy A/B testing: 50% allocation to each, promote winner after 20 trades
- [ ] Graceful strategy retirement — never abrupt mid-day

### Milestone 4.4 — Instrument Expansion
Expansion is gated behind strict performance gates — not time.

| Instrument | Gate Requirement |
|---|---|
| NDX 0DTE (NDXP) | 60+ days live SPX profitability, separate strategy validated |
| RUT 0DTE (RUTW) | NDX proven, portfolio equity > $20,000 |
| SPX spreads widened ($10+) | Portfolio equity > $25,000, consistent track record |
| Multi-instrument concurrently | Portfolio equity > $50,000, human review required |

---

## Risk Management — Non-Negotiables

These rules are HARD limits baked into the code, not settings:

1. **Per-trade max loss**: Never risk more than 10% of account equity on a single spread. Max loss = `(spread_width - credit) × 100 × contracts`. Defined-risk spreads cap loss structurally — 1 contract of a $5-wide SPX spread at $5k equity is ~7%, which is acceptable.
2. **Daily loss limit**: If daily P&L hits -5% of equity, stop trading for the day
3. **Defined risk only**: All trades must be vertical spreads or iron condors — no naked positions
4. **Paper validation first**: No live trading of any strategy without 30-day paper validation
5. **No expiration holds**: All 0DTE positions closed by 3:45 PM ET — hard-coded in watchdog
6. **Dead man's switch**: System closes all positions if broker connection lost, daily limit hit, or unexpected error

---

## Overfitting Prevention

- **Out-of-sample validation**: The last 20% of historical data is never seen during optimization
- **Minimum trade count**: 200+ trades required (no curve-fitting on thin samples)
- **Single variable rule**: Hermes changes ONE parameter per iteration
- **Walk-forward testing**: Rolling cross-validation across multiple time periods
- **30-day paper requirement**: Always paper trade before going live
- **Live monitoring**: Compare live direction accuracy and P&L to backtest weekly

---

## Future Ideas (Backlog)

- VIX regime filter: adjust spread width and confidence threshold based on VIX level (wider condors in low VIX, skip condors in high VIX)
- News/catalyst filter: skip 0DTE on FOMC, CPI, or major earnings days
- Gamma scalping layer: hedge the direction model with a small futures position on high-confidence days
- Multi-broker redundancy: failover to second broker if primary goes down
- Web dashboard: real-time equity curve, open spreads, direction signal, system health
- Discord/Slack alerts: trade notifications, daily summary, system health

---

## Project Log

| Date | Event |
|---|---|
| May 2026 | Architecture designed, project initialized (MES futures) |
| June 2026 | Pivoted to 0DTE SPX options. PDT rule eliminated June 4, 2026. |
| June 2026 | Switched to free data stack: yfinance + CBOE VIX CSV + FRED + py_vollib synthetic pricing. Barchart OnDemand is a separate paid product — not viable. |
| June 2026 | Instrument updated from XSP to SPX. Starting capital updated to $5,000. |
