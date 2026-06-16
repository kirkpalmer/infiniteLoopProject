"""Parse and validate Hermes responses for the InfiniteLoop optimization loop."""

from __future__ import annotations

from typing import Any

ALLOWED_PARAMS = [
    "gap_threshold_pct",
    "orb_breakout_pct",
    "delta_bias_threshold",
    "neutral_band_pct",
    "entry_hour",
    "short_delta",
    "spread_width_usd",
    "profit_target_pct",
    "stop_loss_pct",
]

NEVER_CHANGE = ["forced_exit_hour", "max_loss_pct", "daily_halt_pct"]


def validate_change_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Ensure Hermes only changes one allowed parameter."""

    param_name = payload.get("param_to_change")
    if param_name in NEVER_CHANGE:
        raise ValueError(f"Hermes attempted to change a protected parameter: {param_name}")
    if param_name not in ALLOWED_PARAMS:
        raise ValueError(f"Hermes attempted to change an unsupported parameter: {param_name}")
    return payload
