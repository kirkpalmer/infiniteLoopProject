"""
config.py — Central environment configuration for the Strategy Lab.

Loads the project-root .env file exactly once, before any module reads
os.getenv(). Import this module FIRST in every entry point (server.py,
scripts, tests):

    import config  # noqa: F401  (side effect: loads .env)

Why this exists: previously nothing called load_dotenv(), so DATABASE_URL /
OLLAMA_* were only visible if they happened to be set in the shell. The
server then silently fell back to "ephemeral mode" and no Oracle iterations
were ever persisted. This module makes env loading explicit and loud.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

LOGGER = logging.getLogger("infiniteloop.config")

# Project root = parent of strategy-lab/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

# Load .env without overriding anything already set in the real environment.
_LOADED = load_dotenv(ENV_PATH, override=False)


def database_url() -> str | None:
    return os.getenv("DATABASE_URL")


def ollama_base_url() -> str:
    return os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")


def ollama_model() -> str:
    return os.getenv("OLLAMA_MODEL", "hermes3")


def report() -> str:
    """One-line, secret-free summary for startup logs."""
    db = "set" if database_url() else "MISSING"
    return (
        f"config: .env {'loaded' if _LOADED else 'NOT FOUND'} at {ENV_PATH} | "
        f"DATABASE_URL={db} | OLLAMA={ollama_base_url()} model={ollama_model()}"
    )


# Log once at import so every entry point shows the config state.
LOGGER.info(report())
