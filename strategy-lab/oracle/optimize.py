"""
oracle/optimize.py — Optuna parameter sweep for Oracle's direction thresholds.

This replaces LLM-guided coordinate ascent for NUMERIC parameter tuning.
Optuna's TPE sampler models the parameter landscape from all prior trials and
samples where improvement is most probable — the "bigger leaps based on
previous iterations" idea, mechanized. Hermes remains the strategist for
STRUCTURAL changes (new features, rule changes); the backtest engine is the
only tester.

Discipline (non-negotiable, mirrors the Hermes loop):
  - The sweep only ever sees in-sample features. OOS stays locked.
  - Guardrails stop the optimizer from cheating:
      * skip_rate must stay <= MAX_SKIP_RATE (else it learns to skip
        everything and scores on a tiny easy subset)
      * at least MIN_ACTIVE_DAYS active classification days
  - Objective is MACRO accuracy (mean of UP/DOWN/NEUTRAL accuracy), not raw
    overall accuracy — raw accuracy rewards ignoring minority classes.
  - Every trial is persisted to the registry (oracle_iterations) so the
    dashboard, the landscape endpoint, and Hermes chat context all see it.

Usage:
    from oracle.optimize import run_oracle_sweep
    result = run_oracle_sweep(features_is, n_trials=300, oracle_registry=reg)

CLI (from strategy-lab/):
    python -m oracle.optimize --trials 300
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))

from .classifier import OracleStrategy
from .backtest import run_oracle_backtest, OracleResults
from .records import IterationRecord

LOGGER = logging.getLogger("infiniteloop.oracle.optimize")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_TRIALS = 300
MAX_SKIP_RATE = 0.40      # optimizer may not skip more than 40% of days
MIN_ACTIVE_DAYS = 200     # mirrors the 200-trade minimum rule
FAILED_TRIAL_SCORE = -1.0  # score for trials that violate guardrails

# Trading-aligned objective guardrails. The objective is DIRECTIONAL-CALL
# PRECISION ("when Oracle calls UP/DOWN, how often is it right?") because
# UP/DOWN calls are the only days we trade — NEUTRAL calls stand aside.
# Without call-count floors the optimizer gets precise by barely trading.
MIN_DIRECTIONAL_CALL_RATE = 0.08   # ≥8% of all days must produce a trade signal
MIN_CALLS_PER_DIRECTION_RATE = 0.01  # each of UP and DOWN ≥1% of days (no one-class collapse)

# Search space bounds. neutral_band_pct is searched as a FRACTION of
# gap_threshold_pct so the logical constraint neutral_band < gap_threshold
# always holds by construction.
SEARCH_SPACE: dict[str, dict] = {
    "gap_threshold_pct":         {"low": 0.0005, "high": 0.010,  "log": True},
    "neutral_band_frac":         {"low": 0.10,   "high": 0.95,   "log": False},  # × gap_threshold_pct
    "orb_breakout_pct":          {"low": 0.0003, "high": 0.005,  "log": True},
    "delta_bias_threshold":      {"low": 50.0,   "high": 600.0,  "log": False},
    "vwap_slope_threshold":      {"low": 1e-5,   "high": 5e-4,   "log": True},
    "vol_filter_high":           {"low": 20.0,   "high": 45.0,   "log": False},
    "vol_filter_low":            {"low": 9.0,    "high": 16.0,   "log": False},
    "prev_day_return_threshold": {"low": 0.001,  "high": 0.010,  "log": True},
    "prev_day_vwap_threshold":   {"low": 0.0002, "high": 0.005,  "log": True},
    # Conviction gates — OOS evidence: directional calls below ~0.5 confidence
    # scored 7.7%. The MAX_SKIP_RATE guardrail stops the optimizer from
    # abstaining its way to a high score.
    "min_confidence":            {"low": 0.0,    "high": 0.65,   "log": False},
    "min_score_separation":      {"low": 0.0,    "high": 0.20,   "log": False},
}

# Params drawn directly from SEARCH_SPACE (gap/neutral_band handled specially)
_DIRECT_PARAMS = (
    "orb_breakout_pct", "delta_bias_threshold", "vwap_slope_threshold",
    "vol_filter_high", "vol_filter_low",
    "prev_day_return_threshold", "prev_day_vwap_threshold",
    "min_confidence", "min_score_separation",
)


@dataclass
class SweepResult:
    best_params: dict
    best_score: float                   # directional-call precision of the best trial
    best_overall_accuracy: float
    best_results: Optional[OracleResults]
    n_trials: int
    n_failed: int                       # trials that violated guardrails
    param_importances: dict = field(default_factory=dict)

    def summary_str(self) -> str:
        return (
            f"Sweep complete: {self.n_trials} trials ({self.n_failed} failed guardrails) | "
            f"best directional precision={self.best_score:.2%} overall={self.best_overall_accuracy:.2%}"
        )


def _suggest_params(trial, search_space: dict = SEARCH_SPACE) -> dict:
    """Draw one parameter set from the (possibly advisor-narrowed) search space."""
    gap = trial.suggest_float("gap_threshold_pct", **search_space["gap_threshold_pct"])
    frac = trial.suggest_float("neutral_band_frac", **search_space["neutral_band_frac"])
    params = {
        "gap_threshold_pct": gap,
        "neutral_band_pct": gap * frac,   # constraint holds by construction
    }
    for name in _DIRECT_PARAMS:
        params[name] = trial.suggest_float(name, **search_space[name])
    return params


def _build_optuna_distributions(search_space: dict = SEARCH_SPACE) -> dict:
    """Build Optuna distribution objects matching a search space dict."""
    import optuna
    return {
        name: optuna.distributions.FloatDistribution(
            spec["low"], spec["high"], log=spec.get("log", False)
        )
        for name, spec in search_space.items()
    }


def _inject_history_as_trials(study, registry, search_space: dict = SEARCH_SPACE) -> int:
    """
    Load all past oracle_iterations from the registry and inject them as COMPLETED
    Optuna trials. TPE treats these as known data points and won't re-sample
    regions it already understands, spending all new trial budget on genuinely
    unexplored territory.

    Returns the number of trials successfully injected.
    """
    if registry is None:
        return 0

    try:
        history = registry.get_all_completed_trials(min_accuracy=0.40)
    except Exception as exc:
        LOGGER.warning("Could not load history for Optuna seeding: %s", exc)
        return 0

    if not history:
        return 0

    try:
        import optuna
        from optuna.trial import FrozenTrial, TrialState
        from datetime import datetime, timezone
    except ImportError:
        LOGGER.warning("Optuna FrozenTrial API unavailable — skipping history injection")
        return 0

    dists = _build_optuna_distributions(search_space)
    now = datetime.now(timezone.utc)
    injected = 0

    for i, (params, score) in enumerate(history):
        seed = _seed_trial_from_params(params)
        if seed is None:
            continue

        # All search-space keys must be present and inside distribution bounds
        valid = True
        for name, dist in dists.items():
            v = seed.get(name)
            if v is None or v < dist.low or v > dist.high:
                valid = False
                break
        if not valid:
            continue

        try:
            trial = FrozenTrial(
                number=i,
                trial_id=i,
                state=TrialState.COMPLETE,
                value=float(score),
                values=None,
                datetime_start=now,
                datetime_complete=now,
                params=seed,
                distributions=dists,
                intermediate_values={},
                system_attrs={},
                user_attrs={"source": "historical_db"},
                fail_reason=None,
            )
            study.add_trial(trial)
            injected += 1
        except Exception as exc:
            LOGGER.debug("Could not inject historical trial %d: %s", i, exc)

    LOGGER.info(
        "Injected %d / %d historical trials — TPE will explore outside these known regions",
        injected, len(history),
    )
    return injected


def _seed_trial_from_params(params: dict) -> dict | None:
    """Invert a real param dict into search-space coordinates for enqueue_trial."""
    from .classifier import OracleParams
    defaults = OracleParams().to_dict()
    try:
        gap = float(params["gap_threshold_pct"])
        frac = float(params["neutral_band_pct"]) / gap if gap > 0 else 0.5
        seed = {"gap_threshold_pct": gap, "neutral_band_frac": frac}
        for name in _DIRECT_PARAMS:
            # Older persisted params may predate newer knobs — fall back to defaults
            seed[name] = float(params.get(name, defaults.get(name, SEARCH_SPACE[name]["low"])))
        # Clamp everything inside the search bounds or Optuna will reject the trial
        for name, value in seed.items():
            space = SEARCH_SPACE[name]
            seed[name] = min(max(value, space["low"]), space["high"])
        return seed
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def macro_accuracy(results: OracleResults) -> float:
    """Mean of per-class accuracy — the sweep objective."""
    return (results.up_accuracy + results.down_accuracy + results.neutral_accuracy) / 3.0


def run_oracle_sweep(
    features_is: pd.DataFrame,
    n_trials: int = DEFAULT_TRIALS,
    outcome_col: str = "outcome",
    oracle_registry=None,          # BaseOracleRegistry | None
    on_trial: Optional[Callable] = None,   # callback(trial_num, record, best_strategy)
    seed_params: Optional[dict] = None,    # falls back to registry best-ever
    run_notes: str = "optuna sweep",
    min_active_days: int = MIN_ACTIVE_DAYS,
    hermes_client=None,            # HermesClient | None — enables pre-sweep bounds analysis
) -> SweepResult:
    """
    Run an Optuna TPE sweep over Oracle's direction thresholds on IS data only.

    Every trial is persisted to the registry. `on_trial` may raise StopIteration
    to stop the sweep early (the study finishes the current trial and stops).
    """
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    # Adapt the active-days floor to small datasets (tests / short histories)
    effective_min_days = min(min_active_days, max(int(len(features_is) * 0.5), 1))
    if effective_min_days < min_active_days:
        LOGGER.warning(
            "IS set has only %d rows — lowering active-day floor to %d for this sweep",
            len(features_is), effective_min_days,
        )

    # ------------------------------------------------------------------
    # Phase 0: Hermes pre-sweep analysis — ask Hermes to narrow the
    # search space based on what the full optimization history shows.
    # Falls back to SEARCH_SPACE if Hermes is unavailable or history
    # is too thin.
    # ------------------------------------------------------------------
    from .sweep_advisor import get_hermes_sweep_bounds
    active_search_space, advisor_reasoning = get_hermes_sweep_bounds(
        registry=oracle_registry,
        hermes_client=hermes_client,
        current_search_space=SEARCH_SPACE,
        n_top=20,
    )
    if advisor_reasoning:
        LOGGER.info("Sweep advisor active — bounds may be narrowed from history analysis")

    run_id: int | None = None
    if oracle_registry is not None:
        try:
            if seed_params is None:
                seed_params = oracle_registry.get_best_params_ever()
            notes_with_advisor = (
                f"{run_notes} | advisor: {advisor_reasoning[:120]}"
                if advisor_reasoning else run_notes
            )
            run_id = oracle_registry.create_run(seed_params or {}, notes=notes_with_advisor)
        except Exception as exc:
            LOGGER.error("Registry unavailable for sweep (continuing without persistence): %s", exc)

    state = {
        "best_macro": float("-inf"),
        "best_overall": 0.0,
        "best_params": None,
        "best_results": None,
        "n_failed": 0,
        "stop": False,
    }

    total_days = max(len(features_is), 1)
    min_dir_calls = max(int(total_days * MIN_DIRECTIONAL_CALL_RATE), 8)
    min_each_dir = max(int(total_days * MIN_CALLS_PER_DIRECTION_RATE), 3)

    def objective(trial) -> float:
        params = _suggest_params(trial, active_search_space)
        strategy = OracleStrategy.from_params(params)
        results = run_oracle_backtest(features_is, strategy, outcome_col)

        active_days = results.trade_days
        guardrail_ok = (
            results.skip_rate <= MAX_SKIP_RATE
            and active_days >= effective_min_days
            and results.directional_calls >= min_dir_calls
            and min(results.up_calls, results.down_calls) >= min_each_dir
        )
        # OBJECTIVE: precision of directional calls — the trade-signal hit rate.
        score = results.directional_precision if guardrail_ok else FAILED_TRIAL_SCORE
        if not guardrail_ok:
            state["n_failed"] += 1

        is_new_best = guardrail_ok and score > state["best_macro"]
        if is_new_best:
            state["best_macro"] = score
            state["best_overall"] = results.overall_accuracy
            state["best_params"] = strategy.get_params()
            state["best_results"] = results

        reasoning = (
            f"optuna trial {trial.number}: dir_precision={score:.4f} "
            f"(UP {results.up_precision:.2f} on {results.up_calls} calls, "
            f"DOWN {results.down_precision:.2f} on {results.down_calls} calls) "
            f"overall={results.overall_accuracy:.4f} skip={results.skip_rate:.2%} active={active_days}"
            + ("" if guardrail_ok else " [GUARDRAIL VIOLATION]")
        )

        # Persist every trial — same table the Hermes loop uses
        if oracle_registry is not None and run_id is not None:
            try:
                oracle_registry.save_iteration(
                    run_id=run_id,
                    iteration=trial.number + 1,
                    param_changed="sweep",
                    old_value=None,
                    new_value=float(trial.number + 1),
                    overall_accuracy=results.overall_accuracy,
                    up_accuracy=results.up_accuracy,
                    down_accuracy=results.down_accuracy,
                    neutral_accuracy=results.neutral_accuracy,
                    skip_rate=results.skip_rate,
                    accepted=is_new_best,
                    hermes_reasoning=reasoning,
                    full_params=strategy.get_params(),
                )
            except Exception as exc:
                LOGGER.error("Failed to persist sweep trial %d: %s", trial.number, exc)

        if on_trial is not None:
            record = IterationRecord(
                iteration=trial.number + 1,
                param_changed="sweep",
                old_value=None,
                new_value=trial.number + 1,
                accuracy=results.overall_accuracy,
                up_accuracy=results.up_accuracy,
                down_accuracy=results.down_accuracy,
                neutral_accuracy=results.neutral_accuracy,
                skip_rate=results.skip_rate,
                accepted=is_new_best,
                hermes_reasoning=reasoning,
                run_id=run_id,
            )
            best_strategy = (
                OracleStrategy.from_params(state["best_params"])
                if state["best_params"] else strategy
            )
            try:
                on_trial(trial.number + 1, record, best_strategy)
            except StopIteration:
                state["stop"] = True
                trial.study.stop()

        return score

    sampler = optuna.samplers.TPESampler(multivariate=True, seed=None)
    study = optuna.create_study(direction="maximize", sampler=sampler)

    # ------------------------------------------------------------------
    # Phase 1: Inject ALL historical trials as completed Optuna trials.
    # TPE learns the full landscape from prior sessions immediately —
    # no budget wasted re-testing known combinations.
    # ------------------------------------------------------------------
    n_injected = _inject_history_as_trials(study, oracle_registry, active_search_space)

    # ------------------------------------------------------------------
    # Phase 2: Enqueue the single best-known params as the first NEW
    # trial so the sweep starts from the best discovered so far.
    # (Skip if it was already injected above.)
    # ------------------------------------------------------------------
    if seed_params and n_injected == 0:
        seed = _seed_trial_from_params(seed_params)
        if seed:
            study.enqueue_trial(seed)
            LOGGER.info("Sweep seeded from best-known params (no history to inject)")
    elif seed_params:
        LOGGER.info("History injected (%d trials) — best-known params already included", n_injected)

    LOGGER.info("Starting Optuna sweep: %d trials over %d IS rows", n_trials, len(features_is))
    study.optimize(objective, n_trials=n_trials)

    # Parameter importances (fANOVA needs scikit-learn; degrade gracefully)
    importances: dict = {}
    try:
        importances = {
            k: round(v, 4)
            for k, v in optuna.importance.get_param_importances(study).items()
        }
    except Exception as exc:
        LOGGER.warning("Could not compute param importances: %s", exc)

    completed = len(study.trials)
    result = SweepResult(
        best_params=state["best_params"] or (seed_params or OracleStrategy().get_params()),
        best_score=state["best_macro"] if state["best_params"] else 0.0,
        best_overall_accuracy=state["best_overall"],
        best_results=state["best_results"],
        n_trials=completed,
        n_failed=state["n_failed"],
        param_importances=importances,
    )

    if oracle_registry is not None and run_id is not None:
        try:
            oracle_registry.finish_run(
                run_id=run_id,
                best_params=result.best_params,
                best_accuracy=result.best_overall_accuracy,
                total_iterations=completed,
            )
        except Exception as exc:
            LOGGER.error("Failed to finish sweep run record: %s", exc)

    LOGGER.info(result.summary_str())
    if importances:
        LOGGER.info("Param importances: %s", importances)
    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    import config  # noqa: F401 — loads .env
    from data.market_data import MarketDataClient
    from data.loader import load_day_features
    from oracle.outcomes import label_outcomes, merge_outcomes_into_features
    from strategy.oracle_registry import open_oracle_registry

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Oracle Optuna threshold sweep")
    parser.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    parser.add_argument("--start-date", type=str, default="2022-01-01")
    parser.add_argument("--end-date", type=str, default="2026-06-06")
    args = parser.parse_args()

    client = MarketDataClient()
    features_raw = load_day_features(client, args.start_date, args.end_date)
    spx_daily = client.get_spx_daily(args.start_date, args.end_date)
    outcomes = label_outcomes(spx_daily, client.vix)
    features = merge_outcomes_into_features(features_raw, outcomes)

    split_idx = int(len(features) * 0.80)     # OOS 20% stays locked
    features_is = features.iloc[:split_idx].copy()

    backend, registry = open_oracle_registry()
    LOGGER.info("Registry backend: %s | IS rows: %d", backend, len(features_is))

    sweep = run_oracle_sweep(features_is, n_trials=args.trials, oracle_registry=registry)
    print(sweep.summary_str())
    print("Best params:", sweep.best_params)
    if sweep.param_importances:
        print("Param importances:", sweep.param_importances)
