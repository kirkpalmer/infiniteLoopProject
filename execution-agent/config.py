"""
config.py — Load and validate all environment variables at startup.

Import this module FIRST in main.py so every other module sees the env vars.
Missing required variables raise ValueError immediately — fail fast rather
than trading with broken credentials.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

LOGGER = logging.getLogger("infiniteloop.config")

# Load .env from the execution-agent directory (or any parent)
_env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(_env_path, override=False)

# ── Webull credentials ────────────────────────────────────────────────────────
WEBULL_APP_KEY     = os.getenv("WEBULL_APP_KEY", "")
WEBULL_APP_SECRET  = os.getenv("WEBULL_APP_SECRET", "")
WEBULL_TRADE_TOKEN = os.getenv("WEBULL_TRADE_TOKEN", "")
WEBULL_ACCOUNT_ID  = os.getenv("WEBULL_ACCOUNT_ID", "")

# MUST be 'paper' until Phase 3 is approved in writing.
WEBULL_TRADING_MODE = os.getenv("WEBULL_TRADING_MODE", "paper").strip().lower()

# ── Database ──────────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "")

# ── Risk overrides (defaults in constants.py; env vars allow Railway tuning) ──
MAX_DAILY_LOSS_PCT    = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.05"))
MAX_RISK_PER_TRADE_PCT = float(os.getenv("MAX_RISK_PER_TRADE_PCT", "0.10"))

# ── Notifications ─────────────────────────────────────────────────────────────
ALERT_EMAIL = os.getenv("ALERT_EMAIL", "")

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


# Convenience flag used by order manager and watchdog
IS_PAPER_MODE = WEBULL_TRADING_MODE != "live"


def validate() -> None:
    """
    Raise ValueError if any required credential is missing or trading mode
    is invalid. Call once at startup before anything else runs.
    """
    missing = []
    for name, value in [
        ("WEBULL_APP_KEY",     WEBULL_APP_KEY),
        ("WEBULL_APP_SECRET",  WEBULL_APP_SECRET),
        ("WEBULL_TRADE_TOKEN", WEBULL_TRADE_TOKEN),
        ("WEBULL_ACCOUNT_ID",  WEBULL_ACCOUNT_ID),
        ("DATABASE_URL",       DATABASE_URL),
    ]:
        if not value:
            missing.append(name)

    if missing:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing)}. "
            "Set them in execution-agent/.env or Railway environment."
        )

    if WEBULL_TRADING_MODE not in ("paper", "live"):
        raise ValueError(
            f"WEBULL_TRADING_MODE must be 'paper' or 'live', got '{WEBULL_TRADING_MODE}'"
        )

    if WEBULL_TRADING_MODE == "live":
        LOGGER.critical(
            "LIVE TRADING MODE — all orders will be REAL. "
            "Ensure Phase 3 sign-off is complete before proceeding."
        )
    else:
        LOGGER.info("Trading mode: PAPER (orders go to Webull paper account)")
