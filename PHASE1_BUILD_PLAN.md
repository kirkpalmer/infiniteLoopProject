# InfiniteLoop — Phase 1 Build Plan
## 0DTE SPX Options Strategy Lab

This document is the complete specification for building Phase 1 (Strategy Lab) of InfiniteLoop. It is divided into three sections:

1. **Manual Prerequisites** — things Kirk must do before writing any code
2. **Build Instructions** — paste each section into Claude Code in sequence
3. **Success Criteria** — what "done" looks like

> **Read CLAUDE.md before starting.** Claude Code should read `CLAUDE.md` at the root of the project first. These are the source of truth for conventions, risk rules, and architecture decisions.

---

# SECTION 1 — MANUAL PREREQUISITES

---

### STEP M1 — Install Python 3.11+
1. Download from https://www.python.org/downloads/ (3.11 or 3.12)
2. On Windows: check **"Add Python to PATH"** during install
3. Verify: `python --version`

---

### STEP M2 — Install Git
1. Download from https://git-scm.com/downloads
2. Install with defaults
3. Verify: `git --version`

---

### STEP M3 — Create a Private GitHub Repository
1. Go to https://github.com → New repository → name: `infiniteLoop` → Private
2. Do NOT initialize with README (project already has files)
3. In terminal, navigate to `C:\MyBrain\My Brain\infiniteLoopProject\` and run:
   ```
   git init
   git remote add origin <your-repo-url>
   ```

---

### STEP M4 — Install Ollama (Local AI Server for Hermes 3)
1. Go to https://ollama.com → download Windows installer
2. Run installer (Ollama runs as a background service)
3. Open terminal and pull Hermes 3:
   ```
   ollama pull hermes3
   ```
   (~4-5 GB download)
4. Verify:
   ```
   ollama run hermes3 "Respond in JSON: {\"status\": \"ok\"}"
   ```
5. Ollama runs at `http://localhost:11434` — leave it running.

---

### STEP M5 — Confirm Free Data Sources (No Paid API Required)

All historical data for Phase 1 comes from free sources — no API key or paid subscription needed.

| Data | Source | How |
|------|--------|-----|
| SPX/SPY daily + hourly OHLCV | Yahoo Finance via `yfinance` | Auto-fetched by `market_data.py` |
| VIX daily history | CBOE free CSV | One manual download (see MANUAL STOP 1) |
| Risk-free rate | FRED via `pandas-datareader` | Auto-fetched; no key needed |
| Spread P&L | `py_vollib` Black-Scholes | Computed locally; VIX is the IV input |

**Nothing to do in this step** — `market_data.py` handles all fetching automatically. The only manual step is downloading the VIX CSV (covered in MANUAL STOP 1).

---

### STEP M6 — Set Up PostgreSQL on Railway
1. Go to https://railway.app → create a free account
2. Click **New Project** → **Provision PostgreSQL**
3. Once created: click PostgreSQL service → **Connect** tab → copy **Database URL**
4. URL format: `postgresql://postgres:password@host.railway.app:5432/railway`

---

### STEP M7 — Create the .env File
Create `C:\MyBrain\My Brain\infiniteLoopProject\.env`:
```bash
# Webull API — leave blank for Phase 1 (not needed yet)
WEBULL_APP_KEY=
WEBULL_APP_SECRET=
WEBULL_TRADE_TOKEN=
WEBULL_ACCOUNT_ID=
WEBULL_TRADING_MODE=paper

# Database
DATABASE_URL=postgresql://postgres:password@host:5432/dbname

# Ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=hermes3

# Risk limits (hard limits are in code)
MAX_DAILY_LOSS_PCT=0.05
MAX_RISK_PER_TRADE_PCT=0.10

# Notifications
ALERT_EMAIL=kirkpalmer67@gmail.com
```
Never commit this file. It goes in `.gitignore`.

---

### STEP M8 — Open Claude Code in the Project Folder
1. `cd "C:\MyBrain\My Brain\infiniteLoopProject"`
2. `claude`
3. Tell Claude Code: *"Read CLAUDE.md completely before we start. Then read PHASE1_BUILD_PLAN.md — it is the full build spec. We are building Phase 1 step by step. Start with Step 1: Project Scaffolding."*

---

# SECTION 2 — BUILD INSTRUCTIONS

---

> **How Claude Code should use this plan:**
> - Work through steps in order. Announce each step before starting it.
> - At every `⏸️ MANUAL STOP`, pause completely and wait for Kirk to confirm before continuing.
> - After each step, run any tests or verification commands specified. Fix failures before moving on.
> - If Kirk is interrupted mid-session, he will tell you which step to resume from.

---

## ⏸️ MANUAL STOP 0 — Verify Prerequisites Before Writing Any Code

**Claude Code: Before writing a single line of Python, confirm the following with Kirk.**

Ask Kirk to verify each item below. Do not proceed to STEP 1 until all are confirmed.

| # | Prerequisite | How to verify |
|---|---|---|
| 1 | Python 3.11+ installed | `python --version` |
| 2 | Git installed | `git --version` |
| 3 | Ollama installed with hermes3 model | `ollama list` — should show hermes3 |
| 4 | Ollama is currently running | `ollama ps` — or start it |
| 5 | Railway PostgreSQL provisioned | Kirk has the DATABASE_URL string |
| 6 | `.env` file created at project root with DATABASE_URL filled in | Open and confirm |

Tell Kirk: **"Please confirm each item above, then say 'ready' and I will begin STEP 1."**

---

## STEP 1 — Project Scaffolding

**Prompt for Claude Code:**
```
Read CLAUDE.md and PHASE1_BUILD_PLAN.md for full context.

Create the full directory structure for Phase 1 (Strategy Lab) of InfiniteLoop.
Create all directories and empty __init__.py files in each:

strategy-lab/
  data/
    raw/                      # Downloaded data files — gitignored
      spx_daily/              # SPX daily OHLCV from Barchart.com (already downloaded)
      futures/                # ES 1-min CSVs from Barchart.com — ESH20–ESH26 (already downloaded)
      vix/                    # VIX_History.csv from CBOE/Barchart (already downloaded)
  backtest/
  hermes/
  strategy/
  tests/
  logs/

Create the following files:

1. strategy-lab/constants.py with these constants and inline comments:
   # SPX options constants
   SPX_MULTIPLIER = 100
   SPX_TICK_SIZE = 0.01

   # Backtesting
   OOS_SPLIT = 0.80
   WALK_FORWARD_FOLDS = 5
   MIN_TRADE_COUNT = 200

   # Direction model defaults (Hermes optimizes these)
   GAP_THRESHOLD_PCT = 0.25
   ORB_BREAKOUT_PCT = 0.12
   NEUTRAL_BAND_PCT = 0.20
   ORB_MINUTES = 30        # use first 30 minutes to establish ORB

   # Spread defaults (Hermes optimizes these)
   DEFAULT_SHORT_DELTA = 20
   DEFAULT_SPREAD_WIDTH_USD = 5
   DEFAULT_PROFIT_TARGET_PCT = 50
   DEFAULT_STOP_LOSS_PCT = 200

   # Risk — HARD LIMITS, never change
   MAX_RISK_PER_TRADE_PCT = 0.10  # 10% of equity — defined-risk spreads cap loss structurally
   MAX_DAILY_LOSS_PCT = 0.05
   FORCED_EXIT_HOUR = 15
   FORCED_EXIT_MINUTE = 45

   # Validation thresholds (Tier 3 — full validation)
   MIN_DIRECTION_ACCURACY = 0.55
   MIN_SHARPE = 0.8
   MIN_PROFIT_FACTOR = 1.5
   MAX_DRAWDOWN_PCT = 0.20

   # Tiered promotion system — fail fast, only run full backtest when promising
   TIER1_MONTHS = 6          # quick screen: 6 months in-sample
   TIER2_YEARS = 2           # medium: 2 years in-sample
   TIER3_YEARS = 4           # full: 4 years IS + OOS (last 20%)
   OOS_FRACTION = 0.20       # last 20% of data is OOS — never touched during optimization

   TIER1_MIN_ACCURACY = 0.50  # Tier 1 pass threshold
   TIER1_MIN_PF = 1.20

   TIER2_MIN_ACCURACY = 0.55  # Tier 2 pass threshold
   TIER2_MIN_PF = 1.50
   TIER2_MIN_SHARPE = 0.80
   TIER2_MAX_DD = 0.25

   TIER3_OOS_TOLERANCE = 0.20  # OOS accuracy must be within 20% of in-sample

   # Event calendar skip — hard-coded, not configurable
   # Updated annually from: federalreserve.gov, bls.gov, bea.gov
   # See strategy-lab/data/events.py for the full year's schedule

   # Volatility regime gate — Hermes-optimizable
   DEFAULT_MIN_IV_RANK_PCT = 25.0  # skip if IV rank below this (not enough premium)
   DEFAULT_MAX_VIX = 32.0          # skip if VIX above this (realized vol too high)

2. strategy-lab/requirements.txt:
   vectorbt>=0.26.0
   pandas>=2.0.0
   numpy>=1.24.0
   psycopg2-binary>=2.9.0
   python-dotenv>=1.0.0
   requests>=2.31.0
   scipy>=1.11.0
   py_vollib>=1.0.1
   yfinance>=0.2.40      # SPX/SPY OHLCV — free, unlimited daily, 2yr hourly
   pandas-datareader>=0.10.0  # FRED risk-free rate (DTB3)
   pytest>=7.4.0
   pytest-cov>=4.1.0
   rich>=13.0.0          # results dashboard terminal rendering
   matplotlib>=3.7.0     # equity curve chart in dashboard HTML export

3. .gitignore at project root (if missing):
   .env
   __pycache__/
   *.pyc
   *.pyo
   .pytest_cache/
   *.db
   .venv/
   venv/
   strategy-lab/logs/
   strategy-lab/data/raw/

4. strategy-lab/data/raw/.gitkeep files in each subdirectory (spx_daily/, spx_hourly/, vix/)

Then run:
  pip install -r strategy-lab/requirements.txt --break-system-packages

Confirm all directories and files are created.
```

