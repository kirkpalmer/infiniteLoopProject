"""
oracle/sweep_advisor.py — Hermes-powered pre-sweep bounds advisor.

Before each Optuna sweep, this module:
  1. Loads the full Oracle optimization history from the registry
  2. Summarises which parameter regions have been explored and what scores they produced
  3. Asks Hermes to suggest NARROWED search bounds that focus the next sweep on
     unexplored promising territory rather than re-probing known ground

If Hermes is unavailable or produces bad output the original SEARCH_SPACE bounds
are returned unchanged — the sweep still runs, just without narrowing.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections import defaultdict
from typing import Any

LOGGER = logging.getLogger("infiniteloop.oracle.sweep_advisor")

# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

_PROMPT = """\
You are the Oracle sweep advisor for InfiniteLoop, a 0DTE SPX options trading system.

Oracle classifies each trading day as UP, DOWN, or NEUTRAL. The optimization objective
is DIRECTIONAL PRECISION — when Oracle calls UP or DOWN, how often is it correct?

You have been given the complete history of Oracle parameter optimization. Your task:
analyse which parameter regions are already well-explored, identify where the best results
cluster, and suggest NARROWED search bounds that focus the next sweep on unexplored
promising territory.

=== CURRENT SEARCH BOUNDS ===
{current_bounds}

=== TOP {n_top} PARAMETER SETS BY ACCURACY (best → worst) ===
{top_params}

=== PARAMETER VALUE RANGES SEEN ACROSS TOP {n_top} RESULTS ===
{param_stats}

=== FULL HISTORY SUMMARY ===
Total recorded iterations: {total_trials}
Accuracy range: {score_min:.4f} → {score_max:.4f}
Median accuracy: {score_med:.4f}

=== YOUR TASK ===
1. Identify the parameter range that the top results cluster in.
2. Look for parameters where the top results all share a similar sub-range —
   those are good candidates for narrowing.
3. If a parameter is all over the place among top results, leave its bounds wide.
4. Do NOT expand beyond the current bounds — only narrow.
5. Bounds must be valid (low < high) and inside the values shown above.

