"""Shared constants for the InfiniteLoop execution agent (Layer 2)."""

# ── Instrument ────────────────────────────────────────────────────────────────
SPX_MULTIPLIER       = 100          # $100 per point per contract
STRIKE_INCREMENT     = 5.0          # SPX strikes are $5 apart
TICK_SIZE            = 0.05         # options minimum tick

# ── Session timing (US/Eastern) ───────────────────────────────────────────────
RTH_OPEN_HOUR        = 9
RTH_OPEN_MINUTE      = 30
RTH_CLOSE_HOUR       = 16
RTH_CLOSE_MINUTE     = 0

# ── Classification window ─────────────────────────────────────────────────────
# Oracle needs at least this many RTH minutes before it classifies.
# 30 min = just the ORB; 45 min = ORB + first trend confirmation.
MIN_RTH_MINUTES_BEFORE_CLASSIFY = 45

# ── Hard-coded exit timing (NON-NEGOTIABLE) ───────────────────────────────────
# All 0DTE spreads MUST be closed by 15:45 ET. Never hold to expiration.
# Enforced by the watchdog — NOT configurable.
FORCED_EXIT_HOUR     = 15
FORCED_EXIT_MINUTE   = 45

# ── Risk limits (enforced in code, not config) ────────────────────────────────
MAX_RISK_PER_TRADE_PCT   = 0.10     # 10 % of equity per spread
MAX_DAILY_LOSS_PCT       = 0.05     # halt trading if daily P&L ≤ -5 %
MAX_CONTRACTS            = 1        # 1 contract until equity > $15,000
SINGLE_CONTRACT_CAP      = 15_000.0 # equity threshold to start scaling

# ── Spread defaults (from TradeParams validated by the policy race) ───────────
DEFAULT_SPREAD_WIDTH      = 5.0     # points
DEFAULT_SHORT_DELTA       = 0.20    # ~20-delta short strike
DEFAULT_PROFIT_TARGET_PCT = 50.0    # close at 50 % of max credit captured
DEFAULT_STOP_LOSS_PCT     = 200.0   # close if loss = 2× credit
DEFAULT_ENTRY_MINUTE      = 50      # minutes after RTH open (post-classification)