---

## ⏸️ MANUAL STOP 1 — Confirm Data Files Are in Place

**Claude Code: Pause here and verify the Barchart data files Kirk already downloaded are present and readable.**

Run these checks:
```bash
cd strategy-lab
ls data/raw/spx_daily/
ls data/raw/futures/
ls data/raw/vix/
```

Tell Kirk:

> "You already have the key data files downloaded from Barchart.com. Let me confirm they're all in place:
>
> - SPX daily CSV: [found / missing]
> - ES futures CSVs (esh20–esh26): [N files found / missing]
> - VIX_History.csv: [found / missing]
>
> If any are missing, let me know and I'll tell you where to re-download them. If all are present, say 'confirmed' and I'll continue."

Do not proceed to STEP 1B until Kirk confirms all files are present.

---

## STEP 1B — Historical Data Guide

**All data sources are free.** Two are fetched automatically by code; one requires a manual download.

---

### Data 1 — VIX Daily History (ALREADY DOWNLOADED ✅)

`strategy-lab/data/raw/vix/VIX_History.csv` — 9,200+ rows from 1990 through June 2026.
Columns: DATE, OPEN, HIGH, LOW, CLOSE. `barchart_csv.py` reads it directly.

If this file ever needs to be re-downloaded: `https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv`

---

### Data 2 — SPX Daily OHLCV (ALREADY DOWNLOADED ✅)

`strategy-lab/data/raw/spx_daily/spx_daily_historical-data-download-06-07-2026.csv`
1,616 rows, Jan 2, 2020 – Jun 5, 2026. Columns (Barchart format): Time, Open, High, Low, Latest (=close), Change, %Change, Volume.
`barchart_csv.py` reads and normalizes this.

### Data 2B — ES Futures 1-min (ALREADY DOWNLOADED ✅)

`strategy-lab/data/raw/futures/esh20_...` through `esh26_...` — 7 files, ~20,000 rows each.
Covers the last ~3 weeks before each March quarterly expiration (2020–2026). ~105 trading days total.
These are actual futures tick bars — higher quality than SPY hourly for direction features on those dates.

### Data 2C — SPY Hourly (yfinance — auto-fetched for non-covered dates)

For all trading days NOT covered by the ES CSV files (~75% of dates), `market_data.py` fetches SPY hourly bars from yfinance automatically. No account or API key required.

---

### Data 3 — Risk-Free Rate (FRED — auto-fetched)

**Why:** Black-Scholes requires a risk-free rate input. We use the 3-month T-bill rate (DTB3).

**Source:** FRED (Federal Reserve Economic Data) via `pandas-datareader`. No account or API key needed.

`options.py` fetches this automatically and caches it. Updated infrequently — one fetch covers years of data.

---

### Data 4 — Spread P&L (Synthetic — computed locally)

**Why:** The current Phase 1 workflow does not require raw historical options-chain files. Spread P&L is computed locally from SPX daily data, VIX history, and Black-Scholes assumptions, so `data/raw/options/` is reserved for future raw chain imports and may remain empty.

**How it works:**
- `py_vollib` prices each option leg using: SPX price (yfinance), target delta, VIX-derived sigma, FRED rate
- Spread credit = short leg price − long leg price
- Exit prices are re-computed at the same time markers (entry hour, profit target, stop, 3:45 PM forced exit) using declining time-to-expiry

**Accuracy:** Sufficient for direction model validation. The 30-day paper trading period in Phase 2 measures the gap between synthetic and real fills.

---

### Summary Table

| Data | Source | Status | Folder |
|------|--------|--------|--------|
| VIX daily history | CBOE CSV | ✅ Already downloaded | `data/raw/vix/` |
| SPX daily OHLCV | Barchart.com CSV | ✅ Already downloaded | `data/raw/spx_daily/` |
| ES futures 1-min | Barchart.com CSV (ESH20–ESH26) | ✅ Already downloaded | `data/raw/futures/` |
| Raw options chains | Not required in current Phase 1 workflow | Reserved | `data/raw/options/` |
| SPY hourly (gap fill) | yfinance | Auto-fetched on first run | SQLite cache |
| Risk-free rate | FRED via pandas-datareader | Auto-fetched on first run | SQLite cache |
| Spread P&L | py_vollib Black-Scholes | Computed locally | n/a |

**Prompt for Claude Code:**
```
Verify the data directory structure for InfiniteLoop Phase 1.

The following files were already downloaded by Kirk and should be present:
  strategy-lab/data/raw/spx_daily/   — one SPX daily CSV (Barchart format)
  strategy-lab/data/raw/futures/     — ESH20 through ESH26 CSVs (Barchart 1-min format)
  strategy-lab/data/raw/vix/         — VIX_History.csv (CBOE format)
  strategy-lab/data/raw/options/     — intentionally empty for the current Phase 1 workflow

List the contents of each directory. If any are missing, flag them.
Do not delete or move any existing files.
No download script is needed — Barchart CSVs are already on disk.
`data/raw/options/` is kept as a placeholder for future raw options-chain ingestion.
yfinance will auto-fetch SPY hourly for non-covered dates when market_data.py runs.
```

---

## STEP 2 — Market Data Loaders (Barchart CSV + yfinance + FRED)

**Note on data already on disk:**
Kirk downloaded the following from Barchart.com before the API question was resolved.
These files are in `strategy-lab/data/raw/` and are directly usable:

| File | What it is | Date range |
|------|-----------|-----------|
| `spx_daily/spx_daily_historical-data-download-06-07-2026.csv` | SPX daily OHLCV | Jan 2, 2020 – Jun 5, 2026 |
| `futures/esh20_intraday-1min_historical-data-download-06-07-2026.csv` | ES March 2020 contract, 1-min | Feb 27 – Mar 20, 2020 |
| `futures/esh21_...` through `esh26_...` | ES March contracts 2021–2026, 1-min | ~3 weeks before each March expiry |
| `vix/VIX_History.csv` | CBOE VIX daily | 1990 – Jun 2026 |

Barchart CSV column format: `Time, Open, High, Low, Latest, Change, %Change, Volume`
- "Latest" = close price
- Timestamps are in Central Time (CT), quoted strings: `"2020-03-20 08:29"`
- Last row of each file is a footer: `"Downloaded from Barchart.com as of..."` — must be stripped

**Data strategy:**
- SPX daily: Barchart CSV (primary, already on disk) — no yfinance fetch needed
- ES 1-min intraday: Barchart CSV for dates covered (~105 trading days, Feb/Mar each year)
- SPY hourly: yfinance for all other dates (~75% of trading days)
- VIX: Barchart/CBOE CSV (already on disk)
- Risk-free rate: FRED via pandas-datareader

