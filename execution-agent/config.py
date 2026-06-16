"""
config.py — Load and validate all environment variables at startup.

Import this module FIRST in main.py so every other module sees the env vars.
Missing required variables raise ValueError immediately — fail fast rather
than trading with broken credentials.

Auth note: The Webull SDK (webull-openapi-python-sdk) only needs WEBULL_APP_KEY
and WEBULL_APP_SECRET. It handles token exchange, refresh, and request signing
internally. These are your PRODUCTION developer credentials from developer.webull.com.

Paper trading vs live trading:
  WEBULL_PAPER_TRADING=true  → connect to Webull for real market data, but
                                simulate order fills in our own code (no real money)
  WEBULL_PAPER_TRADING=false → place real orders (Phase 3 only)

We ALWAYS connect to the production Webull API (api.webull.com) for market data.
Webull's UAT sandbox requires separate credentials and has limited data — we don't
use it. Paper mode is controlled by WEBULL_PAPER_TRADING, not the API endpoint.

WEBULL_ACCOUNT_ID is still required: the trading API needs it for every order.
Fetch it once at startup via trade_client.account_v2.get_account_list() if unsure.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

LOGGER = logging.getLogger("infiniteloop.config")

# Load .env from the execution-agent directory
_env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(_env_path, override=False)

# ── Webull SDK credentials ────────────────────────────────────────────────────
# Production developer credentials from developer.webull.com
WEBULL_APP_KEY    = os.getenv("WEBULL_APP_KEY", "")
WEBULL_APP_SECRET = os.getenv("WEBULL_APP_SECRET", "")
WEBULL_REGION_ID  = os.getenv("WEBULL_REGION_ID", "us")

# Account ID — required for order and balance API calls.
WEBULL_ACCOUNT_ID = os.getenv("WEBULL_ACCOUNT_ID", "")

# ── Paper trading flag ────────────────────────────────────────────────────────
# MUST be 'true' until Phase 3 is approved.
# Paper mode = real market data, simulated order fills, is_paper=True in DB.
_paper_raw = os.getenv("WEBULL_PAPER_TRADING", "true").strip().lower()
IS_PAPER_MODE: bool = _paper_raw not in ("false", "0", "no")

# ── Webull API endpoints (always production — UAT needs separate credentials) ─
WEBULL_API_HOST    = "api.webull.com"
WEBULL_STREAM_HOST = "data-api.webull.com"

# ── Database ──────────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "")

# ── Risk overrides (defaults in constants.py) ─────────────────────────────────
MAX_DAILY_LOSS_PCT     = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.05"))
MAX_RISK_PER_TRADE_PCT = float(os.getenv("MAX_RISK_PER_TRADE_PCT", "0.10"))

# ── Notifications ─────────────────────────────────────────────────────────────
ALERT_EMAIL = os.getenv("ALERT_EMAIL", "")

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


def build_api_client():
    """
    Build and return an authenticated Webull ApiClient using production credentials.
    The SDK handles token exchange, refresh, and request signing automatically.
    Call this once at startup and share the instance across all components.
    """
    from webull.core.client import ApiClient
    # No add_endpoint() call needed — the SDK's built-in endpoints.json already
    # has api.webull.com for the "us" region, which is what we want.
    client = ApiClient(WEBULL_APP_KEY, WEBULL_APP_SECRET, WEBULL_REGION_ID)
    return client


def validate() -> None:
    """
    Raise ValueError if any required credential is missing.
    Call once at startup before anything else runs.
    """
    missing = []
    for name, value in [
        ("WEBULL_APP_KEY",    WEBULL_APP_KEY),
        ("WEBULL_APP_SECRET", WEBULL_APP_SECRET),
        ("WEBULL_ACCOUNT_ID", WEBULL_ACCOUNT_ID),
        ("DATABASE_URL",      DATABASE_URL),
    ]:
        if not value:
            missing.append(name)

    if missing:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing)}. "
            "Set them in Railway environment variables."
        )

    if IS_PAPER_MODE:
        LOGGER.info(
            "PAPER TRADING MODE — real market data, simulated fills. "
            "Set WEBULL_PAPER_TRADING=false for live trading (Phase 3 only)."
        )
    else:
        LOGGER.critical(
            "LIVE TRADING MODE — real orders will be placed. "
            "Ensure Phase 3 sign-off is complete before proceeding."
        )
