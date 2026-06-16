# Session Notes — June 7, 2026

Design decisions and new features agreed upon in this session. These supplement CLAUDE.md, ROADMAP.md, and ARCHITECTURE.md. All items below have been incorporated into PHASE1_BUILD_PLAN.md and ARCHITECTURE.md.

---

## 1. Kirk's Real Trading Parameters (Seed Values for Hermes)

Kirk has been manually trading weekly SPX verticals and iron condors with real money. Key validated findings:

- **5-year backtest insight**: SPX stays within ±120 points 90%+ of the time in a week. This is the core statistical edge — implied volatility consistently overestimates realized volatility, and selling premium captures that gap.
- **Entry**: Thursday afternoon, next-Friday expiry (~8 trading days, captures weekend decay)
- **Exit**: 60–70% of max profit by Tuesday or Wednesday
- **Return per trade**: 5–8%
- **Loss management**: Exits when market moves significantly against position
- **Direction read**: Volume up + price up = bullish; volume up + price down = bearish; flat/flat = neutral

**Mapping to 0DTE seed parameters:**
```python
ORBDirectionParams(
    gap_threshold_pct    = 0.30,   # >0.30% gap = committed directional day
    orb_breakout_pct     = 0.15,   # ORB breakout must be meaningful
    delta_bias_threshold = 200,    # cumulative delta in first 30 min
    neutral_band_pct     = 0.15,   # inside ±0.15% gap → lean neutral/condor

    entry_hour           = 10,     # enter after ORB established
    short_delta          = 15,     # ~40–50 points OTM on SPX (0DTE equivalent of 120pt weekly buffer)
    spread_width_usd     = 5,      # $5-wide to start
    profit_target_pct    = 65,     # Kirk's real-world exit point
    stop_loss_pct        = 150,    # exit if spread doubles against you
)
```

---

## 2. Event Calendar Filter (Hard Skip — Pre-Market)

On scheduled macro event days, the morning orderflow signal breaks down. The direction classifier is not built to predict FOMC decisions, CPI surprises, or NFP prints. These are known unknowns — the dates are schedulable months in advance.

**Implementation:** `strategy-lab/data/events.py`

```python
def is_event_day(date: datetime.date) -> bool:
    """Return True if today is a major macro event — skip trading."""
    return date in FOMC_DATES | CPI_DATES | NFP_DATES | PCE_DATES
```

**Event types to skip:**
- FOMC announcement days (8 per year, Fed publishes schedule annually)
- CPI release days (monthly, BLS publishes schedule annually)
- NFP Fridays (first Friday of each month)
- PCE release days (monthly)
- Optionally: the day *after* a major event (market "digestion" mode)

**Data source:** Federal Reserve, BLS, and BEA publish annual schedules. Hardcode in a constants file, update annually. No API needed.

**Where it runs:** Pre-market check in Layer 2 classifier, and as a filter in Layer 1 backtest (skip those days in the training data too, so backtest matches live behavior).

---

## 3. Volatility Regime Gate (Soft Gate — VIX/IV Rank)

Kirk's 90%+ stat holds in normal volatility regimes. In VIX 30+, the realized range widens and the edge degrades. In VIX < 13, there isn't enough premium to justify the trade.

**The sweet spot for premium selling: IV rank 30–70%**
- IV elevated relative to history → collect rich premium
- VIX in normal regime → realized vol likely to undershoot implied vol

**New Hermes-optimizable parameters:**
- `min_iv_rank_pct` (default: 25) — skip if IV rank below this
- `max_vix_threshold` (default: 32) — skip if VIX above this

**Data source:** VIX daily levels from CBOE free CSV. Rolling VIX rank (252-day percentile) computed from the same CSV — no paid API needed.

**New module:** `strategy-lab/data/vol_regime.py` — fetches and caches VIX history and SPX IV rank.

**Pre-entry gate (runs in both Layer 1 backtest filter and Layer 2 pre-classifier):**
```
1. Is today on the event calendar? → SKIP
2. Is IV rank below min_iv_rank_pct? → SKIP (not enough premium)
3. Is VIX above max_vix_threshold? → SKIP (too much realized vol risk)
4. Pass all three? → Run direction classifier → UP / DOWN / NEUTRAL / SKIP
```

---

## 4. Tiered Promotion System (Fast-Fail Backtesting)