**Prompt for Claude Code:**
```
We are building two modules for InfiniteLoop Phase 1 market data:
  strategy-lab/data/barchart_csv.py   — reads the CSV files already on disk
  strategy-lab/data/market_data.py    — orchestrates all data sources

--- FILE 1: strategy-lab/data/barchart_csv.py ---

MODULE DOCSTRING: "Loaders for Barchart.com CSV exports already downloaded to
strategy-lab/data/raw/. Handles the Barchart column naming convention (Latest=close),
quoted timestamps, Central Time localization, and footer stripping."

RAW_DIR constant: Path(__file__).parent / 'raw'

FUNCTION: load_spx_daily(csv_path: Path | None = None) -> pd.DataFrame
  - Default path: RAW_DIR / 'spx_daily' / (first *.csv found in that dir)
  - Read CSV, skip last row if it starts with '"Downloaded'
  - Rename columns: Time→date, Open→open, High→high, Low→low, Latest→close, Volume→volume
  - Drop Change, %Change columns
  - Parse date as datetime.date (format: %Y-%m-%d)
  - Set date as index, sort ascending
  - Drop rows with NaN in open/high/low/close
  - Log: rows loaded, date range
  - Returns: DataFrame with DatetimeIndex, columns: open, high, low, close, volume

FUNCTION: load_es_1min_csv(csv_path: Path) -> pd.DataFrame
  - Read one ES futures CSV file (e.g., esh24_intraday-1min_...)
  - Skip last row if it starts with '"Downloaded'
  - Rename columns: Time→timestamp, Open→open, High→high, Low→low, Latest→close, Volume→volume
  - Parse timestamp: strip quotes, format '%Y-%m-%d %H:%M', localize as US/Central
  - Sort ascending by timestamp
  - Drop rows with NaN OHLCV or volume == 0
  - Log: rows loaded, date range, contract name (from filename)
  - Returns: DataFrame with DatetimeIndex (tz-aware CT), columns: open, high, low, close, volume

FUNCTION: load_all_es_1min(futures_dir: Path | None = None) -> pd.DataFrame
  - Default dir: RAW_DIR / 'futures'
  - Find all *.csv files matching pattern esh??_*.csv
  - Call load_es_1min_csv() on each
  - Concatenate, sort ascending, drop exact duplicates
  - Log: total rows, date range, files loaded
  - Returns: single DataFrame covering all available ES 1-min windows

FUNCTION: get_es_covered_dates(futures_dir: Path | None = None) -> set[datetime.date]
  - Returns the set of dates (trading days) covered by the ES 1-min CSV files
  - Used by loader.py to decide which intraday source to use per day

FUNCTION: load_vix_history(csv_path: Path | None = None) -> pd.DataFrame
  - Default path: RAW_DIR / 'vix' / 'VIX_History.csv'
  - Expects columns: DATE, OPEN, HIGH, LOW, CLOSE
  - Parse DATE (format: %m/%d/%Y)
  - Rename CLOSE → vix_close
  - Sort ascending, set DatetimeIndex
  - Raises FileNotFoundError with download instructions if missing
  - Returns: DataFrame with DatetimeIndex, column 'vix_close'

Logger: 'infiniteloop.data.barchart_csv'
Full type hints and docstrings.

--- FILE 2: strategy-lab/data/market_data.py ---

MODULE DOCSTRING: "Market data orchestrator for InfiniteLoop Strategy Lab.
Routes data requests to the right source: Barchart CSVs (already on disk) for
SPX daily and ES 1-min windows, yfinance for SPY hourly on all other dates,
FRED for the risk-free rate. Caches via DataStore."

DATA_SOURCES comment block:
  # SPX daily:  Barchart CSV (data/raw/spx_daily/) — 2020–2026, already downloaded
  # ES 1-min:   Barchart CSV (data/raw/futures/) — Feb/Mar each year, already downloaded
  # SPY hourly: yfinance SPY 1h — fills gap dates not in ES CSV files
  # VIX:        CBOE CSV (data/raw/vix/VIX_History.csv) — already downloaded
  # Risk-free:  FRED DTB3 via pandas-datareader — auto-fetched, no key needed

CLASS: MarketDataClient
  __init__(self, store: DataStore | None = None) -> None
    - Load BarchartCSV loaders on init (call load_spx_daily, get_es_covered_dates,
      load_vix_history once and store as instance attributes)
    - self.spx_daily: pd.DataFrame
    - self.vix: pd.DataFrame
    - self.es_covered_dates: set[datetime.date]
    - self.es_1min: pd.DataFrame  (all ES CSV data, loaded lazily on first intraday call)
    - self.store = store

  METHOD: get_spx_daily(self, start_date: str, end_date: str) -> pd.DataFrame
    - Slice self.spx_daily to the requested range
    - If any dates in range are missing, log WARNING (gaps in downloaded data)
    - Returns sliced DataFrame

  METHOD: get_intraday_bars(
    self, date: datetime.date, bar_minutes: int = 60
  ) -> pd.DataFrame
    - If date in self.es_covered_dates: return ES 1-min bars for that date,
      resampled to bar_minutes if bar_minutes > 1
      (e.g., bar_minutes=60 → resample 1-min to hourly for consistent feature computation)
    - Else: fetch SPY hourly from yfinance for that date
      (use yfinance download with interval='1h', cache in store)
    - Returns DataFrame with columns: open, high, low, close, volume
    - Log which source was used (ES_CSV or SPY_HOURLY) at DEBUG level
    - This is the single entry point loader.py uses — it never needs to know which source

  METHOD: get_vix_on(self, date: datetime.date) -> float | None
    - Returns self.vix.loc[date, 'vix_close'] if available, else None

  METHOD: get_risk_free_rate(self, as_of_date: str | None = None) -> float
    - Fetches FRED series 'DTB3' (3-month T-bill, in percent)
    - Returns decimal (divide by 100). Fallback: 0.05 with WARNING if FRED unreachable
    - Cache in store for 24 hours

  METHOD: coverage_report(self) -> str
    - Returns a human-readable string summarizing data coverage:
        SPX daily: YYYY-MM-DD to YYYY-MM-DD (N rows)
        ES 1-min (Barchart): N trading days across ESH20–ESH26
        VIX: YYYY-MM-DD to YYYY-MM-DD (N rows)
    - Called at startup in loop.py to confirm data is in place

Logger: 'infiniteloop.data.market_data'
Full type hints and docstrings.
```

---

## STEP 3 — Data Loader & Direction Features

**Prompt for Claude Code:**
```
We are building strategy-lab/data/loader.py and strategy-lab/data/indicators.py.

--- FILE 1: strategy-lab/data/loader.py ---

MODULE DOCSTRING: "Loads SPY hourly OHLCV data via MarketDataClient and assembles
per-day feature rows for the direction classification model. Each row represents
one trading day with morning session features as columns."

CLASS: DayFeatureRow (dataclass):
  date: pd.Timestamp
  # Previous day features
  prev_range_pct: float        # (prev_high - prev_low) / prev_close
  prev_close_vs_vwap: float    # (prev_close - prev_vwap) / prev_vwap
  prev_final_hour_delta: float # cumulative delta proxy in final hour of prev RTH
  prev_volume_ratio: float     # prev day volume / 20-day avg volume
  # Overnight features
  gap_pct: float               # (RTH open - prev close) / prev close (signed)
  overnight_range_pct: float   # (overnight_high - overnight_low) / prev close
  open_vs_overnight_pct: float # where RTH open falls in overnight range: 0=low, 1=high
  # First N-minute RTH features
  orb_high: float              # opening range high (first ORB_MINUTES bars)
  orb_low: float               # opening range low
  orb_range_pct: float         # (orb_high - orb_low) / orb_low
  orb_breakout: str            # 'up', 'down', 'none' — at time of classification
  delta_bias_first30: float    # cumulative delta in first 30 min (positive=buying)
  vwap_at_30: float            # VWAP value at 30-min mark
  rel_volume_first30: float    # volume in first 30 min vs avg for same time window
  # Label (set after the fact for backtesting)
  day_outcome: str             # 'UP', 'DOWN', 'NEUTRAL' — determined by EOD price

FUNCTION: load_day_features(
  client: MarketDataClient,
  start_date: str,
  end_date: str,
  orb_minutes: int = ORB_MINUTES
) -> pd.DataFrame
  - Fetches SPX daily bars for gap/outcome labels via client.get_spx_daily()
  - For each trading day, fetches intraday bars via client.get_intraday_bars(date)
    which automatically routes to ES 1-min CSV (if that date is covered) or SPY hourly (otherwise)
  - Logs a coverage summary at startup: "X days from ES CSV, Y days from SPY hourly"
  - For each trading day:
    1. Split bars into: previous day RTH, overnight session, current day RTH
    2. Compute previous day features
    3. Compute overnight features (gap, overnight range, open position)
    4. Compute first-N-minute RTH features (ORB, delta, VWAP, volume)
    5. Compute day_outcome: if EOD close >= RTH open + 0.3% → 'UP',
       if EOD close <= RTH open - 0.3% → 'DOWN', else → 'NEUTRAL'
  - Return a DataFrame with one row per day, columns = DayFeatureRow fields
  - Log: total days processed, UP/DOWN/NEUTRAL distribution

FUNCTION: split_train_oos(df: pd.DataFrame, oos_split: float = OOS_SPLIT)
  -> tuple[pd.DataFrame, pd.DataFrame]
  - Same as before: first 80% train, last 20% OOS
  - OOS is NEVER used during the Hermes optimization loop

--- FILE 2: strategy-lab/data/indicators.py ---

MODULE DOCSTRING: "ES order flow indicators for InfiniteLoop direction classification.
Computes ORB, gap, delta proxy, VWAP, and session-boundary features from 1-min bars."

FUNCTION: delta_proxy(df: pd.DataFrame) -> pd.Series
  - Same as original: (close >= open) → +(close-low)/(high-low+1e-9)*volume
                      (close < open) → -(high-close)/(high-low+1e-9)*volume
  - Returns Series named 'delta'

FUNCTION: cumulative_delta(df: pd.DataFrame, window: int = 20) -> pd.Series
  - Rolling sum of delta_proxy over window bars. Name: 'cum_delta'

FUNCTION: vwap_daily(df: pd.DataFrame) -> pd.Series
  - VWAP that resets at midnight each day
  - Formula: cumsum(typical_price * volume) / cumsum(volume), grouped by date
  - Name: 'vwap'

FUNCTION: opening_range(df: pd.DataFrame, n_minutes: int = 30) -> tuple[pd.Series, pd.Series]
  - For each day, compute the high and low of the first n_minutes bars of RTH (09:30–10:00)
  - Returns (orb_high, orb_low) as two Series indexed by day date

FUNCTION: relative_volume(df: pd.DataFrame, window: int = 20) -> pd.Series
  - volume / rolling mean volume over window. Name: 'rel_vol'

FUNCTION: compute_gap_pct(prev_close: float, current_open: float) -> float
  - Returns (current_open - prev_close) / prev_close (signed, as decimal)

All functions: full type hints, docstrings. Logger: 'infiniteloop.data.indicators'
```

