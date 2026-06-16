"""Prompt templates for the Hermes 0DTE strategy discovery loop.
Two classes of parameters are optimized: direction thresholds and spread parameters.
All prompts instruct Hermes to respond in valid JSON."""

from __future__ import annotations


def build_initial_prompt(strategy_type: str, params: dict) -> str:
    return (
        f"You are optimizing InfiniteLoop's {strategy_type} 0DTE SPX strategy.\n"
        f"Current parameters: {params}\n"
        "Return valid JSON with keys reasoning, suggested_params, change_summary.\n"
        "Change only ONE parameter at a time."
    )


def build_iteration_prompt(params: dict, history: list[dict], best_scorecard: dict) -> str:
    return (
        f"Optimize this strategy one parameter at a time. Current parameters: {params}\n"
        f"Recent history: {history}\nBest scorecard: {best_scorecard}\n"
        "Allowed changes: gap_threshold_pct, orb_breakout_pct, delta_bias_threshold, neutral_band_pct, entry_hour, short_delta, spread_width_usd, profit_target_pct, stop_loss_pct.\n"
        "Never change forced_exit_hour, max_loss_pct, or daily_halt_pct.\n"
        "Return valid JSON with keys reasoning, param_to_change, new_value, change_summary."
    )


def build_convergence_check_prompt(history: list[dict]) -> str:
    return (
        f"Assess whether the strategy has converged based on history: {history}.\n"
        "Return valid JSON with keys converged, reasoning, suggestion."
    )
