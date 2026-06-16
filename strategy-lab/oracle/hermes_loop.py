"""
oracle/hermes_loop.py — Hermes optimization loop for Oracle.

Oracle's Hermes loop changes ONE direction threshold at a time and evaluates
the result using directional accuracy — never P&L.

Scope:
  - Optimizes: OracleParams fields listed in ORACLE_OPTIMIZABLE_PARAMS
  - Never touches: spread sizing, strike selection, exit timing
  - Metric: overall accuracy + per-class accuracy on in-sample (IS) data only
  - OOS data is locked until the loop converges

Cross-session persistence (via OracleRegistry — REQUIRED, never optional):
  - Every iteration is saved immediately after evaluation
  - On startup, the full tried-value map is loaded so Hermes never repeats
    known values, and sees which direction each parameter responded to
  - The best params ever found seed a fresh run if no seed is given
  - finish_run() always executes (try/finally), even on crash/stop

Robustness rules learned the hard way:
  - A malformed Hermes response is a PARSE failure, not a strategy failure.
    Parse failures get a corrective retry prompt and their own counter
    (MAX_PARSE_FAILURES) — they never count toward convergence patience,
    otherwise 8 bad JSON responses in a row would end the loop "converged"
    having tested nothing.
  - A duplicate proposal (value already tried for that param) skips the
    backtest entirely and tells Hermes what that value scored last time.
"""

from __future__ import annotations

import json
import logging
import sys
from copy import deepcopy
from pathlib import Path
from typing import Optional

import pandas as pd

# Add parent dir to path when running standalone
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))

from hermes.client import HermesClient
from .classifier import OracleStrategy, ORACLE_OPTIMIZABLE_PARAMS
from .backtest import run_oracle_backtest, OracleResults
from .records import IterationRecord  # canonical record — do not redefine here

LOGGER = logging.getLogger("infiniteloop.oracle.hermes_loop")

# ---------------------------------------------------------------------------
# Loop configuration
# ---------------------------------------------------------------------------
MAX_ITERATIONS = 60
HISTORY_WINDOW = 10          # recent session history to include in prompt
CROSS_SESSION_WINDOW = 30    # how many cross-session iterations to show Hermes
CONVERGENCE_PATIENCE = 8     # stop if no improvement for this many EVALUATED iterations
MIN_IMPROVEMENT = 0.002      # minimum accuracy improvement to count as progress (0.2%)
MAX_PARSE_FAILURES = 6       # consecutive unusable Hermes responses before aborting
TRIED_VALUES_PER_PARAM = 25  # cap per-param tried values shown in prompt


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

_RESPONSE_FORMAT = (
    "Change EXACTLY ONE parameter. Choose a value you have NOT tried before.\n"
    "Return ONLY valid JSON, no prose outside it:\n"
    '{"reasoning": "...", "param_to_change": "<name>", "new_value": <number>, '
    '"change_summary": "..."}\n'
)

_PARAM_GUIDE = (
    "Optimizable parameters (change ONE at a time):\n"
    "  gap_threshold_pct          — minimum gap% to signal direction (e.g. 0.0025)\n"
    "  neutral_band_pct           — gap% below which open is considered range-bound (e.g. 0.0020)\n"
    "  orb_breakout_pct           — how far price must close past ORB high/low (e.g. 0.0012)\n"
    "  delta_bias_threshold       — order-flow delta magnitude needed (e.g. 150.0)\n"
    "  vwap_slope_threshold       — minimum VWAP slope to count as trending (e.g. 0.00005)\n"
    "  vol_filter_high            — skip days when VIX > this (e.g. 32.0)\n"
    "  vol_filter_low             — skip days when VIX < this (e.g. 12.0)\n"
    "  prev_day_return_threshold  — prior-day |return| needed for tiebreaker (e.g. 0.003)\n"
    "  prev_day_vwap_threshold    — prior-day |close-vs-VWAP| deviation needed (e.g. 0.001)\n"
    "  min_confidence             — directional calls below this confidence are SKIPPED (e.g. 0.5)\n"
    "  min_score_separation       — |up-down| score gap below this becomes a NEUTRAL call (e.g. 0.05)\n\n"
    "NEVER change: forced_exit_hour, max_loss_pct, daily_halt_pct, entry_hour, "
    "short_delta, spread_width_usd, profit_target_pct, stop_loss_pct.\n\n"
)