---

## STEP 3B — Event Calendar Filter & Volatility Regime Gate

**Prompt for Claude Code:**
```
We are building two new modules for InfiniteLoop Phase 1 strategy filtering:
  strategy-lab/data/events.py
  strategy-lab/data/vol_regime.py

--- FILE 1: strategy-lab/data/events.py ---

MODULE DOCSTRING: "Economic event calendar for InfiniteLoop. Identifies macro event days
where the direction classifier is unreliable (FOMC, CPI, NFP, PCE). On these days the
system skips trading regardless of the morning signal. Updated annually."

CONSTANT: FOMC_DATES (set of datetime.date)
  Add all 8 FOMC meeting announcement dates for 2025 and 2026.
  Source: federalreserve.gov/monetarypolicy/fomccalendars.htm

CONSTANT: CPI_DATES (set of datetime.date)
  Add all monthly CPI release dates for 2025 and 2026.
  Source: bls.gov/schedule

CONSTANT: NFP_DATES (set of datetime.date)
  Add all monthly Nonfarm Payroll release dates (first Friday of each month) for 2025 and 2026.
  Source: bls.gov/schedule

CONSTANT: PCE_DATES (set of datetime.date)
  Add all monthly PCE/Core PCE release dates for 2025 and 2026.
  Source: bea.gov/news/schedule

CONSTANT: ALL_EVENT_DATES = FOMC_DATES | CPI_DATES | NFP_DATES | PCE_DATES

FUNCTION: is_event_day(date: datetime.date) -> bool
  Returns True if date is in ALL_EVENT_DATES.

FUNCTION: is_post_event_day(date: datetime.date) -> bool
  Returns True if the previous trading day (Mon–Fri) was an event day.
  Markets are often in "digestion mode" the day after major events.
  Use a simple calendar offset — does not need to be perfect.

FUNCTION: get_event_name(date: datetime.date) -> str
  Returns the event type as a string ('FOMC', 'CPI', 'NFP', 'PCE', 'POST_EVENT', or '').
  Used for logging and trade_logger.exit_reason.

Logger: 'infiniteloop.data.events'

--- FILE 2: strategy-lab/data/vol_regime.py ---

MODULE DOCSTRING: "Volatility regime gate for InfiniteLoop. Provides per-day VIX and
IV rank data. Used as a pre-classifier gate: only trade when the vol regime is favorable
for premium selling (IV rich enough, VIX not extreme)."

CLASS: VolRegimeData
  __init__(self, vix_df: pd.DataFrame) -> None
    - vix_df: DataFrame indexed by date with 'vix_close' column
      (output of MarketDataClient.get_vix_history())
    - Computes a rolling IV rank from VIX itself:
        vix_rank = percentile of today's VIX vs trailing 252-day window
      This is a VIX-rank proxy — not the same as SPX IV rank, but directionally equivalent
      for the gate's purpose (skip when premium is too thin or vol is spiking)

  METHOD: get_regime(self, date: pd.Timestamp) -> dict
    Returns dict with keys: vix, vix_rank_pct (0–100 rolling percentile)
    Returns None if data not available for date (log WARNING)

  METHOD: is_favorable(
    self, date: pd.Timestamp,
    min_vix_rank: float = DEFAULT_MIN_IV_RANK_PCT,  # reusing constant name; same semantic
    max_vix: float = DEFAULT_MAX_VIX
  ) -> tuple[bool, str]
    Returns (True, '') if regime is favorable for selling.
    Returns (False, reason_str) if not — reason: 'low_vix_rank' | 'high_vix' | 'no_data'
    Skip if vix_rank_pct < min_vix_rank (VIX is low relative to history → thin premium)
    Skip if vix > max_vix (vol spike → wider realized range)

FUNCTION: load_vix_history(vix_csv_path: str) -> pd.DataFrame
  Loads VIX daily closing prices from the manually-downloaded CBOE CSV.
  File: strategy-lab/data/raw/vix/VIX_History.csv
  Returns DataFrame with DatetimeIndex, column 'vix_close'.
  Raises FileNotFoundError with download instructions if missing.
  Cache to SQLite via DataStore if available.

Logger: 'infiniteloop.data.vol_regime'

--- HOW THESE PLUG IN ---
In backtest/engine.py, before processing any day in the features loop:
  1. If events.is_event_day(date): mark as 'SKIP' (reason='event_day'), continue
  2. If not vol_regime.is_favorable(date)[0]: mark as 'SKIP' (reason from is_favorable), continue
  3. Otherwise: run strategy.classify_direction()

In layer 2 (Layer 2 will handle this — note for now, don't implement Layer 2 yet):
  Same gate runs in classifier.py before the morning classification is emitted.
```

---

## STEP 4 — Options Pricing & Spread P&L (Synthetic)

