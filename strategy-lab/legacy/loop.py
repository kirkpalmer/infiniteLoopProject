"""Main 0DTE strategy discovery loop for InfiniteLoop Phase 1.
Orchestrates: load features -> Hermes direction+spread optimization -> backtest -> score -> iterate.
Run this script to discover and validate a new 0DTE spread strategy."""

from __future__ import annotations

import argparse
import json
import logging
import os
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pandas as pd

from backtest.engine import backtest_direction
from backtest.metrics import score_results
from backtest.spread_engine import backtest_spreads
from backtest.validator import Validator
from dashboard import export_html, render
from data.loader import load_day_features, split_train_oos
from data.market_data import MarketDataClient
from hermes.client import HermesClient
from hermes.parser import validate_change_payload
from hermes.prompts import build_convergence_check_prompt, build_initial_prompt, build_iteration_prompt
from strategy.orb_direction import ORBDirectionParams, ORBDirectionStrategy
from strategy.packager import pack_strategy
from strategy.registry import StrategyRegistry
from strategy.oracle_registry import OracleRegistry

LOGGER = logging.getLogger("infiniteloop.loop")
MAX_ITERATIONS = 50
HISTORY_WINDOW = 10
CONVERGENCE_CHECK_EVERY = 10


def _slice_by_months(frame, months: int):
    if frame.empty:
        return frame
    end = frame.index.max()
    start = end - pd.DateOffset(months=months)
    return frame.loc[frame.index >= start].copy()


def _apply_param_change(strategy: ORBDirectionStrategy, payload: dict) -> ORBDirectionStrategy:
    validate_change_payload(payload)
    params = deepcopy(strategy.get_params())
    params[payload["param_to_change"]] = payload["new_value"]
    return ORBDirectionStrategy.from_params(params)


def _normalize_hermes_change(payload: dict, current_params: dict) -> dict | None:
    """Normalize supported Hermes response shapes into param_to_change/new_value."""

    if "param_to_change" in payload and "new_value" in payload:
        return {"param_to_change": payload["param_to_change"], "new_value": payload["new_value"]}

    suggested = payload.get("suggested_params")
    if isinstance(suggested, dict):
        summary = payload.get("change_summary")
        if isinstance(summary, dict):
            changed_name = summary.get("param_changed")
            if changed_name in suggested:
                return {"param_to_change": changed_name, "new_value": suggested[changed_name]}

        # Fallback: detect exactly one changed key vs current params.
        changed_keys = [key for key, value in suggested.items() if key in current_params and current_params[key] != value]
        if len(changed_keys) == 1:
            key = changed_keys[0]
            return {"param_to_change": key, "new_value": suggested[key]}

    return None