Running a full 5-year backtest on every Hermes iteration is wasteful. Bad strategies should fail fast. Only promising strategies earn the full historical run.

**Three-tier promotion system:**

| Tier | Data window | Min trades | Threshold | Speed |
|------|------------|------------|-----------|-------|
| Tier 1 — Quick Screen | 6 months in-sample | ~50 | dir accuracy > 50%, PF > 1.2 | Seconds |
| Tier 2 — Medium Validation | 2 years in-sample | ~200 | dir accuracy > 55%, PF > 1.5, Sharpe > 0.8, DD < 25% | ~30 sec |
| Tier 3 — Full Validation | 4 years in-sample + OOS (last 20%) + walk-forward | 400+ | All criteria + OOS within 20% of in-sample | 2–5 min |

**How the Hermes loop uses this:**
- Hermes iterates primarily within Tier 1 and Tier 2
- Only strategies that survive Tier 2 earn the Tier 3 full run
- If Tier 3 OOS fails, the strategy is flagged as overfit — Hermes backtracks
- Strategies that pass Tier 3 are candidates for the strategy registry

**New constants** (`constants.py`):
```python
TIER1_MONTHS      = 6
TIER2_YEARS       = 2
TIER3_YEARS       = 4   # + last 20% OOS = ~5 years total
OOS_FRACTION      = 0.20

TIER1_MIN_ACCURACY = 0.50
TIER1_MIN_PF       = 1.20

TIER2_MIN_ACCURACY = 0.55
TIER2_MIN_PF       = 1.50
TIER2_MIN_SHARPE   = 0.80
TIER2_MAX_DD       = 0.25

TIER3_MIN_ACCURACY = 0.55
TIER3_MIN_PF       = 1.50
TIER3_MIN_SHARPE   = 0.80
TIER3_MAX_DD       = 0.20
TIER3_OOS_TOLERANCE = 0.20   # live/OOS must be within 20% of in-sample
```

---

## 5. Results Dashboard

After any backtest run, display a rich terminal dashboard using the `rich` Python library. Also export an HTML snapshot for archiving.

**Dashboard contents:**
- Strategy name and current parameters
- Tier reached and pass/fail status
- Scorecard table: direction accuracy, win rate, profit factor, Sharpe ratio, max drawdown, expectancy, trade count
- Last 10 trades table (entry, exit, spread type, P&L)
- Hermes iteration history (parameter changed, before/after metric delta)
- Equity curve (ASCII sparkline in terminal, matplotlib PNG for HTML export)
- Promotion status: "Promoted to Tier 2" / "Promoted to Tier 3" / "Passed — ready for registry"

**New module:** `strategy-lab/dashboard.py`

**Display trigger:** After every backtest run in `loop.py`, call `dashboard.render(result)`.

---

## 6. Orderflow / Footprint — Clarification

Orderflow IS factored in, but with a backtest-live gap that must be understood:

**Layer 2 (live):** Webull MQTT ES tick feed provides real-time tick data. The classifier computes actual cumulative delta (running sum of tick volume, signed by direction). This is true orderflow.

**Layer 1 (backtest):** yfinance provides hourly SPY bars (1-min is limited to 60 days, too short for multi-year backtests). True bid/ask volume at each price (footprint) is not available. We compute a **delta proxy**:
```python
delta_proxy = (close - open) / (high - low) * volume
```
This approximates buying/selling pressure per bar but is not the same as actual footprint data.

**Implication:** The backtest slightly undersells the live signal quality. Direction accuracy in live paper trading may be *better* than the backtest predicts (if the real orderflow signal is cleaner than the proxy). The 30-day paper trading validation is designed to measure this gap.

**Future:** If a tick-level data source with trade side tagging becomes available (e.g., Polygon.io free tier, or Webull historical tick data), upgrading `market_data.py` would close this gap. Worth investigating once live paper trading begins in Phase 2.

---

## 7. Backlog Items Added (from this session)

Added to ROADMAP.md Future Ideas:
- **VIX regime filter**: adjust behavior based on VIX level (already being built as part of vol regime gate)
- **News/catalyst filter**: skip 0DTE on FOMC, CPI, NFP (built as event calendar filter above)
- **Results dashboard**: rich terminal + HTML export (added to Phase 1 milestones)

---

*All items in this document have been incorporated into PHASE1_BUILD_PLAN.md, ARCHITECTURE.md, and ROADMAP.md.*