def _tried_values_section(tried_values: dict[str, list[dict]]) -> str:
    """Compact per-param history: every value ever tested and what it scored."""
    if not tried_values:
        return ""
    trimmed = {
        param: entries[-TRIED_VALUES_PER_PARAM:]
        for param, entries in tried_values.items()
    }
    return (
        "\nVALUES ALREADY TESTED (per parameter, with the accuracy each produced — "
        "NEVER propose a value listed here; use the trend to pick a NEW value):\n"
        + json.dumps(trimmed, indent=1) + "\n"
    )


def _build_oracle_initial_prompt(
    params: dict,
    cross_session_history: list[dict],
    tried_values: dict[str, list[dict]],
    best_ever: dict | None,
) -> str:
    prior_section = ""
    if cross_session_history:
        prior_section = (
            f"\nPRIOR RUN HISTORY ({len(cross_session_history)} iterations across all past runs):\n"
            f"{json.dumps(cross_session_history[-CROSS_SESSION_WINDOW:], indent=1)}\n"
        )

    best_section = ""
    if best_ever:
        best_section = (
            "\nBEST PARAMS EVER FOUND (highest accuracy across all runs):\n"
            + json.dumps(best_ever, indent=1) + "\n"
        )

    return (
        "You are Oracle, an AI agent optimizing the direction classification component of "
        "an InfiniteLoop 0DTE SPX options trading system.\n\n"
        "Your ONLY job is to classify each trading day as UP, DOWN, or NEUTRAL.\n"
        "NEUTRAL means the market is expected to stay inside its daily expected move (+/-EM).\n"
        "EM is derived from VIX: EM = SPX_open x (VIX/100) x sqrt(1/252)\n\n"
        f"Current parameters:\n{json.dumps(params, indent=1)}\n"
        f"{prior_section}"
        f"{_tried_values_section(tried_values)}"
        f"{best_section}"
        f"{_PARAM_GUIDE}"
        f"{_RESPONSE_FORMAT}"
    )


def _build_oracle_iteration_prompt(
    params: dict,
    session_history: list[dict],
    best: dict,
    tried_values: dict[str, list[dict]],
) -> str:
    return (
        "Oracle direction optimization. Optimize directional accuracy for UP/DOWN/NEUTRAL.\n\n"
        f"Current parameters:\n{json.dumps(params, indent=1)}\n\n"
        f"This session — last {len(session_history)} iterations:\n"
        f"{json.dumps(session_history, indent=1)}\n"
        f"{_tried_values_section(tried_values)}"
        f"\nBest result so far:\n{json.dumps(best, indent=1)}\n\n"
        f"{_PARAM_GUIDE}"
        f"{_RESPONSE_FORMAT}"
    )


def _build_corrective_prompt(bad_response: str, params: dict) -> str:
    """Sent after a parse failure — show Hermes exactly what was wrong."""
    return (
        "Your previous response could not be used. It either changed multiple parameters, "
        "changed a forbidden parameter, or was not valid JSON.\n\n"
        f"Your previous response was:\n{bad_response[:1500]}\n\n"
        f"Current parameters:\n{json.dumps(params, indent=1)}\n\n"
        f"{_PARAM_GUIDE}"
        f"{_RESPONSE_FORMAT}"
    )


def _build_convergence_prompt(session_history: list[dict]) -> str:
    return (
        f"Assess whether Oracle has converged based on this session history:\n"
        f"{json.dumps(session_history, indent=1)}\n\n"
        "Return valid JSON:\n"
        '{"converged": true/false, "reasoning": "...", "suggestion": "..."}\n'
    )


# ---------------------------------------------------------------------------
# Param change enforcement
# ---------------------------------------------------------------------------

def _enforce_oracle_scope(payload: dict) -> dict:
    """Raises ValueError if Hermes tried to touch a forbidden param."""
    param_name = payload.get("param_to_change")
    if param_name not in ORACLE_OPTIMIZABLE_PARAMS:
        raise ValueError(
            f"Oracle Hermes attempted to change '{param_name}' which is outside Oracle's scope. "
            f"Allowed: {sorted(ORACLE_OPTIMIZABLE_PARAMS)}"
        )
    return payload


def _apply_change(strategy: OracleStrategy, payload: dict) -> OracleStrategy:
    """Apply a validated single-param change and return a new OracleStrategy."""
    _enforce_oracle_scope(payload)
    params = deepcopy(strategy.get_params())
    params[payload["param_to_change"]] = payload["new_value"]
    return OracleStrategy.from_params(params)


