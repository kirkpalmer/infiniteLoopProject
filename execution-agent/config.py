"""
config.py — Load and validate all environment variables at startup.

Import this module FIRST in main.py so every other module sees the env vars.
Missing required variables raise ValueError immediately — fail fast rather
than trading with broken credentials.

Auth note: The Webull SDK (webull-openapi-python-sdk) only needs WEBULL_APP_KEY
and WEBULL_APP_SECRET. It handles token exchange, refresh, and signing internally.
No WEBULL_TRADE_TOKEN needed.

WEBULL_ENVIRONMENT controls which Webull API environment is used:
  'uat'  → sandbox/test (safe for development and paper trading)
  'prod' → production (real money — Phase 3 only)

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

# ── Webull SDK credentials (only these two needed — SDK handles the rest) ──────
WEBULL_APP_KEY    = os.getenv("WEBULL_APP_KEY", "")
WEBULL_APP_SECRET = os.getenv("WEBULL_APP_SECRET", "")
WEBULL_REGION_ID  = os.getenv("WEBULL_REGION_ID", "us")

# 'uat' = sandbox (paper trading), 'prod' = live (Phase 3 only)
# MUST stay 'uat' until Phase 3 is approved in writing.
WEBULL_ENVIRONMENT = os.getenv("WEBULL_ENVIRONMENT", "uat").strip().lower()

# Account ID — required for order and balance API calls.
# Find yours by calling trade_client.account_v2.get_account_list() once.
WEBULL_ACCOUNT_ID = os.getenv("WEBULL_ACCOUNT_ID", "")

# ── Database ──────────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "")

# ── Risk overrides (defaults in constants.py) ─────────────────────────────────
MAX_DAILY_LOSS_PCT     = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.05"))
MAX_RISK_PER_TRADE_PCT = float(os.getenv("MAX_RISK_PER_TRADE_PCT", "0.10"))

# ── Notifications ─────────────────────────────────────────────────────────────
ALERT_EMAIL = os.getenv("ALERT_EMAIL", "")

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# ── Convenience flags ─────────────────────────────────────────────────────────
IS_PAPER_MODE = WEBULL_ENVIRONMENT != "prod"

# Webull SDK API host for the configured environment
WEBULL_API_HOST = (
    "api.webull.com"
    if WEBULL_ENVIRONMENT == "prod"
    else "us-openapi-alb.uat.webullbroker.com"
)

# Webull SDK streaming host for the configured environment
WEBULL_STREAM_HOST = (
    "data-api.webull.com"
    if WEBULL_ENVIRONMENT == "prod"
    else "us-data-api.uat.webullbroker.com"
)


def build_api_client():
    """
    Build and return an authenticated Webull ApiClient.
    The SDK handles token exchange, refresh, and request signing automatically.
    Call this once at startup and share the instance.
    """
    from webull.core.client import ApiClient
    client = ApiClient(WEBULL_APP_KEY, WEBULL_APP_SECRET, WEBULL_REGION_ID)
    client.add_endpoint(WEBULL_REGION_ID, WEBULL_API_HOST)
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
            "Set them in execution-agent/.env or Railway environment."
        )

    if WEBULL_ENVIRONMENT not in ("uat", "prod"):
        raise ValueError(
            f"WEBULL_ENVIRONMENT must be 'uat' or 'prod', got '{WEBULL_ENVIRONMENT}'"
        )

    if WEBULL_ENVIRONMENT == "prod":
        LOGGER.critical(
            "PRODUCTION ENVIRONMENT — all orders are REAL. "
            "Ensure Phase 3 sign-off is complete before proceeding."
        )
    else:
        LOGGER.info(
            "Environment: UAT/sandbox (paper trading). "
            "Set WEBULL_ENVIRONMENT=prod for live trading (Phase 3 only)."
        )