def run_loop(args: argparse.Namespace) -> None:
    """Run the Phase 1 discovery loop."""

    hermes = HermesClient()
    if not hermes.is_available():
        raise RuntimeError("Hermes is not available - start Ollama and load hermes3")

    client = MarketDataClient()
    LOGGER.info(client.coverage_report())
    features = load_day_features(client, args.start_date, args.end_date)
    train_df, oos_df = split_train_oos(features)
    spx_daily = client.get_spx_daily(args.start_date, args.end_date)
    risk_free_rate = client.get_risk_free_rate(args.end_date)

    strategy = ORBDirectionStrategy.from_params(args.seed_params) if args.seed_params else ORBDirectionStrategy()
    registry = None
    oracle_registry = None
    if not args.dry_run:
        registry = StrategyRegistry()
        registry.initialize_schema()
        try:
            oracle_registry = OracleRegistry()
            LOGGER.info("OracleRegistry connected — iterations will be persisted to DB")
        except Exception as exc:
            LOGGER.warning("OracleRegistry unavailable (dry-run or no DATABASE_URL): %s", exc)
    validator = Validator(None, None, spx_daily, client.vix, risk_free_rate=risk_free_rate)

    history: list[dict] = []
    best_scorecard = None
    best_params = strategy.get_params()
    logs_dir = Path(__file__).resolve().parent / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    for iteration in range(1, args.iterations + 1):
        tier1_df = _slice_by_months(train_df, 6)
        tier2_df = _slice_by_months(train_df, 24)
        tier3_df = train_df

        for tier, tier_df in [(1, tier1_df), (2, tier2_df), (3, tier3_df)]:
            direction_results = backtest_direction(tier_df, strategy)
            spread_results = backtest_spreads(direction_results, spx_daily, client.vix, strategy.get_spread_params(), risk_free_rate)
            scorecard = score_results(direction_results, spread_results)
            history.append({"iter": iteration, "tier": tier, "params": strategy.get_params(), "scorecard": scorecard.to_dict()})

            render(
                scorecard=scorecard,
                spread_results=spread_results,
                direction_results=direction_results,
                strategy_params=strategy.get_params(),
                iteration=iteration,
                tier=tier,
                history=history,
            )

            if tier >= 1:
                timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
                html_path = logs_dir / f"dashboard_{timestamp}_iter{iteration}_tier{tier}.html"
                export_html(
                    scorecard=scorecard,
                    spread_results=spread_results,
                    history=history,
                    output_path=str(html_path),
                )
            if best_scorecard is None or scorecard.sharpe_ratio > best_scorecard.sharpe_ratio:
                best_scorecard = scorecard
                best_params = strategy.get_params()

            LOGGER.info("Iteration %d tier %d: %s", iteration, tier, scorecard.summary_str())

            if tier == 1 and not (scorecard.direction_accuracy >= 0.50 and scorecard.profit_factor >= 1.20):
                break
            if tier == 2 and not scorecard.passed:
                break
            if tier == 3 and scorecard.passed:
                break

        if iteration % CONVERGENCE_CHECK_EVERY == 0:
            try:
                conv_payload = hermes.generate_json(build_convergence_check_prompt(history[-HISTORY_WINDOW:]))
                if conv_payload.get("converged"):
                    break
            except Exception as exc:
                LOGGER.warning("Skipping convergence check due to Hermes error: %s", exc)

        prompt = build_initial_prompt(strategy.get_name(), strategy.get_params()) if iteration == 1 else build_iteration_prompt(strategy.get_params(), history[-HISTORY_WINDOW:], best_scorecard.to_dict() if best_scorecard else {})
        try:
            response = hermes.generate_json(prompt)
        except Exception as exc:
            LOGGER.warning("Skipping iteration due to Hermes error: %s", exc)
            continue

        change = _normalize_hermes_change(response, strategy.get_params())
        if change is None:
            LOGGER.warning("Hermes response did not contain a usable single-parameter change; skipping iteration")
            continue

        try:
            strategy = _apply_param_change(strategy, change)
        except ValueError as exc:
            LOGGER.warning("Skipping invalid Hermes change: %s", exc)

    validation = validator.validate(train_df, oos_df, strategy, strategy.get_name())
    LOGGER.info(validation.summary_str())
    if not args.dry_run and registry is not None:
        registry.save_strategy(strategy.get_name(), 1, "ORB direction seed strategy", strategy.get_params(), best_scorecard or scorecard, status="active" if validation.final_verdict else "candidate")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="InfiniteLoop Phase 1 discovery loop")
    parser.add_argument("--iterations", type=int, default=MAX_ITERATIONS)
    parser.add_argument("--seed-params", type=str, default="")
    parser.add_argument("--strategy", type=str, default="orb_direction")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--start-date", type=str, default="2020-01-01")
    parser.add_argument("--end-date", type=str, default="2026-06-05")
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = build_arg_parser()
    args = parser.parse_args()
    if args.seed_params:
        args.seed_params = json.loads(args.seed_params)
    run_loop(args)


if __name__ == "__main__":
    main()