Respond ONLY with valid JSON:
{{
  "reasoning": "2-3 sentences explaining what the history shows and what to focus on next",
  "bounds": {{
    "gap_threshold_pct":         {{"low": X, "high": Y}},
    "neutral_band_frac":         {{"low": X, "high": Y}},
    "orb_breakout_pct":          {{"low": X, "high": Y}},
    "delta_bias_threshold":      {{"low": X, "high": Y}},
    "vwap_slope_threshold":      {{"low": X, "high": Y}},
    "vol_filter_high":           {{"low": X, "high": Y}},
    "vol_filter_low":            {{"low": X, "high": Y}},
    "prev_day_return_threshold": {{"low": X, "high": Y}},
    "prev_day_vwap_threshold":   {{"low": X, "high": Y}},
    "min_confidence":            {{"low": X, "high": Y}},
    "min_score_separation":      {{"low": X, "high": Y}}
  }}
}}"""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def get_hermes_sweep_bounds(
    registry,
    hermes_client,
    current_search_space: dict,
    n_top: int = 20,
) -> tuple[dict, str]:
    """
    Ask Hermes to suggest narrowed search bounds based on optimization history.

    Returns:
        (search_space, reasoning)
        search_space — possibly narrowed copy of current_search_space
        reasoning    — Hermes's explanation (empty string if advisor skipped)
    """
    if registry is None or hermes_client is None:
        return current_search_space, ""

    try:
        history = registry.get_all_completed_trials(min_accuracy=0.40)
    except Exception as exc:
        LOGGER.warning("Sweep advisor: could not load history — %s", exc)
        return current_search_space, ""

    if len(history) < 10:
        LOGGER.info(
            "Sweep advisor: only %d trials in history (need ≥10) — skipping Hermes analysis",
            len(history),
        )
        return current_search_space, ""

    top = history[:n_top]
    prompt = _build_prompt(top, history, current_search_space, n_top)

    try:
        raw = hermes_client.generate(prompt)
    except Exception as exc:
        LOGGER.warning("Sweep advisor: Hermes call failed — %s", exc)
        return current_search_space, ""

    return _parse_response(raw, current_search_space)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_prompt(
    top: list[tuple[dict, float]],
    all_history: list[tuple[dict, float]],
    search_space: dict,
    n_top: int,
) -> str:
    # Format current bounds
    bound_lines = [
        f"  {name}: [{spec['low']:.5g}, {spec['high']:.5g}] log={spec.get('log', False)}"
        for name, spec in search_space.items()
    ]

    # Top-N param sets
    top_lines = []
    for i, (params, score) in enumerate(top, 1):
        parts = [f"accuracy={score:.4f}"]
        for name in search_space:
            v = params.get(name) or params.get("neutral_band_pct")  # gap special case handled below
            if name == "neutral_band_frac":
                # Reconstruct frac from stored neutral_band_pct / gap_threshold_pct
                gap = params.get("gap_threshold_pct", 0)
                nb = params.get("neutral_band_pct", 0)
                v = round(nb / gap, 4) if gap > 0 else None
            elif name in params:
                v = params[name]
            else:
                v = None
            parts.append(f"{name}={_fmt(v)}")
        top_lines.append(f"{i}. " + "  ".join(parts))

    # Per-param stats across top-N
    param_values: dict[str, list[float]] = defaultdict(list)
    for params, _ in top:
        for k, v in params.items():
            if v is not None and k in search_space:
                try:
                    param_values[k].append(float(v))
                except (TypeError, ValueError):
                    pass
        # Also compute neutral_band_frac
        gap = params.get("gap_threshold_pct", 0)
        nb = params.get("neutral_band_pct", 0)
        if gap > 0:
            param_values["neutral_band_frac"].append(nb / gap)

    stat_lines = []
    for name in search_space:
        vals = param_values.get(name, [])
        if len(vals) >= 3:
            stat_lines.append(
                f"  {name}: min={min(vals):.5g}  max={max(vals):.5g}  "
                f"median={statistics.median(vals):.5g}  "
                f"stdev={statistics.stdev(vals):.5g}"
            )
        else:
            stat_lines.append(f"  {name}: only {len(vals)} data points — too few to analyse")

    scores = [s for _, s in all_history]

    return _PROMPT.format(
        current_bounds="\n".join(bound_lines),
        n_top=n_top,
        top_params="\n".join(top_lines),
        param_stats="\n".join(stat_lines),
        total_trials=len(all_history),
        score_min=min(scores),
        score_max=max(scores),
        score_med=statistics.median(scores),
    )


def _fmt(v: Any) -> str:
    if v is None:
        return "?"
    try:
        return f"{float(v):.5g}"
    except (TypeError, ValueError):
        return str(v)


def _parse_response(raw: str, current_search_space: dict) -> tuple[dict, str]:
    """Parse Hermes JSON response and return (updated_space, reasoning)."""
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start == -1 or end == 0:
        LOGGER.warning("Sweep advisor: no JSON object found in Hermes response")
        return current_search_space, ""

    try:
        parsed = json.loads(raw[start:end])
    except json.JSONDecodeError as exc:
        LOGGER.warning("Sweep advisor: JSON parse error — %s", exc)
        return current_search_space, ""

    reasoning = str(parsed.get("reasoning", ""))
    LOGGER.info("Sweep advisor reasoning: %s", reasoning)

    suggested = parsed.get("bounds", {})
    if not isinstance(suggested, dict) or not suggested:
        LOGGER.info("Sweep advisor: no bounds in response — keeping originals")
        return current_search_space, reasoning

    updated = {}
    n_narrowed = 0
    for name, spec in current_search_space.items():
        if name not in suggested:
            updated[name] = spec
            continue
        try:
            new_low = float(suggested[name]["low"])
            new_high = float(suggested[name]["high"])
        except (KeyError, TypeError, ValueError):
            LOGGER.debug("Sweep advisor: bad bounds format for %s — skipping", name)
            updated[name] = spec
            continue

        # Clamp to original bounds and validate
        clamped_low = max(new_low, spec["low"])
        clamped_high = min(new_high, spec["high"])

        if clamped_low >= clamped_high:
            LOGGER.warning(
                "Sweep advisor: invalid narrowed bounds for %s (%.5g ≥ %.5g) — keeping original",
                name, clamped_low, clamped_high,
            )
            updated[name] = spec
            continue

        # Only log if actually narrowed
        if clamped_low > spec["low"] or clamped_high < spec["high"]:
            LOGGER.info(
                "Sweep advisor narrowed %-30s [%.5g, %.5g] → [%.5g, %.5g]",
                name, spec["low"], spec["high"], clamped_low, clamped_high,
            )
            n_narrowed += 1

        updated[name] = {**spec, "low": clamped_low, "high": clamped_high}

    LOGGER.info("Sweep advisor narrowed %d / %d parameters", n_narrowed, len(current_search_space))
    return updated, reasoning
