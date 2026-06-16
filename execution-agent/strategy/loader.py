"""
strategy/loader.py — Load the active Oracle strategy from PostgreSQL.

Layer 1 (Strategy Lab) writes validated strategies to the `strategies` table.
Layer 2 reads from it here. The two layers are fully decoupled — Layer 2
never imports from strategy-lab; it only reads the DB.

The active strategy is serialized as a JSON params dict. We reconstruct
an OracleClassifier (a lightweight re-implementation of the core logic)
from those params without importing the full strategy-lab codebase.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

import config

LOGGER = logging.getLogger("infiniteloop.strategy.loader")


@dataclass
class OracleParams:
    """Direction-classifier thresholds — mirrors oracle/classifier.py params."""
    gap_threshold_pct: float       = 0.002
    neutral_band_pct: float        = 0.001
    orb_breakout_pct: float        = 0.001
    delta_bias_threshold: float    = 200.0
    vwap_slope_threshold: float    = 5e-5
    vol_filter_high: float         = 40.0
    vol_filter_low: float          = 10.0
    prev_day_return_threshold: float = 0.003
    prev_day_vwap_threshold: float   = 0.001
    min_confidence: float          = 0.0
    min_score_separation: float    = 0.0

    @classmethod
    def from_dict(cls, d: dict) -> "OracleParams":
        valid = {k: float(v) for k, v in d.items() if hasattr(cls, k) and v is not None}
        return cls(**valid)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class TradeParams:
    """Trade-agent parameters — mirrors tradeagent/simulator.py TradeParams."""
    em_mult_high: float        = 0.5
    em_mult_low: float         = 1.2
    spread_width: float        = 5.0
    profit_target_pct: float   = 50.0
    stop_loss_pct: float       = 200.0
    entry_minute: int          = 50
    condor_em_mult: float      = 1.2

    @classmethod
    def from_dict(cls, d: dict) -> "TradeParams":
        valid = {}
        for k in ["em_mult_high", "em_mult_low", "spread_width",
                   "profit_target_pct", "stop_loss_pct", "condor_em_mult"]:
            if k in d and d[k] is not None:
                valid[k] = float(d[k])
        if "entry_minute" in d and d["entry_minute"] is not None:
            valid["entry_minute"] = int(d["entry_minute"])
        return cls(**valid)


@dataclass
class ActiveStrategy:
    """Combined Oracle + Trade params loaded from DB."""
    strategy_id: int
    name: str
    oracle: OracleParams
    trade: TradeParams
    overall_accuracy: float
    directional_precision: float
    notes: str


def load_active_strategy(database_url: Optional[str] = None) -> Optional[ActiveStrategy]:
    """
    Query the strategies table for the current active strategy.

    Returns None if no active strategy exists (execution agent should halt
    until one is available).
    """
    url = database_url or config.DATABASE_URL
    try:
        import psycopg2
        conn = psycopg2.connect(url, connect_timeout=10)
    except Exception as exc:
        LOGGER.error("Could not connect to database: %s", exc)
        return None

    try:
        with conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, name, oracle_params, trade_params,
                       overall_accuracy, directional_precision, notes
                FROM strategies
                WHERE status = 'active'
                ORDER BY promoted_at DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
    except Exception as exc:
        LOGGER.error("Strategy query failed: %s", exc)
        return None
    finally:
        conn.close()

    if row is None:
        LOGGER.warning("No active strategy found in strategies table")
        return None

    strategy_id, name, oracle_json, trade_json, accuracy, precision, notes = row

    def _parse(raw) -> dict:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            return json.loads(raw)
        return {}

    oracle_params = OracleParams.from_dict(_parse(oracle_json))
    trade_params  = TradeParams.from_dict(_parse(trade_json) if trade_json else {})

    strategy = ActiveStrategy(
        strategy_id=strategy_id,
        name=name or "unnamed",
        oracle=oracle_params,
        trade=trade_params,
        overall_accuracy=float(accuracy or 0),
        directional_precision=float(precision or 0),
        notes=notes or "",
    )
    LOGGER.info(
        "Loaded active strategy '%s' (id=%d, precision=%.1f%%)",
        strategy.name, strategy.strategy_id, strategy.directional_precision * 100,
    )
    return strategy