**Prompt for Claude Code:**
```
We are building strategy-lab/data/options.py for InfiniteLoop Phase 1.

MODULE DOCSTRING: "Synthetic options pricing and spread P&L calculator for InfiniteLoop.
Uses Black-Scholes (py_vollib) with VIX as the implied volatility proxy.
No paid options chain data required — all pricing is computed locally."

IMPORTS: py_vollib.black_scholes, py_vollib.black_scholes_greeks.analytical
         pandas_datareader for risk-free rate (imported lazily — fall back gracefully)

CONSTANTS at module level:
  VIX_0DTE_SCALING = 1.15   # 0DTE IV is typically 10–20% above the 30-day VIX
  MIN_CREDIT_THRESHOLD = 0.10  # skip spread if credit < $0.10 (not worth the risk)

DATACLASS: SpreadResult
  date: pd.Timestamp
  trade_type: str           # 'bull_put_spread' | 'bear_call_spread' | 'iron_condor' | 'skipped'
  direction_signal: str     # 'UP' | 'DOWN' | 'NEUTRAL'
  direction_correct: bool
  short_strike: float | None
  long_strike: float | None
  credit_received: float    # per-unit credit collected at entry
  max_loss: float           # spread_width - credit_received (per unit)
  exit_price: float         # cost to close at exit time
  pnl_per_contract: float   # (credit - exit_price) * SPX_MULTIPLIER
  exit_reason: str          # 'profit_target' | 'stop_loss' | 'forced_exit' | 'skipped'

FUNCTION: sigma_from_vix(vix: float, scaling: float = VIX_0DTE_SCALING) -> float
  """Convert VIX level to annualized sigma for Black-Scholes.
  VIX is in percent — divide by 100 and scale up for 0DTE."""
  return (vix / 100.0) * scaling

FUNCTION: time_to_expiry(entry_hour: int, entry_minute: int = 0,
                          market_close_hour: int = 16) -> float
  """Return fraction of year remaining from entry time to 4:00 PM ET close.
  SPX 0DTE expires at market close."""
  hours_remaining = (market_close_hour - entry_hour) - (entry_minute / 60.0)
  return max(hours_remaining, 0.0) / 8760.0

FUNCTION: find_strike_by_delta(
  spot: float,
  option_type: str,      # 'p' (put) or 'c' (call)
  target_delta: float,   # e.g. 0.15 for 15-delta put
  sigma: float,
  t: float,              # time to expiry in years
  r: float               # risk-free rate
) -> float
  - Binary search for strike where abs(BS_delta) ≈ target_delta
  - Search range: spot ± 20% in 0.25-point steps
  - Uses py_vollib black_scholes_greeks.analytical.delta()
  - Returns the closest strike
  - Raises ValueError if target delta cannot be found in search range

FUNCTION: price_spread(
  spot: float,
  short_strike: float,
  long_strike: float,
  option_type: str,   # 'p' or 'c'
  sigma: float,
  t: float,
  r: float
) -> float
  """Price a vertical spread as short_price - long_price.
  For puts: short put - long put (credit received)
  For calls: short call - long call (credit received)"""
  from py_vollib.black_scholes import black_scholes
  short_price = black_scholes(option_type, spot, short_strike, t, r, sigma)
  long_price  = black_scholes(option_type, spot, long_strike, t, r, sigma)
  return short_price - long_price

FUNCTION: simulate_spread(
  date: pd.Timestamp,
  direction: str,           # 'UP', 'DOWN', 'NEUTRAL', 'SKIP'
  spot_price: float,        # SPX price at entry (from spx_daily open)
  vix: float,               # VIX close on that date
  spread_params: dict,      # from strategy.get_spread_params()
  risk_free_rate: float = 0.05,
  direction_correct: bool = False
) -> SpreadResult
  - If direction == 'SKIP': return skipped SpreadResult immediately
  - Compute sigma = sigma_from_vix(vix)
  - Compute t_entry = time_to_expiry(spread_params['entry_hour'])
  - Select option_type: 'p' for UP (bull put), 'c' for DOWN (bear call)
  - Find short_strike via find_strike_by_delta(target_delta=spread_params['short_delta']/100)
  - Compute long_strike = short_strike - spread_params['spread_width_usd'] (puts)
                        or short_strike + spread_params['spread_width_usd'] (calls)
  - Compute credit = price_spread(..., t=t_entry)
  - If credit < MIN_CREDIT_THRESHOLD: return skipped SpreadResult
  - Simulate exit rules using re-priced spread at later times:
      * Compute t_profit_check = time_to_expiry(13)  # 1:00 PM ET check
      * Compute t_stop_check   = time_to_expiry(13)  # same time — check both at once
      * Compute t_forced_exit  = time_to_expiry(15, 45)
      * If at t_profit_check: price ≤ credit * (1 - profit_target_pct/100):
          exit_price = credit * (1 - profit_target_pct/100), reason='profit_target'
      * Elif price ≥ credit * (1 + stop_loss_pct/100):
          exit_price = credit * (1 + stop_loss_pct/100), reason='stop_loss'
      * Else: re-price at t_forced_exit, exit_price = that price, reason='forced_exit'
  - pnl_per_contract = (credit - exit_price) * SPX_MULTIPLIER
  - Return SpreadResult

FUNCTION: simulate_iron_condor(
  date: pd.Timestamp,
  spot_price: float, vix: float,
  spread_params: dict, risk_free_rate: float = 0.05,
  direction_correct: bool = True
) -> SpreadResult
  - Sell bull put spread AND bear call spread simultaneously
  - Put side: short at target_delta puts, long at short_strike - spread_width
  - Call side: short at target_delta calls, long at short_strike + spread_width
  - Total credit = put_credit + call_credit
  - Max loss = spread_width - min(put_credit, call_credit)  [conservative]
  - Exit logic: same as simulate_spread but applied to combined position value
  - Return combined SpreadResult (trade_type='iron_condor')

Logger: 'infiniteloop.data.options'
Full type hints and docstrings.
```

---

## STEP 5 — SQLite Cache

**Prompt for Claude Code:**
```
We are building strategy-lab/data/store.py for InfiniteLoop Phase 1.

MODULE DOCSTRING: "Local SQLite cache for SPX/SPY OHLCV, VIX data, day features, and
synthetic pricing inputs. Avoids repeat yfinance/FRED network calls on every backtest run."

CLASS: DataStore
  __init__(self, db_path: str | Path) -> None
    - Create SQLite file if not exists
    - Create tables if not exists:
        TABLE futures_cache: symbol TEXT, date TEXT, tf TEXT, data BLOB, cached_at TEXT
          UNIQUE(symbol, date, tf)
        TABLE options_cache: symbol TEXT, date TEXT, data BLOB, cached_at TEXT
          UNIQUE(symbol, date)
        TABLE features_cache: symbol TEXT, start_date TEXT, end_date TEXT,
          data BLOB, cached_at TEXT, UNIQUE(symbol, start_date, end_date)

  METHOD: save_futures(self, symbol: str, date: str, tf: str, df: pd.DataFrame) -> None
  METHOD: load_futures(self, symbol: str, date: str, tf: str) -> pd.DataFrame | None
  METHOD: save_options(self, symbol: str, date: str, df: pd.DataFrame) -> None
  METHOD: load_options(self, symbol: str, date: str) -> pd.DataFrame | None
  METHOD: save_features(self, symbol: str, start_date: str, end_date: str, df: pd.DataFrame) -> None
  METHOD: load_features(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame | None

Use pickle for DataFrame serialization (BLOB column). Use parameterized queries.
Logger: 'infiniteloop.data.store'
```

---

## STEP 6 — Backtest Engines

**Prompt for Claude Code:**
```
We are building strategy-lab/backtest/engine.py and spread_engine.py.

--- FILE 1: strategy-lab/backtest/engine.py ---

MODULE DOCSTRING: "Direction model backtester. Given a set of day-feature rows and
a BaseStrategy, computes direction accuracy metrics. This is separate from spread P&L —
first we measure how well the classifier works, then spread_engine applies the spreads."

FUNCTION: backtest_direction(
  features_df: pd.DataFrame,   # output of load_day_features()
  strategy                     # any object with classify_direction(row) -> str
) -> pd.DataFrame
  - For each row in features_df, call strategy.classify_direction(row)
  - Compare prediction to row['day_outcome']
  - Return a DataFrame with columns:
      date, predicted, actual, correct (bool)
  - This is the core of the direction model evaluation

FUNCTION: direction_accuracy(results_df: pd.DataFrame) -> dict
  - Computes overall accuracy, per-class accuracy (UP/DOWN/NEUTRAL), skip rate
  - Returns dict with: accuracy, up_accuracy, down_accuracy, neutral_accuracy, skip_rate

--- FILE 2: strategy-lab/backtest/spread_engine.py ---

MODULE DOCSTRING: "Spread P&L backtester for InfiniteLoop Phase 1. Given direction results
and a strategy's spread params, simulates each day's spread trade using synthetic
Black-Scholes pricing (VIX as IV proxy). Returns a trade-by-trade P&L DataFrame."

FUNCTION: backtest_spreads(
  direction_results: pd.DataFrame,   # output of backtest_direction()
  spx_daily: pd.DataFrame,           # SPX daily OHLCV from MarketDataClient.get_spx_daily()
  vix_df: pd.DataFrame,              # VIX daily from MarketDataClient.get_vix_history()
  spread_params: dict,               # from strategy.get_spread_params()
  risk_free_rate: float = 0.05       # from MarketDataClient.get_risk_free_rate()
) -> pd.DataFrame
  - For each day in direction_results:
      - Look up spot_price = spx_daily.loc[date, 'open']
      - Look up vix = vix_df.loc[date, 'vix_close']
      - If predicted == 'NEUTRAL' → simulate_iron_condor(...)
      - If predicted == 'UP' → simulate_spread(..., direction='UP')
      - If predicted == 'DOWN' → simulate_spread(..., direction='DOWN')
      - If predicted == 'SKIP' → log as skipped trade
      - If spot or vix not available for date: log WARNING, treat as 'SKIP'
  - Returns DataFrame with one SpreadResult per row
  - Converts SpreadResult fields to DataFrame columns

Logger: 'infiniteloop.backtest.engine', 'infiniteloop.backtest.spread_engine'
```

---

## STEP 7 — Backtest Metrics & Scorecard

