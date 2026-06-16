"""Shared constants for InfiniteLoop Strategy Lab Phase 1."""

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
ORB_MINUTES = 30  # use first 30 minutes to establish ORB
ORB_CONFIRM_MINUTES = 15  # breakout measured this many minutes AFTER the ORB window closes
                          # (classification happens at ~minute 45 — NEVER at the daily close)

# Spread defaults (Hermes optimizes these)
DEFAULT_SHORT_DELTA = 20
DEFAULT_SPREAD_WIDTH_USD = 5
DEFAULT_PROFIT_TARGET_PCT = 50
DEFAULT_STOP_LOSS_PCT = 200

# Risk - HARD LIMITS, never change
MAX_RISK_PER_TRADE_PCT = 0.10  # 10% of equity - defined-risk spreads cap loss structurally
MAX_DAILY_LOSS_PCT = 0.05
FORCED_EXIT_HOUR = 15
FORCED_EXIT_MINUTE = 45

# Validation thresholds (Tier 3 - full validation)
MIN_DIRECTION_ACCURACY = 0.55
MIN_SHARPE = 0.8
MIN_PROFIT_FACTOR = 1.5
MAX_DRAWDOWN_PCT = 0.20

# Tiered promotion system - fail fast, only run full backtest when promising
TIER1_MONTHS = 6  # quick screen: 6 months in-sample
TIER2_YEARS = 2  # medium: 2 years in-sample
TIER3_YEARS = 4  # full: 4 years IS + OOS (last 20%)
OOS_FRACTION = 0.20  # last 20% of data is OOS - never touched during optimization

TIER1_MIN_ACCURACY = 0.50  # Tier 1 pass threshold
TIER1_MIN_PF = 1.20

TIER2_MIN_ACCURACY = 0.55  # Tier 2 pass threshold
TIER2_MIN_PF = 1.50
TIER2_MIN_SHARPE = 0.80
TIER2_MAX_DD = 0.25

TIER3_OOS_TOLERANCE = 0.20  # OOS accuracy must be within 20% of in-sample

# Event calendar skip - hard-coded, not configurable
# Updated annually from: federalreserve.gov, bls.gov, bea.gov
# See strategy-lab/data/events.py for the full year's schedule

# Volatility regime gate - Hermes-optimizable
DEFAULT_MIN_IV_RANK_PCT = 25.0  # skip if IV rank below this (not enough premium)
DEFAULT_MAX_VIX = 32.0  # skip if VIX above this (realized vol too high)
