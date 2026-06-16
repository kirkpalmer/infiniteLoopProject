# CLAUDE.md — InfiniteLoop Trading System

This file gives Claude full context for working on the InfiniteLoop project. Read it completely before writing any code or making any suggestions.

---

## What This Project Is

InfiniteLoop is a three-layer autonomous 0DTE options trading system. It discovers trading strategies using AI, executes them live via broker API, and manages a growing portfolio. The system is designed to compound: find edge → trade it → grow it → find more edge.

**Owner:** Kirk (software developer, experienced futures trader)
**Starting capital:** $5,000 on 0DTE SPX options (S&P 500 Index, European-style, cash-settled)
**Broker:** Webull (OpenAPI — HTTP + MQTT + gRPC)
**Language:** Python 3.11+ throughout
**Full docs:** See `ROADMAP.md` and `docs/ARCHITECTURE.md`

---

## The Three Layers

### Layer 1 — Strategy Lab (`strategy-lab/`) — runs LOCAL
Discovers and validates 0DTE spread strategies using a Hermes 3 AI loop. Uses free historical data (SPY/SPX via yfinance, VIX from CBOE) for direction classification, and synthetic options pricing via Black-Scholes (`py_vollib` + VIX as IV proxy) for spread P&L modeling. Packages validated strategies to PostgreSQL for Layer 2 to consume.

### Layer 2 — Execution Agent (`execution-agent/`) — runs on RAILWAY
Always-on service. Subscribes to Webull MQTT for live ES futures tick data. After the first 30–45 minutes of the trading day, classifies market direction (UP / DOWN / NEUTRAL) and selects the appropriate 0DTE spread to sell. Places options orders via Webull HTTP API. Logs everything to PostgreSQL.

### Layer 3 — Portfolio Manager (`portfolio-manager/`) — runs on RAILWAY
Monitors equity growth, scales spread width and contract count, detects strategy decay, governs expansion to new instruments (QQQ 0DTE, GLD 0DTE, etc.). Runs on scheduled cadences, not tick-by-tick.

### Shared — PostgreSQL (hosted on Railway)
Central state for all three layers. Tables: `strategies`, `trades`, `equity_snapshots`, `portfolio_events`. Schema in `docs/ARCHITECTURE.md`.

---

## Hermes Agents — Multi-Agent Design

The system uses **multiple specialized Hermes agents**, each optimizing one responsibility. They do not share state or optimization loops.

| Agent | Name | Responsibility | Metric it optimizes |
|---|---|---|---|
| Direction Agent | **Oracle** | Classify each trading day as UP / DOWN / NEUTRAL | Directional accuracy (% correct per class) |
| Trade Agent | *(unnamed, Phase 2)* | Select strike, spread width, entry/exit timing | P&L, Sharpe, win rate |

**Oracle is Phase 1.** The trade agent is Phase 2+. Do not build trade agent logic until Oracle is validated.

Oracle's Hermes loop changes ONE direction parameter at a time and reports accuracy, never P&L. The trade agent's loop is separate, runs after Oracle is locked, and changes ONE spread/sizing parameter at a time.

---

## Strategy Type

The system trades **0DTE (zero days to expiration) vertical spreads and iron condors on SPX (S&P 500 Index)**. SPX options are European-style and cash-settled — no assignment risk. The PDT rule was eliminated June 4, 2026 — a retail account can trade 0DTE every day with no penalty. The core logic is:

1. **Direction classification (Oracle)** — Use overnight ES futures orderflow + first 30–45 min of RTH trading to classify the day as UP, DOWN, or NEUTRAL.
2. **Spread selection (Trade Agent, Phase 2)** — Route to the appropriate defined-risk spread:
   - UP → Sell Bull Put Spread (sell OTM put, buy further OTM put) — collect credit, profit if market stays above short strike
   - DOWN → Sell Bear Call Spread (sell OTM call, buy further OTM call) — collect credit, profit if market stays below short strike
   - NEUTRAL → Sell Iron Condor (both sides) — profit from a range-bound day
   - Low confidence → Skip (stay in cash that day)
3. **Theta decay** — Enter the spread mid-morning, collect premium, close at profit target or stop before 3:45 PM ET.

**NEUTRAL definition:** A day is labeled NEUTRAL if SPX closes within ±Expected Move of the RTH open. Expected Move = `SPX_open × (VIX / 100) × √(1/252)`. This is the ATM straddle approximation — the market-implied 1-sigma daily range. Iron condors profit when the day stays inside this range.