**Prompt for Claude Code:**
```
We are building strategy-lab/backtest/metrics.py for InfiniteLoop Phase 1.

MODULE DOCSTRING: "Scorecard for 0DTE spread strategy performance. Combines
direction model accuracy with spread P&L metrics into a single evaluation object."

DATACLASS: StrategyScorecard
  # Direction metrics
  total_days: int
  direction_accuracy: float        # 0.0 to 1.0
  up_accuracy: float
  down_accuracy: float
  neutral_accuracy: float
  skip_rate: float                 # fraction of days skipped (low confidence)

  # Spread P&L metrics
  total_trades: int
  win_rate: float
  profit_factor: float             # gross_profit / gross_loss
  sharpe_ratio: float
  max_drawdown_pct: float
  total_return_pct: float
  expectancy_dollars: float        # average P&L per trade day
  avg_win_dollars: float
  avg_loss_dollars: float

  # Validation result
  passed: bool = False             # True only if ALL minimums met

  METHOD: to_dict(self) -> dict
  METHOD: summary_str(self) -> str

FUNCTION: score_results(
  direction_results: pd.DataFrame,
  spread_results: pd.DataFrame,
  initial_equity: float = 5000.0
) -> StrategyScorecard
  - Computes direction accuracy metrics from direction_results
  - Computes spread P&L metrics from spread_results
  - Builds cumulative P&L curve for Sharpe and drawdown calculation
  - Sets passed = True ONLY IF ALL of:
      direction_accuracy >= MIN_DIRECTION_ACCURACY (from constants)
      total_trades >= MIN_TRADE_COUNT
      profit_factor >= MIN_PROFIT_FACTOR
      sharpe_ratio >= MIN_SHARPE
      max_drawdown_pct <= MAX_DRAWDOWN_PCT
  - Logs summary_str() at INFO level
  - Returns StrategyScorecard

Logger: 'infiniteloop.backtest.metrics'
```

---

## STEP 7B — Results Dashboard

**Prompt for Claude Code:**
```
We are building strategy-lab/dashboard.py for InfiniteLoop Phase 1.

MODULE DOCSTRING: "Rich terminal dashboard for InfiniteLoop backtest results.
Called after every backtest run to display scorecard, trade table, equity curve,
and Hermes iteration history. Also exports an HTML snapshot for archiving."

Use the `rich` library for terminal rendering.

FUNCTION: render(
  scorecard: StrategyScorecard,
  spread_results: pd.DataFrame,
  direction_results: pd.DataFrame,
  strategy_params: dict,
  iteration: int,
  tier: int,
  history: list[dict]           # list of {'params': ..., 'scorecard': ..., 'tier': ...}
) -> None
  Renders the following panels to the terminal:

  PANEL 1 — Strategy Header
    - Strategy name, iteration number, tier reached (1/2/3)
    - Promotion status: "⏩ Promoted to Tier 2" | "⏩ Promoted to Tier 3" | "✅ Tier 3 PASSED" | "❌ Failed Tier N"
    - Current parameter dict (formatted as a table: param | current value | last change)

  PANEL 2 — Scorecard Table
    Columns: Metric | Value | Threshold | Status (✅/❌)
    Rows:
      Direction Accuracy | 0.XX | > 55% | ✅/❌
      Win Rate           | 0.XX | > 50% | (informational)
      Profit Factor      | X.XX | > 1.5 | ✅/❌
      Sharpe Ratio       | X.XX | > 0.8 | ✅/❌
      Max Drawdown       | -XX% | < 20% | ✅/❌
      Total Trades       | XXX  | ≥ 200  | ✅/❌
      Expectancy         | $XX  | > 0   | ✅/❌
      Avg Win / Avg Loss | $XX / $XX | — | (informational)
      Skip Rate          | XX%  | —     | (informational)

  PANEL 3 — Last 10 Trades Table
    Columns: Date | Type | Direction | Correct? | Credit | Exit $ | P&L | Reason
    Color-code rows: green = profit, red = loss, yellow = skipped

  PANEL 4 — Hermes Iteration History (last 5)
    Columns: Iter | Tier | Changed | Old → New | Sharpe Δ | PF Δ

  PANEL 5 — Equity Curve
    ASCII sparkline of cumulative P&L over the backtest period.
    Use the `rich` Sparkline or a simple text plot if unavailable.

FUNCTION: export_html(
  scorecard: StrategyScorecard,
  spread_results: pd.DataFrame,
  history: list[dict],
  output_path: str
) -> None
  Exports a standalone HTML file with:
  - Same scorecard table (as HTML table)
  - Equity curve as a matplotlib PNG embedded as base64
  - Full trade history as a scrollable table
  - Saved to: strategy-lab/logs/dashboard_{timestamp}.html

Dashboard is called in loop.py after every tier evaluation, not just at the end.
When running Tier 1, show a condensed version (scorecard + status only).
Full dashboard renders at Tier 2 and Tier 3.

Logger: 'infiniteloop.dashboard'
```

---

## STEP 8 — Validator (OOS + Walk-Forward)

**Prompt for Claude Code:**
```
We are building strategy-lab/backtest/validator.py for InfiniteLoop Phase 1.

MODULE DOCSTRING: "Out-of-sample and walk-forward validation for 0DTE spread strategies.
The OOS set is the final arbiter — if a strategy doesn't perform OOS, it is rejected."

DATACLASS: ValidationResult
  strategy_name: str
  in_sample_scorecard: StrategyScorecard
  oos_scorecard: StrategyScorecard
  walk_forward_scorecards: list[StrategyScorecard]
  oos_degradation_pct: float          # (IS_accuracy - OOS_accuracy) / IS_accuracy
  walk_forward_consistency: float     # fraction of WF folds that passed
  final_verdict: bool
  rejection_reason: str = ''

  METHOD: summary_str(self) -> str

CLASS: Validator
  __init__(self, direction_engine, spread_engine,
           spx_daily: pd.DataFrame, vix_df: pd.DataFrame,
           risk_free_rate: float = 0.05) -> None

  METHOD: validate(
    self,
    train_df: pd.DataFrame,    # IS day features (first 80%)
    oos_df: pd.DataFrame,      # OOS day features (last 20%) — never seen during optimization
    strategy,
    strategy_name: str
  ) -> ValidationResult

  LOGIC:
    1. Run direction + spread backtest on train_df → in_sample_scorecard
    2. Run direction + spread backtest on oos_df → oos_scorecard
    3. Walk-forward validation on train_df (WALK_FORWARD_FOLDS folds)
    4. oos_degradation_pct = (IS_accuracy - OOS_accuracy) / (IS_accuracy + 1e-9)
    5. walk_forward_consistency = fraction of WF scorecards where passed == True
    6. final_verdict = True ONLY IF:
         oos_scorecard.passed == True
         oos_degradation_pct < 0.30  (accuracy drops less than 30% OOS)
         walk_forward_consistency >= 0.60
    7. Populate rejection_reason if final_verdict is False
    8. Return ValidationResult

Logger: 'infiniteloop.backtest.validator'
```

---

## STEP 9 — Strategy Base Class & Registry

**Prompt for Claude Code:**
```
We are building the strategy layer for InfiniteLoop Phase 1.

--- FILE 1: strategy-lab/strategy/base.py ---

MODULE DOCSTRING: "Abstract base class for all InfiniteLoop 0DTE spread strategies.
Implements the classify_direction + get_spread_params interface."

from abc import ABC, abstractmethod
import pandas as pd
from dataclasses import dataclass

@dataclass
class StrategyMetadata:
  name: str
  version: int
  description: str
  created_at: str     # ISO timestamp
  params: dict        # full param dict (direction + spread)
  scorecard: dict     # StrategyScorecard.to_dict() after validation

class BaseStrategy(ABC):
  Implement this interface exactly as specified in CLAUDE.md:

  @abstractmethod
  def classify_direction(self, data: pd.DataFrame) -> str:
    """Return 'UP', 'DOWN', 'NEUTRAL', or 'SKIP'.
    data: a single day's feature row (from DayFeatureRow) as a 1-row DataFrame or Series."""
    ...

  @abstractmethod
  def get_spread_params(self) -> dict:
    """Return spread parameters:
    entry_hour, short_delta, spread_width_usd, profit_target_pct,
    stop_loss_pct, forced_exit_hour."""
    ...

  @abstractmethod
  def get_params(self) -> dict:
    """Return full parameter dict (direction + spread params combined)."""
    ...

  @abstractmethod
  def get_name(self) -> str:
    ...

--- FILE 2: strategy-lab/strategy/registry.py ---

Same structure as original but with updated trades table schema for options.
(See ARCHITECTURE.md section 4 for the full CREATE TABLE statements — use those exactly.)

The initialize_schema() method must create all 4 tables: strategies, trades,
equity_snapshots, portfolio_events — using the options-aware schema from ARCHITECTURE.md.

--- FILE 3: strategy-lab/strategy/packager.py ---

Same structure as original design. pack_strategy() calls:
  strategy.get_name(), strategy.get_params(), strategy.get_spread_params()

Logger names: 'infiniteloop.strategy.registry', 'infiniteloop.strategy.packager'
```

---

## STEP 10 — Hermes AI Client