def _normalize_hermes_response(raw: dict, current_params: dict) -> dict | None:
    """
    Normalize Hermes response to a canonical {param_to_change, new_value} dict
    with new_value coerced to float. Returns None if unusable.
    """
    candidate: dict | None = None
    if "param_to_change" in raw and "new_value" in raw:
        candidate = {"param_to_change": raw["param_to_change"], "new_value": raw["new_value"]}
    else:
        suggested = raw.get("suggested_params")
        if isinstance(suggested, dict):
            changed = [
                k for k, v in suggested.items()
                if k in current_params and current_params[k] != v
            ]
            if len(changed) == 1:
                candidate = {"param_to_change": changed[0], "new_value": suggested[changed[0]]}

    if candidate is None:
        return None
    try:
        candidate["new_value"] = float(candidate["new_value"])
    except (TypeError, ValueError):
        return None
    return candidate


def _value_already_tried(
    param: str, value: float, tried_values: dict[str, list[dict]]
) -> dict | None:
    """Return the prior result dict if this exact value was already tested."""
    for entry in tried_values.get(param, []):
        prior = entry.get("value")
        if prior is not None and abs(float(prior) - value) < 1e-12:
            return entry
    return None


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_oracle_loop(
    features_is: pd.DataFrame,
    initial_strategy: Optional[OracleStrategy] = None,
    max_iterations: int = MAX_ITERATIONS,
    outcome_col: str = "outcome",
    on_iteration: Optional[callable] = None,
    oracle_registry=None,   # BaseOracleRegistry | None — injected for persistence
    run_notes: str = "",
) -> tuple[OracleStrategy, list[IterationRecord]]:
    """
    Run the Oracle Hermes optimization loop on in-sample features.

    Args:
        features_is:      In-sample feature DataFrame with 'outcome' column.
        initial_strategy: Starting strategy. If None and oracle_registry is set,
                          seeds from the best params ever found in the DB.
                          Falls back to OracleStrategy() defaults.
        max_iterations:   Maximum number of Hermes iterations.
        outcome_col:      Column name for the labeled outcome.
        on_iteration:     Optional callback(iteration, record, strategy) for live UI
                          updates. May raise StopIteration to stop the loop early.
        oracle_registry:  Registry for cross-session persistence (Postgres or SQLite
                          via open_oracle_registry()). If None, runs ephemeral —
                          allowed only for tests.
        run_notes:        Optional free-text notes stored with the run in the DB.

    Returns:
        (best_strategy, history)  — best OracleStrategy found and full iteration history.
    """
    hermes = HermesClient()
    if not hermes.is_available():
        raise RuntimeError(
            "Hermes (Ollama) is not running. Start Ollama and load hermes3 before running Oracle."
        )

    # ------------------------------------------------------------------
    # Load cross-session context from the registry
    # ------------------------------------------------------------------
    cross_session_history: list[dict] = []
    tried_values: dict[str, list[dict]] = {}
    best_ever_params: dict | None = None
    run_id: int | None = None

    if oracle_registry is not None:
        try:
            cross_session_history = oracle_registry.load_cross_session_history(
                limit=CROSS_SESSION_WINDOW
            )
            tried_values = oracle_registry.get_tried_values()
            best_ever_params = oracle_registry.get_best_params_ever()
            LOGGER.info(
                "Loaded cross-session context [%s]: %d prior iterations, %d tried-value entries, "
                "best_ever_params=%s",
                getattr(oracle_registry, "backend", "?"),
                len(cross_session_history),
                sum(len(v) for v in tried_values.values()),
                "found" if best_ever_params else "none",
            )
        except Exception as exc:
            LOGGER.warning("Could not load cross-session history (continuing without it): %s", exc)
    else:
        LOGGER.warning("run_oracle_loop called WITHOUT a registry — iterations will NOT persist")

    # Seed strategy: prefer explicit arg, then DB best, then defaults
    if initial_strategy is not None:
        current = initial_strategy
    elif best_ever_params is not None:
        LOGGER.info("Seeding Oracle from best-ever DB params")
        current = OracleStrategy.from_params(best_ever_params)
    else:
        current = OracleStrategy()

    # Create run record
    if oracle_registry is not None:
        try:
            run_id = oracle_registry.create_run(current.get_params(), notes=run_notes)
        except Exception as exc:
            LOGGER.error("Could not create oracle_run record (continuing without persistence): %s", exc)

    best = current
    history: list[IterationRecord] = []
    session_hermes_history: list[dict] = []

    # ------------------------------------------------------------------
    # Baseline run
    # ------------------------------------------------------------------
    LOGGER.info("Running Oracle baseline backtest...")
    baseline = run_oracle_backtest(features_is, current, outcome_col)
    best_accuracy = baseline.overall_accuracy
    best_results = baseline

    LOGGER.info(
        "Oracle baseline: accuracy=%.2f%% (UP=%.2f%% DOWN=%.2f%% NEUTRAL=%.2f%%)",
        best_accuracy * 100,
        baseline.up_accuracy * 100,
        baseline.down_accuracy * 100,
        baseline.neutral_accuracy * 100,
    )

    no_improvement_count = 0
    parse_failures = 0          # consecutive unusable Hermes responses
    corrective_payload: str | None = None  # last bad response, for the retry prompt
    stop_requested = False

    # ------------------------------------------------------------------
    # Main loop — finish_run is guaranteed via try/finally
    # ------------------------------------------------------------------
    try:
        for iteration in range(1, max_iterations + 1):
            LOGGER.info("Oracle iteration %d / %d", iteration, max_iterations)

            # --- 1. Ask Hermes for a single-param change -------------------
            if corrective_payload is not None:
                prompt = _build_corrective_prompt(corrective_payload, current.get_params())
            elif iteration == 1:
                prompt = _build_oracle_initial_prompt(
                    current.get_params(), cross_session_history, tried_values, best_ever_params
                )
            else:
                prompt = _build_oracle_iteration_prompt(
                    current.get_params(),
                    session_hermes_history[-HISTORY_WINDOW:],
                    {**best_results.summary_dict(), "params": best.get_params()},
                    tried_values,
                )

            try:
                raw_payload = hermes.generate_json(prompt)
            except Exception as exc:
                parse_failures += 1
                corrective_payload = str(exc)
                LOGGER.warning(
                    "Hermes call failed (%d/%d consecutive): %s",
                    parse_failures, MAX_PARSE_FAILURES, exc,
                )
                if parse_failures >= MAX_PARSE_FAILURES:
                    LOGGER.error("Aborting loop: %d consecutive Hermes failures", parse_failures)
                    break
                continue

            payload = _normalize_hermes_response(raw_payload, current.get_params())
            if payload is None:
                parse_failures += 1
                corrective_payload = json.dumps(raw_payload)[:1500]
                LOGGER.warning(
                    "Unusable Hermes response (%d/%d consecutive): %s",
                    parse_failures, MAX_PARSE_FAILURES, corrective_payload[:300],
                )
                if parse_failures >= MAX_PARSE_FAILURES:
                    LOGGER.error("Aborting loop: %d consecutive unusable responses", parse_failures)
                    break
                continue

            try:
                _enforce_oracle_scope(payload)
            except ValueError as exc:
                parse_failures += 1
                corrective_payload = json.dumps(raw_payload)[:1500]
                LOGGER.warning("Hermes change rejected (%d/%d): %s", parse_failures, MAX_PARSE_FAILURES, exc)
                if parse_failures >= MAX_PARSE_FAILURES:
                    break
                continue

            # Usable response — reset parse-failure tracking
            parse_failures = 0
            corrective_payload = None

            param = payload["param_to_change"]
            new_value = payload["new_value"]
            old_value = current.get_params().get(param)

            # --- 2. Duplicate check: don't burn a backtest on a known value ---
            prior = _value_already_tried(param, new_value, tried_values)
            if prior is not None:
                LOGGER.info(
                    "Duplicate proposal: %s=%s already tested (accuracy=%s) — informing Hermes",
                    param, new_value, prior.get("accuracy"),
                )
                session_hermes_history.append({
                    "iteration": iteration,
                    "param_changed": param,
                    "new_value": new_value,
                    "note": (
                        f"DUPLICATE — you already tested this value; it scored "
                        f"{prior.get('accuracy')} (accepted={prior.get('accepted')}). "
                        f"Propose a NEW value."
                    ),
                })
                no_improvement_count += 1
                if no_improvement_count >= CONVERGENCE_PATIENCE:
                    LOGGER.info("Oracle converged (patience exhausted on duplicates/rejections).")
                    break
                continue

            # --- 3. Evaluate candidate -------------------------------------
            candidate = _apply_change(current, payload)
            candidate_results = run_oracle_backtest(features_is, candidate, outcome_col)
            accepted = candidate_results.overall_accuracy > best_accuracy + MIN_IMPROVEMENT

            record = IterationRecord(
                iteration=iteration,
                param_changed=param,
                old_value=old_value,
                new_value=new_value,
                accuracy=candidate_results.overall_accuracy,
                up_accuracy=candidate_results.up_accuracy,
                down_accuracy=candidate_results.down_accuracy,
                neutral_accuracy=candidate_results.neutral_accuracy,
                skip_rate=candidate_results.skip_rate,
                accepted=accepted,
                hermes_reasoning=str(raw_payload.get("reasoning", "")),
                run_id=run_id,
            )
            history.append(record)

            session_hermes_history.append({
                "iteration": iteration,
                "param_changed": param,
                "old_value": old_value,
                "new_value": new_value,
                "accuracy": round(candidate_results.overall_accuracy, 4),
                "up_accuracy": round(candidate_results.up_accuracy, 4),
                "down_accuracy": round(candidate_results.down_accuracy, 4),
                "neutral_accuracy": round(candidate_results.neutral_accuracy, 4),
                "accepted": accepted,
            })

            # Keep the in-memory tried-value map current so duplicates within
            # this session are caught even if DB writes fail.
            tried_values.setdefault(param, []).append({
                "value": new_value,
                "accuracy": round(candidate_results.overall_accuracy, 4),
                "accepted": accepted,
            })

            # --- 4. Persist immediately ------------------------------------
            if oracle_registry is not None and run_id is not None:
                try:
                    oracle_registry.save_iteration(
                        run_id=run_id,
                        iteration=iteration,
                        param_changed=param,
                        old_value=old_value,
                        new_value=new_value,
                        overall_accuracy=candidate_results.overall_accuracy,
                        up_accuracy=candidate_results.up_accuracy,
                        down_accuracy=candidate_results.down_accuracy,
                        neutral_accuracy=candidate_results.neutral_accuracy,
                        skip_rate=candidate_results.skip_rate,
                        accepted=accepted,
                        hermes_reasoning=record.hermes_reasoning,
                        full_params=candidate.get_params(),
                    )
                except Exception as exc:
                    LOGGER.error("Failed to persist iteration %d: %s", iteration, exc)

            # --- 5. Accept / reject -----------------------------------------
            if accepted:
                LOGGER.info(
                    "Accepted: %s %s -> %s | accuracy %.2f%% -> %.2f%%",
                    param, old_value, new_value,
                    best_accuracy * 100, candidate_results.overall_accuracy * 100,
                )
                current = candidate
                best = candidate
                best_accuracy = candidate_results.overall_accuracy
                best_results = candidate_results
                no_improvement_count = 0
            else:
                LOGGER.info(
                    "Rejected: %s %s -> %s | accuracy %.2f%% (best=%.2f%%)",
                    param, old_value, new_value,
                    candidate_results.overall_accuracy * 100, best_accuracy * 100,
                )
                no_improvement_count += 1

            # --- 6. Notify UI -------------------------------------------------
            if on_iteration:
                try:
                    on_iteration(iteration, record, best)
                except StopIteration:
                    LOGGER.info("Stop requested via on_iteration callback")
                    stop_requested = True
                    break

            # --- 7. Convergence checks ----------------------------------------
            if no_improvement_count >= CONVERGENCE_PATIENCE:
                LOGGER.info(
                    "Oracle converged after %d iterations (no improvement for %d consecutive).",
                    iteration, CONVERGENCE_PATIENCE,
                )
                break

            if iteration % 15 == 0:
                try:
                    conv_response = hermes.generate_json(
                        _build_convergence_prompt(session_hermes_history[-HISTORY_WINDOW:])
                    )
                    if conv_response.get("converged") is True:
                        LOGGER.info(
                            "Hermes reports Oracle has converged: %s",
                            conv_response.get("reasoning"),
                        )
                        break
                except Exception as exc:
                    LOGGER.warning("Skipping convergence check due to Hermes error: %s", exc)

    finally:
        # ------------------------------------------------------------------
        # ALWAYS close the run record — even on crash, stop, or abort
        # ------------------------------------------------------------------
        if oracle_registry is not None and run_id is not None:
            try:
                oracle_registry.finish_run(
                    run_id=run_id,
                    best_params=best.get_params(),
                    best_accuracy=best_accuracy,
                    total_iterations=len(history),
                )
            except Exception as exc:
                LOGGER.error("Failed to finish oracle_run record: %s", exc)

    if stop_requested:
        raise StopIteration("User requested stop")  # preserved contract with server.py

    LOGGER.info(
        "Oracle loop complete. Best accuracy: %.2f%% | Params: %s",
        best_accuracy * 100, best.get_params(),
    )
    return best, history


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    LOGGER.info("Oracle Hermes loop CLI — use server.py for the full pipeline")
    LOGGER.info(
        "Import and call run_oracle_loop(features_is, oracle_registry=open_oracle_registry()[1]) to start."
    )