**Direction classification features (inputs to Oracle's Hermes-optimized logic):**
- Previous day: range %, close vs. VWAP, final-hour delta bias, volume vs. 30-day average
- Overnight session: gap % (RTH open vs. prior close), overnight high/low range, open position relative to overnight range
- First 30–45 min RTH: Opening Range Breakout (ORB) direction, delta bias, VWAP slope, relative volume
- ES/MES orderflow (from Webull MQTT live): cumulative delta, absorption at key levels, imbalance

**What Oracle's Hermes optimizes (one parameter at a time):**
- `gap_threshold_pct` — minimum gap size to consider directional
- `orb_breakout_pct` — ORB breakout magnitude needed to signal direction
- `delta_bias_threshold` — cumulative delta threshold for UP/DOWN signal
- `neutral_band_pct` — width of neutral band (days below this threshold → NEUTRAL)
- `vwap_slope_threshold` — minimum VWAP slope to count as directional
- `vol_filter_high` — skip days when VIX is above this (chaotic, unpredictable)
- `vol_filter_low` — skip days when VIX is below this (no premium to sell)

**What Oracle's Hermes NEVER changes:** `expected_move_formula`, `neutral_definition`, `oos_split_pct`

**What the Trade Agent's Hermes optimizes (Phase 2, not Oracle):**
- `entry_hour`, `short_delta`, `spread_width_usd`, `profit_target_pct`, `stop_loss_pct`

**What is NEVER changed by any Hermes agent:** `max_loss_pct`, `daily_halt_pct`, `forced_exit_hour` (3:45 PM ET)

Simple MA crossovers, generic indicators, or naked options are NOT what we're building.

---

## Non-Negotiable Risk Rules

These are HARD constraints. They must be enforced in code, not configuration. Never suggest removing or softening them:

1. **Per-trade max loss**: Never risk more than 10% of current account equity on a single spread. Max loss = `(spread_width - credit_received) × 100 × contracts`. SPX contracts are large — a $5-wide spread at $5,000 equity represents ~7% max risk per trade, which is acceptable because the loss is structurally defined. Size to 1 contract maximum until equity exceeds $15,000.
2. **Daily loss limit**: If daily P&L hits -5% of account equity, stop all trading for the day — no exceptions.
3. **Defined risk only**: Every trade must be a vertical spread or iron condor. No naked long or short single-leg options ever.
4. **No live trading without paper validation**: Every strategy must run 30 days in paper mode before going live.
5. **Dead man's switch**: If broker connection is lost, daily limit is hit, or an unhandled exception occurs — close any open spreads immediately and halt.
6. **Out-of-sample validation is mandatory**: The last 20% of historical data is NEVER used during the Hermes optimization loop. Only used for final validation.
7. **Minimum trade count**: A strategy must generate 200+ trades in backtest before being considered. No curve-fitting on thin samples.
8. **No expiration holds**: All 0DTE positions must be closed by 3:45 PM ET. Never hold a 0DTE spread to expiration. Enforced by the watchdog, not just strategy logic.

If you're ever asked to relax these rules, push back and explain why they exist.

---

## Technology Stack

| Component | Tech | Notes |
|---|---|---|
| Strategy AI | Hermes 3 (NousResearch) via Ollama | Local inference, free, private. Model tag: `hermes3` |
| Backtesting | VectorBT (`vectorbt`) | Direction model: signal-based VectorBT. Spread P&L: synthetic Black-Scholes pricing. |
| Historical price data | `yfinance` | SPX daily OHLCV (`^GSPC`) + SPY hourly/1-min for intraday features. Free, no key needed. |
| Historical VIX data | CBOE free CSV | VIX daily history — used as IV proxy for Black-Scholes spread pricing. Free download. |
| Risk-free rate | FRED API (`pandas-datareader`) | 3-month T-bill rate for Black-Scholes. Free, no key needed. |
| Spread P&L modeling | `py_vollib` | Black-Scholes pricing using VIX as IV input. Synthetic but realistic for 0DTE spreads. |
| Broker | Webull OpenAPI | HTTP for multi-leg options orders, MQTT for live ES tick data (direction signal), gRPC for fill events |
| Cloud hosting | Railway | Docker-based deploys |
| Database | PostgreSQL | `asyncpg` for async in Layers 2/3, `psycopg2` in Layer 1 |
| Env management | `python-dotenv` | `.env` file, never commit secrets |
| Logging | Python `logging` + structured JSON | Layer 2 and 3 use JSON for Railway log aggregation |

---

## Project Structure

```
infiniteLoopProject/
├── CLAUDE.md                    ← You are here
├── README.md                    ← Project overview
├── ROADMAP.md                   ← Phased build plan with milestones
├── docs/
│   └── ARCHITECTURE.md          ← Full technical reference
├── strategy-lab/                ← Layer 1 (local)
│   ├── data/
│   │   ├── market_data.py       # yfinance + CBOE VIX + FRED data fetcher (free sources)
│   │   ├── loader.py            # Load & clean OHLCV, assemble per-day direction features
│   │   ├── indicators.py        # SPY/SPX indicators: delta proxy, VWAP, ORB, relative vol
│   │   ├── options.py           # Synthetic spread P&L via py_vollib + VIX (Black-Scholes)
│   │   ├── events.py            # Economic event calendar (FOMC/CPI/NFP/PCE skip days)
│   │   ├── vol_regime.py        # VIX regime gate (skip when VIX too high or too low)
│   │   └── store.py             # Local SQLite cache
│   ├── oracle/                  # ← ORACLE: Direction Agent (Phase 1)
│   │   ├── __init__.py
│   │   ├── features.py          # Build daily feature vectors: gap%, ORB, delta, VWAP slope
│   │   ├── outcomes.py          # Label historical days UP/DOWN/NEUTRAL using expected move
│   │   ├── classifier.py        # Oracle direction classifier (seed: ORB + gap logic)
│   │   ├── backtest.py          # Accuracy backtest: % correct per class, confusion matrix
│   │   └── hermes_loop.py       # Oracle's Hermes optimization loop (direction params only)
│   ├── backtest/
│   │   ├── engine.py            # Direction-model backtest (VectorBT signal-based)
│   │   ├── spread_engine.py     # Spread P&L backtest — Phase 2, not Oracle
│   │   ├── metrics.py           # Scorecard: Sharpe, PF, win_rate, DD, expectancy
│   │   └── validator.py         # OOS + walk-forward validation
│   ├── hermes/
│   │   ├── client.py            # Ollama HTTP client (shared by all agents)
│   │   ├── prompts.py           # Prompt templates — oracle_direction.py + trade_sizing.py
│   │   └── parser.py            # Parse response, enforce single-variable rule
│   ├── strategy/
│   │   ├── base.py              # Strategy abstract base class
│   │   ├── registry.py          # Read/write PostgreSQL strategy registry
│   │   └── packager.py          # Serialize/deserialize strategy objects
│   └── loop.py                  # Main discovery loop orchestrator
├── execution-agent/             ← Layer 2 (Railway)
│   ├── data/
│   │   ├── feed.py              # Webull MQTT subscriber (ES tick data for direction)
│   │   └── normalizer.py        # Normalize ticks to 1-min OHLCV bars
│   ├── strategy/
│   │   └── loader.py            # Pull active strategy from DB
│   ├── signals/
│   │   └── classifier.py        # Classify direction after 30–45 min, emit UP/DOWN/NEUTRAL
│   ├── spreads/
│   │   ├── selector.py          # Select spread type and strikes based on direction + params
│   │   └── pricer.py            # Real-time spread pricing via Webull options quote API
│   ├── risk/
│   │   ├── manager.py           # Position sizing: max loss ≤ 10% equity per defined-risk spread
│   │   └── limits.py            # Hard-coded safety limits (not config)
│   ├── orders/
│   │   ├── manager.py           # Webull HTTP multi-leg options order placement
│   │   └── state.py             # Track open spreads/positions
│   ├── logging/
│   │   └── trade_logger.py      # Write fills to PostgreSQL
│   ├── health/
│   │   └── watchdog.py          # Dead man's switch, 3:45 PM forced exit
│   ├── Dockerfile
│   └── main.py
└── portfolio-manager/           ← Layer 3 (Railway)
    ├── tracking/
    │   ├── equity.py            # Daily equity snapshots
    │   └── performance.py       # Per-strategy rolling metrics
    ├── scaling/
    │   └── engine.py            # Spread width + contract count scaling rules
    ├── rotation/
    │   ├── decay_detector.py    # Live vs. backtest drift detection
    │   └── rotator.py           # Strategy A/B testing, promotion/retirement
    ├── expansion/
    │   └── gate_checker.py      # Instrument expansion gates (QQQ 0DTE, etc.)
    ├── notifications/
    │   └── alerts.py            # Email/SMS alerts
    ├── Dockerfile
    └── main.py
```

---

## Development Phases

Work follows this strict order. Do not build Phase 2 until Phase 1 is working. Do not go live until paper trading validates.

- **Phase 1** — Strategy Lab: yfinance + VIX data pipeline → direction classifier → synthetic spread P&L backtest → Hermes loop → validation → packaging
- **Phase 2** — Execution Agent on Railway in **paper trading mode**
- **Phase 3** — Go live with $500, observe for 30 days, no changes
- **Phase 4** — Portfolio Manager: scaling, rotation, instrument expansion

Check `ROADMAP.md` for detailed milestone checklists.

---

## Coding Conventions

### General
- Python 3.11+, type hints on all function signatures
- Async (`asyncio`) for Layers 2 and 3; synchronous is fine for Layer 1
- Dataclasses or Pydantic models for data structures — no bare dicts for important objects
- Every module gets a docstring explaining its role in the system
- No magic numbers — constants go in a `constants.py` or at the top of the file with a comment

### Error Handling
- Layer 2 never crashes silently. All exceptions must be caught, logged, and trigger the watchdog if order-related.
- Use specific exception types, not bare `except Exception`
- Log at WARNING for recoverable issues, ERROR for things that affect trading, CRITICAL for anything that triggers a halt

### Database
- Use parameterized queries always — no string interpolation into SQL
- All DB writes in Layer 2 happen AFTER a trade is confirmed filled, not on signal
- Use transactions for multi-table writes

### Strategy Interface
Every strategy must implement this interface (defined in `strategy-lab/strategy/base.py`):

```python
class BaseStrategy(ABC):
    @abstractmethod
    def classify_direction(self, data: pd.DataFrame) -> str:
        """Return 'UP', 'DOWN', or 'NEUTRAL' based on morning session features.
        Data includes: previous day OHLCV + indicators, overnight bars, first N RTH bars."""
        ...

    @abstractmethod
    def get_spread_params(self) -> dict:
        """Return spread parameters dict:
        {
          'entry_hour': int,          # e.g. 10 (enter between 10:00–10:59 AM ET)
          'short_delta': int,         # e.g. 20 (sell the ~20-delta strike)
          'spread_width_usd': int,    # e.g. 5 (5-point wide spread)
          'profit_target_pct': int,   # e.g. 50 (close at 50% of max credit)
          'stop_loss_pct': int,       # e.g. 200 (close if loss = 2x credit received)
          'forced_exit_hour': int,    # always 15 (3:00 PM ET) — not changeable by Hermes
        }
        """
        ...

    @abstractmethod
    def get_params(self) -> dict:
        """Return full parameter dict (direction params + spread params) for serialization."""
        ...

    @abstractmethod
    def get_name(self) -> str:
        """Return unique strategy name string."""
        ...
```

### Hermes Prompt Rules (applies to ALL agents)
- Always instruct Hermes to respond in valid JSON
- Always include the current parameter dict in the prompt
- Always include the last N backtest results (not just the most recent)
- Enforce the single-variable rule in the parser (`hermes/parser.py`) — not just in the prompt
- Each agent's Hermes loop ONLY touches parameters within its scope (see agent table above)
- Hermes NEVER changes: `forced_exit_hour`, `max_loss_pct`, `daily_halt_pct`, `expected_move_formula`

### Oracle-Specific Hermes Rules
- Oracle's metric is **directional accuracy** — never P&L
- Oracle reports: overall accuracy, per-class accuracy (UP/DOWN/NEUTRAL), confusion matrix
- Oracle's Hermes prompt includes: current thresholds, last 10 backtest accuracy results, class distribution of training set
- Oracle optimizes direction threshold parameters ONLY — not strike, width, or timing

---

## Environment Variables

```bash
# Webull API
WEBULL_APP_KEY=
WEBULL_APP_SECRET=
WEBULL_TRADE_TOKEN=
WEBULL_ACCOUNT_ID=
WEBULL_TRADING_MODE=paper        # MUST be 'paper' until Phase 3 approved

# Database
DATABASE_URL=postgresql://user:pass@host:port/dbname

# Data sources — all free, no API keys needed
# yfinance: no key required
# VIX: downloaded from CBOE as free CSV → strategy-lab/data/raw/vix/VIX_History.csv
# FRED (risk-free rate): no key required via pandas-datareader

# Ollama (Layer 1)
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=hermes3

# Risk limits (Layer 2) — defaults; hard limits are in code
MAX_DAILY_LOSS_PCT=0.05
MAX_RISK_PER_TRADE_PCT=0.10

# Notifications
ALERT_EMAIL=kirkpalmer67@gmail.com
```

Never commit `.env` to git. Always add it to `.gitignore`.

---

## Key Design Decisions (Don't Relitigate Without Good Reason)

1. **VectorBT for backtesting**: Fast for the iterative Hermes loop. Direction model uses VectorBT signal-based backtesting. Spread P&L uses Black-Scholes synthetic pricing (py_vollib + VIX as sigma) — all free, no paid data provider needed.

2. **Single variable at a time**: Hermes changes ONE parameter per iteration. Scientific method. Enforced in the parser, not just the prompt.

3. **PostgreSQL as the inter-layer bus**: Layers communicate through the DB only. No direct API calls between services.

4. **SPX as the primary instrument**: S&P 500 Index options (SPXW for weekly/daily expiries), European-style, cash-settled, no assignment risk, 0DTE available every trading day. Each point = $100. With $5,000 equity, 1 contract of a $5-wide spread is standard sizing. SPY is the fallback if Webull doesn't support SPX multi-leg orders in their API.

5. **Direction-first, spread-second**: The direction classifier is the primary edge. The spread structure amplifies it. Hermes optimizes both, but direction accuracy drives profitability more than spread width selection.

6. **Webull as broker**: PDT rule eliminated June 4, 2026. $500 account trades 0DTE freely. Webull supports options and has a real developer API. Don't suggest switching unless Webull's multi-leg options API has a fundamental blocker.

7. **Railway for hosting**: Simple, cheap, Docker-based. Revisit when portfolio exceeds $10k.

8. **Free data stack for backtesting**: `yfinance` provides SPX/SPY daily and hourly OHLCV at no cost. CBOE publishes VIX daily history as a free CSV. `py_vollib` + VIX as IV proxy replaces historical options chain data — spread P&L is computed synthetically via Black-Scholes. This is a known approximation; paper trading in Phase 2 validates the gap between synthetic and real pricing. If the system is profitable in paper trading, upgrading to a paid data source is a future option.

9. **No expiration holds**: 0DTE options in the last 30 minutes can move violently as gamma spikes. The 3:45 PM forced exit is hard-coded in the watchdog, not configurable.

---

## What "Done" Looks Like for Each Phase

- **Phase 1 done**: Hermes loop runs end-to-end, produces a direction classifier + spread config that passes OOS + walk-forward validation, stored in strategy registry
- **Phase 2 done**: Execution agent runs on Railway in paper mode for 30 days, direction accuracy and P&L within 20% of backtest expectation, zero critical errors
- **Phase 3 done**: 30 days of live trading with $500, system profitable or within expected range, no system failures
- **Phase 4 done**: Portfolio manager auto-scales spread width/contracts, detects decay and triggers re-discovery, expansion gates implemented

---

## Current Status

| Phase | Status |
|---|---|
| Phase 1 — Oracle (Direction Agent) | 🔲 In progress |
| Phase 2 — Execution Agent (paper mode) | 🔲 Not started |
| Phase 3 — Go Live | 🔲 Not started |
| Phase 4 — Portfolio Manager | 🔲 Not started |

**Oracle build order:**
1. `strategy-lab/data/market_data.py` — fetch SPX/SPY/VIX
2. `strategy-lab/oracle/features.py` — daily feature vectors
3. `strategy-lab/oracle/outcomes.py` — label UP/DOWN/NEUTRAL using expected move
4. `strategy-lab/oracle/classifier.py` — seed ORB+gap classifier
5. `strategy-lab/oracle/backtest.py` — accuracy backtest + confusion matrix
6. `strategy-lab/oracle/hermes_loop.py` — Hermes optimization loop

**Dashboard build order (parallel to Oracle):**
1. `dashboard/oracle.html` — Oracle accuracy page (dark theme)
2. Additional dashboard pages added per-phase

Last updated: June 2026 — Pivoted from MES futures scalping to 0DTE SPX options premium selling. PDT rule eliminated June 4, 2026. Starting capital: $5,000. Instrument: SPX (SPXW) 0DTE verticals and iron condors. Multi-agent Hermes architecture: Oracle handles direction, unnamed trade agent handles sizing/exits (Phase 2+).