**Prompt for Claude Code:**
```
We are building the Hermes AI loop components for InfiniteLoop Phase 1.
This is largely the same as the original design but with updated prompts for
the 0DTE direction + spread optimization problem.

--- FILE 1: strategy-lab/hermes/client.py ---
(Identical to original design — HTTP client for Ollama, generate(), generate_json(),
is_available(). No changes needed from original spec.)

--- FILE 2: strategy-lab/hermes/prompts.py ---

MODULE DOCSTRING: "Prompt templates for the Hermes 0DTE strategy discovery loop.
Two classes of parameters are optimized: direction thresholds and spread parameters.
All prompts instruct Hermes to respond in valid JSON."

FUNCTION: build_initial_prompt(strategy_type: str, params: dict) -> str
  Returns a prompt that:
  - Explains InfiniteLoop: a 0DTE options system that classifies market direction
    (UP/DOWN/NEUTRAL) each morning using ES futures orderflow + ORB, then sells
    the appropriate vertical spread or iron condor on SPX
  - Shows the current parameter dict (direction params + spread params)
  - Asks Hermes to suggest starting parameter values
  - JSON schema: {"reasoning": "...", "suggested_params": {...}, "change_summary": "..."}
  - Emphasizes: ONE parameter at a time. Order flow focus (gap, ORB, delta, VWAP).

FUNCTION: build_iteration_prompt(params: dict, history: list[dict], best_scorecard: dict) -> str
  - history: list of {"params": {...}, "scorecard": {...}} dicts
  - Returns prompt asking Hermes to suggest ONE parameter change to improve the strategy
  - Shows: current params, recent history, best scorecard
  - Explains both categories of params Hermes can change:
    Direction params: gap_threshold_pct, orb_breakout_pct, delta_bias_threshold, neutral_band_pct
    Spread params: entry_hour, short_delta, spread_width_usd, profit_target_pct, stop_loss_pct
  - Explicitly states: DO NOT change forced_exit_hour, max_loss_pct, daily_halt_pct
  - JSON schema: {"reasoning": "...", "param_to_change": "name", "new_value": <value>, "change_summary": "..."}

FUNCTION: build_convergence_check_prompt(history: list[dict]) -> str
  - Same structure as original
  - JSON schema: {"converged": bool, "reasoning": "...", "suggestion": "..."}

--- FILE 3: strategy-lab/hermes/parser.py ---

Same structure as original. ALLOWED_PARAMS list (module level):
  ['gap_threshold_pct', 'orb_breakout_pct', 'delta_bias_threshold', 'neutral_band_pct',
   'entry_hour', 'short_delta', 'spread_width_usd', 'profit_target_pct', 'stop_loss_pct']

NEVER_CHANGE list (validated by parser, raises ValueError if attempted):
  ['forced_exit_hour', 'max_loss_pct', 'daily_halt_pct']
```

---

## STEP 11 — Seed Strategy (ORB Direction)

**Prompt for Claude Code:**
```
We are building strategy-lab/strategy/orb_direction.py — the seed strategy
for the Hermes optimization loop.

MODULE DOCSTRING: "ORB Direction Strategy — the seed 0DTE strategy for InfiniteLoop.
Uses the Opening Range Breakout (first 30 min of RTH) as the primary directional signal,
filtered by overnight gap and ES delta bias. Classifies each day as UP, DOWN, or NEUTRAL,
then defines the spread structure for that day's trade."

DATACLASS: ORBDirectionParams
  # Direction parameters (Hermes optimizes these)
  gap_threshold_pct: float = 0.25     # gap > 0.25% → strong directional bias
  orb_breakout_pct: float = 0.12      # ORB breakout > 0.12% of price is significant
  delta_bias_threshold: float = 150.0 # cumulative delta in first 30 min (absolute value)
  neutral_band_pct: float = 0.20      # gap within ±0.20% → lean neutral

  # Spread parameters (Hermes optimizes these)
  entry_hour: int = 10                # enter spread between 10:00–10:59 AM ET
  short_delta: int = 20               # sell the ~20-delta strike
  spread_width_usd: int = 5           # $5-wide spread
  profit_target_pct: int = 50         # close at 50% of max credit
  stop_loss_pct: int = 200            # close if loss = 2× credit

  # Risk — NEVER changed by Hermes
  forced_exit_hour: int = 15          # 3:00 PM ET (watchdog exits at 3:45)

CLASS: ORBDirectionStrategy(BaseStrategy)
  __init__(self, params: ORBDirectionParams = ORBDirectionParams()) -> None

  get_name() -> str: return 'orb_direction'

  get_params() -> dict: return dataclasses.asdict(self.params)

  get_spread_params() -> dict:
    return {
      'entry_hour': self.params.entry_hour,
      'short_delta': self.params.short_delta,
      'spread_width_usd': self.params.spread_width_usd,
      'profit_target_pct': self.params.profit_target_pct,
      'stop_loss_pct': self.params.stop_loss_pct,
      'forced_exit_hour': self.params.forced_exit_hour,
    }

  classify_direction(self, data: pd.DataFrame | pd.Series) -> str:
    LOGIC (in priority order):
    1. If abs(gap_pct) > gap_threshold_pct:
         - gap is significant → use gap direction as bias
         - UP bias if gap_pct > 0, DOWN bias if gap_pct < 0
         - Confirm with ORB: if price broke ORB in same direction → return that direction
         - If ORB contradicts gap → return 'NEUTRAL'
    2. If abs(gap_pct) <= neutral_band_pct:
         - Small gap → lean neutral
         - Check delta_bias_first30: if abs(delta) > delta_bias_threshold → directional
         - Otherwise → 'NEUTRAL'
    3. Medium gap (between neutral_band and gap_threshold):
         - Check ORB breakout direction
         - If broke up with positive delta → 'UP'
         - If broke down with negative delta → 'DOWN'
         - Otherwise → 'NEUTRAL'
    4. Always return one of: 'UP', 'DOWN', 'NEUTRAL', 'SKIP'
       Return 'SKIP' if required features are NaN (data unavailable)

  from_params(params_dict: dict) -> 'ORBDirectionStrategy': class method

ALLOWED_PARAMS (module level):
  ['gap_threshold_pct', 'orb_breakout_pct', 'delta_bias_threshold', 'neutral_band_pct',
   'entry_hour', 'short_delta', 'spread_width_usd', 'profit_target_pct', 'stop_loss_pct']
```

---

## ⏸️ MANUAL STOP 3 — Pre-Loop Checklist

**Claude Code: Pause before building the Hermes loop. This is the last checkpoint before the system runs autonomously. Confirm everything with Kirk.**

Run these checks and show Kirk the output:

```bash
# 1. Ollama is running and hermes3 is available
ollama list

# 2. All tests pass
cd strategy-lab && python -m pytest tests/ -v --tb=short

# 3. Data cache populated (VIX CSV present, SQLite cache has data)
ls -lh data/raw/vix/
python -c "from data.market_data import MarketDataClient; c = MarketDataClient(); df = c.get_spx_daily('2023-01-01', '2024-12-31'); print('SPX rows:', len(df))"
```

Tell Kirk:

> "Here's where we are before starting the Hermes discovery loop:
>
> - Tests: [pass/fail count]
> - Ollama: [hermes3 available yes/no]
> - Data: [SPX rows cached, VIX CSV present yes/no]
>
> If everything looks good, say 'go' and I'll build the main loop and run it.
> If anything needs attention, let me know and we'll fix it first."

Do not proceed to STEP 12 until Kirk says go.

---

## STEP 12 — Main Loop Orchestrator

**Prompt for Claude Code:**
```
We are building strategy-lab/loop.py — the main Hermes discovery loop for InfiniteLoop.

MODULE DOCSTRING: "Main 0DTE strategy discovery loop for InfiniteLoop Phase 1.
Orchestrates: load features → Hermes direction+spread optimization → backtest → score → iterate.
Run this script to discover and validate a new 0DTE spread strategy."

CONSTANTS:
  MAX_ITERATIONS = 50
  HISTORY_WINDOW = 10
  CONVERGENCE_CHECK_EVERY = 10

ARGPARSE arguments:
  --iterations: int, default MAX_ITERATIONS
  --seed-params: str (JSON override of default params), optional
  --strategy: str, default 'orb_direction'
  --dry-run: flag, do not save to registry

FUNCTION: run_loop(args) -> None:

  LOGIC:
  1. Load env, check Hermes availability
  2. Initialize MarketDataClient, DataStore
  3. Load or fetch day features (use cache if available)
  4. Split into train/OOS
  5. Fetch SPX daily, VIX history, and risk-free rate for backtest period
  6. Initialize ORBDirectionStrategy (or specified strategy)
  7. Initialize StrategyRegistry → initialize_schema()
  8. Initialize Validator

  9. HERMES LOOP — TIERED PROMOTION:
     Each iteration runs the fastest tier that can still disqualify the strategy.

     For each iteration:
       a. TIER 1 SCREEN (6-month window from train_df):
          - Run backtest_direction() + backtest_spreads() on 6-month slice
          - Score → StrategyScorecard
          - dashboard.render(..., tier=1) — condensed view
          - If Tier 1 FAILS (accuracy < 0.50 or PF < 1.20):
              log INFO "Tier 1 fail — adjusting params"
              ask Hermes for next param change, continue to next iteration
          - If Tier 1 PASSES: → promote to Tier 2

       b. TIER 2 VALIDATION (2-year window from train_df):
          - Run backtest_direction() + backtest_spreads() on 2-year slice
          - Score → StrategyScorecard
          - dashboard.render(..., tier=2) — full scorecard
          - If Tier 2 FAILS: log INFO, ask Hermes, continue
          - If Tier 2 PASSES: → promote to Tier 3

       c. TIER 3 FULL VALIDATION (full train_df — 4+ years):
          - Run backtest_direction() + backtest_spreads() on full train set
          - Score → StrategyScorecard
          - dashboard.render(..., tier=3) — full scorecard + equity curve
          - Track best_scorecard (by Sharpe), best_params
          - If Tier 3 PASSES: flag as candidate for OOS validation

       d. Every CONVERGENCE_CHECK_EVERY: ask Hermes convergence check
       e. Build prompt → generate_json() → parse → apply single param change
       f. Handle parser failures gracefully (log WARNING, skip iteration)

     NOTE: Most iterations spend only seconds in Tier 1. Only promising strategies
     do the full 4-year run. Tier 3 runs are rare — they indicate a real candidate.

  10. Restore best_params to strategy

  11. VALIDATION:
      result = validator.validate(train_df, oos_df, strategy, strategy.get_name())
      If final_verdict is False: log ERROR with rejection_reason, exit

  12. SAVE TO REGISTRY (unless --dry-run):
      Save and promote to active. Log success.

  13. Save full history to strategy-lab/logs/loop_history_{timestamp}.json

Logging: StreamHandler (INFO) + FileHandler ('strategy-lab/logs/loop.log')
Format: '%(asctime)s %(name)s %(levelname)s %(message)s'
```

---

## STEP 13 — Test Suite

**Prompt for Claude Code:**
```
Write the complete test suite for InfiniteLoop Phase 1.

Create strategy-lab/tests/ with:

conftest.py — fixtures:
  - synthetic_day_features(): 300 rows of DayFeatureRow-shaped DataFrame with realistic
    random values, 60/20/20 UP/DOWN/NEUTRAL distribution
  - synthetic_spx_daily(): mock SPX daily OHLCV DataFrame (500 rows at ~5400 price)
  - synthetic_vix_df(): mock VIX daily DataFrame (500 rows, VIX values 14–28)
  - mock_market_data_client(): Mock MarketDataClient that returns the synthetic DataFrames above
  - mock_hermes_client(): Mock that returns valid JSON param change suggestions

test_barchart_csv.py:
  - test_load_spx_daily_renames_latest_to_close
  - test_load_spx_daily_strips_footer_row
  - test_load_es_1min_csv_parses_timestamps_as_ct
  - test_load_all_es_1min_deduplicates_and_sorts
  - test_load_vix_history_returns_vix_close_column
  - test_get_es_covered_dates_returns_set_of_dates

test_market_data.py:
  - test_get_intraday_bars_uses_es_csv_on_covered_date
  - test_get_intraday_bars_uses_spy_hourly_on_uncovered_date
  - test_get_spx_daily_slices_to_requested_range
  - test_get_risk_free_rate_returns_float_between_0_and_1
  - test_coverage_report_lists_all_sources

test_loader.py:
  - test_day_features_correct_columns
  - test_day_outcome_labels_correct (UP when EOD > open + 0.3%)
  - test_train_oos_split_no_overlap

test_indicators.py:
  - test_delta_proxy_positive_for_up_close
  - test_vwap_resets_daily
  - test_opening_range_correct_window (ORB uses only first N minutes)
  - test_gap_pct_signed_correctly

test_options.py:
  - test_simulate_spread_returns_result
  - test_simulate_skips_when_credit_below_threshold
  - test_iron_condor_has_both_put_and_call_sides
  - test_pnl_positive_on_profit_target
  - test_sigma_from_vix_scales_correctly
  - test_find_strike_by_delta_returns_otm_strike

test_engines.py:
  - test_backtest_direction_returns_correct_result
  - test_backtest_spreads_one_row_per_day
  - test_skip_day_has_zero_pnl

test_metrics.py:
  - test_scorecard_all_fields_populated
  - test_passed_false_when_accuracy_below_threshold
  - test_profit_factor_inf_on_no_losses
  - test_summary_str_is_string

test_validator.py:
  - test_oos_not_seen_during_training
  - test_rejection_reason_populated_on_failure
  - test_walk_forward_consistency_computed

test_hermes_parser.py:
  - test_rejects_forced_exit_hour_change
  - test_rejects_multiple_changes
  - test_valid_direction_param_change_accepted
  - test_valid_spread_param_change_accepted
  - test_type_coercion_int_stays_int

test_orb_direction.py:
  - test_classify_up_on_positive_gap_and_orb_breakout
  - test_classify_neutral_on_small_gap_low_delta
  - test_classify_skip_on_nan_features
  - test_get_params_roundtrip (from_params(get_params()) == original)
  - test_allowed_params_excludes_risk_params

After writing all tests, run:
  cd strategy-lab && python -m pytest tests/ -v --tb=short

Fix any failures. Report final test results.
```

---

## STEP 14 — Final Integration Verification

**Prompt for Claude Code:**
```
Run a full end-to-end integration check for InfiniteLoop Phase 1.

1. Verify Ollama is available:
   python -c "from hermes.client import HermesClient; c = HermesClient(); print('Ollama:', c.is_available())"

2. Verify database connection:
   python -c "
   from dotenv import load_dotenv; load_dotenv('../.env')
   from strategy.registry import StrategyRegistry
   r = StrategyRegistry(); r.initialize_schema()
   print('DB OK, schema initialized')
   "

3. Verify data sources (Barchart CSVs + yfinance fallback):
   python -c "
   from data.market_data import MarketDataClient
   c = MarketDataClient()
   print(c.coverage_report())
   df = c.get_spx_daily('2024-01-01', '2024-03-31')
   print('SPX daily rows:', len(df), '| columns:', list(df.columns))
   print('Risk-free rate:', c.get_risk_free_rate())
   "

4. Dry-run the loop with 3 iterations:
   python loop.py --iterations 3 --dry-run

   Expected output:
   - Day features loaded from yfinance cache
   - 3 Hermes iterations logged
   - Scorecard printed per iteration
   - Validation attempted
   - No CRITICAL errors

5. Run full test suite:
   python -m pytest tests/ -v --tb=short

Report: test pass rate, any import errors, whether dry-run completed without CRITICAL errors.
Fix any failures before reporting complete.
```

---

---

## ⏸️ MANUAL STOP 4 — Review Results Before Declaring Phase 1 Done

**Claude Code: Pause here. Show Kirk the dashboard output and walk through the results together before marking Phase 1 complete.**

Tell Kirk:

> "The loop has run. Here's what I found:
>
> **Strategy:** [name] v[version]
> **Direction Accuracy:** [X]% (threshold: 55%)
> **Profit Factor:** [X] (threshold: 1.5)
> **Sharpe Ratio:** [X] (threshold: 0.8)
> **Max Drawdown:** [X]% (threshold: 20%)
> **Total Trades:** [X] (minimum: 200)
> **OOS Result:** [passed/failed]
>
> **My assessment:** [1-2 sentences on whether the strategy looks genuine or overfit]
>
> A few questions before I write it to the strategy registry:
> 1. Does this direction accuracy feel right based on your trading experience?
> 2. Are you comfortable with the max drawdown number?
> 3. Any parameters you want Hermes to explore more before we lock it in?
>
> Say 'save it' to write to the registry and close out Phase 1, or 'keep going' to run more Hermes iterations."

Wait for Kirk's decision before writing to the registry or moving to Phase 2.

---

# SECTION 3 — SUCCESS CRITERIA

Phase 1 is **done** when ALL of the following are true:

| Check | Criteria |
|---|---|
| ✅ Tests pass | `pytest tests/` → 0 failures |
| ✅ Loop runs | `python loop.py --iterations 3 --dry-run` → no CRITICAL errors |
| ✅ Ollama connected | `HermesClient.is_available()` → True |
| ✅ Market data works | `MarketDataClient` fetches SPX daily + VIX without error |
| ✅ DB initialized | All 4 tables (strategies, trades, equity_snapshots, portfolio_events) exist |
| ✅ Valid strategy found | Loop produces a strategy passing IS backtest (OOS is the real test) |
| ✅ Strategy saved | Strategy record in `strategies` table with `status='active'` |
| ✅ Docs complete | `docs/PHASE1_HOW_IT_WORKS.md` covers all components |

When all are green, Phase 1 is done and Phase 2 (Execution Agent on Railway) begins.

---

*Last updated: June 2026 | InfiniteLoop Trading System — 0DTE SPX Options | Starting capital: $5,000*
