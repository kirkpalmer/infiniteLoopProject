"""
oracle/report.py — Plain-text/markdown status report generator.

Produces a single downloadable report covering everything needed to review
Oracle's state outside the dashboard: current params, IS/OOS performance,
eligibility gate, walk-forward folds, confidence buckets, per-day OOS signals,
and recent optimization runs. Built for handoff into strategist sessions
(Claude/Cowork) without copy-pasting dashboard panels.
"""

from __future__ import annotations

import json
from datetime import datetime

from .backtest import OracleResults

DIRECTION_CLASSES = ("UP", "DOWN", "NEUTRAL")


def _pct(v) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def _results_block(title: str, r: OracleResults) -> list[str]:
    lines = [
        f"## {title}",
        "",
        f"- **Directional precision (trade-signal hit rate): {_pct(r.directional_precision)} "
        f"on {r.directional_calls} UP/DOWN calls** — "
        f"UP {_pct(r.up_precision)} ({r.up_calls} calls) | DOWN {_pct(r.down_precision)} ({r.down_calls} calls)",
        f"- Overall accuracy (incl. NEUTRAL calls): {_pct(r.overall_accuracy)} on {r.trade_days} active days ({r.total_days} total)",
        f"- Recall — UP: {_pct(r.up_accuracy)} ({r.up_count} days) | DOWN: {_pct(r.down_accuracy)} ({r.down_count} days) | NEUTRAL: {_pct(r.neutral_accuracy)} ({r.neutral_count} days)",
        f"- Skip rate: {_pct(r.skip_rate)}"
        + (f" — {', '.join(f'{k}: {v}' for k, v in r.skip_reasons.items())}" if r.skip_reasons else ""),
        f"- Avg confidence: {r.avg_confidence:.2f}",
        "",
        "Confusion matrix (rows = actual, cols = predicted):",
        "",
        "| actual \\ predicted | UP | DOWN | NEUTRAL |",
        "|---|---|---|---|",
    ]
    for actual in DIRECTION_CLASSES:
        row = r.confusion.get(actual, {})
        lines.append(
            f"| {actual} | {row.get('UP', 0)} | {row.get('DOWN', 0)} | {row.get('NEUTRAL', 0)} |"
        )
    lines.append("")
    return lines


def build_report(
    params: dict,
    is_results: OracleResults,
    oos_results: OracleResults | None,
    eligibility: dict | None,
    walkforward: dict | None,
    days_payload: dict | None,
    run_summary: list[dict] | None,
    last_sweep: dict | None,
    registry_backend: str,
    is_rows: int,
    oos_rows: int,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines: list[str] = [
        "# InfiniteLoop — Oracle Status Report",
        "",
        f"Generated: {now} | Registry: {registry_backend} | IS rows: {is_rows} | OOS rows (locked): {oos_rows}",
        "",
        "## Current Parameters",
        "",
        "```json",
        json.dumps(params, indent=2),
        "```",
        "",
    ]

    lines += _results_block("In-Sample Performance", is_results)

    if oos_results is not None:
        lines += _results_block("Out-of-Sample Performance (locked 20%)", oos_results)
        drift = abs(is_results.overall_accuracy - oos_results.overall_accuracy)
        lines += [
            f"**IS/OOS drift: {_pct(drift)}** — {'PASS' if drift < 0.10 else 'FAIL'} (<10% required)",
            "",
        ]

    if eligibility:
        lines += [
            "## Eligibility Gate",
            "",
            f"**{'ELIGIBLE FOR PAPER TRADING' if eligibility.get('passed') else 'CANDIDATE — criteria failing'}**"
            f" ({eligibility.get('score', '?')} passing)",
            "",
            "| Criterion | Value | Threshold | Status |",
            "|---|---|---|---|",
        ]
        for c in eligibility.get("criteria", []):
            op = ">=" if c.get("direction") == "above" else "<="
            lines.append(
                f"| {c.get('description', c.get('name'))} | {c.get('value')} | {op} {c.get('threshold')} | "
                f"{'PASS' if c.get('passed') else 'FAIL'} |"
            )
        lines.append("")

    if walkforward:
        lines += [
            "## Walk-Forward Validation",
            "",
            f"**Verdict: {walkforward.get('verdict')}** — mean {_pct(walkforward.get('mean_accuracy'))} "
            f"± {_pct(walkforward.get('std_accuracy'))} "
            f"(min {_pct(walkforward.get('min_accuracy'))}, max {_pct(walkforward.get('max_accuracy'))}) | "
            f"macro {_pct(walkforward.get('mean_macro'))} | "
            f"{walkforward.get('total_trade_days')} active days | "
            f"{walkforward.get('thin_folds')} thin fold(s) excluded",
            "",
            "| Fold | Period | Days | Active | Skip | Accuracy | Macro | UP | DOWN | NEUTRAL |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for f in walkforward.get("folds", []):
            thin = " ⚠" if f.get("thin") else ""
            lines.append(
                f"| {f['fold']}{thin} | {f['start']} → {f['end']} | {f['total_days']} | {f['trade_days']} | "
                f"{_pct(f['skip_rate'])} | {_pct(f['accuracy'])} | {_pct(f['macro'])} | "
                f"{_pct(f['up'])} | {_pct(f['down'])} | {_pct(f['neutral'])} |"
            )
        lines.append("")

    if days_payload:
        buckets = days_payload.get("confidence_buckets") or []
        if buckets:
            lines += ["## Accuracy by Confidence Bucket (OOS)", ""]
            for b in buckets:
                lines.append(f"- conf {b['confidence_range']}: {_pct(b['accuracy'])} ({b['days']} days)")
            lines.append("")

        days = days_payload.get("days") or []
        if days:
            reasons = days_payload.get("skip_reasons") or {}
            lines += [
                "## Daily Signals — OOS",
                "",
                f"Skip rate {_pct(days_payload.get('skip_rate'))}"
                + (f" — {', '.join(f'{k}: {v}' for k, v in reasons.items())}" if reasons else ""),
                "",
                "| Date | Call | Actual | OK | Conf | UP | DOWN | NEU | Lean | Skip Reason |",
                "|---|---|---|---|---|---|---|---|---|---|",
            ]
            for d in reversed(days):
                ok = "—" if d["skipped"] else ("✓" if d["correct"] else "✗")
                conf = "—" if d["skipped"] else f"{d['confidence']:.2f}"
                lines.append(
                    f"| {d['date']} | {d['call']} | {d['actual']} | {ok} | {conf} | "
                    f"{d['up_score']:.2f} | {d['down_score']:.2f} | {d['neutral_score']:.2f} | "
                    f"{d['lean']} | {d['skip_reason']} |"
                )
            lines.append("")

    if last_sweep:
        lines += [
            "## Last Optuna Sweep",
            "",
            f"- Completed: {last_sweep.get('when', '?')} | trials: {last_sweep.get('n_trials')} "
            f"| guardrail violations: {last_sweep.get('failed_trials')}",
        ]
        importances = last_sweep.get("importances") or {}
        if importances:
            lines.append("- Parameter importances (fANOVA):")
            for k, v in sorted(importances.items(), key=lambda kv: -kv[1]):
                lines.append(f"    - {k}: {v:.1%}")
        lines.append("")

    if run_summary:
        lines += [
            "## Recent Optimization Runs",
            "",
            "| Run | Started | Iterations | Accepted | Best Acc | Notes |",
            "|---|---|---|---|---|---|",
        ]
        for r in run_summary[:10]:
            lines.append(
                f"| {r['run_id']} | {r['started_at']} | {r['total_iterations']} | "
                f"{r['accepted_count']} | {_pct(r['best_accuracy'])} | {r['notes'] or ''} |"
            )
        lines.append("")

    lines += [
        "---",
        "Notes: OOS = last 20% of history, never used during optimization. "
        "Macro = mean of per-class accuracy. Skip reasons: low_confidence = conviction gate; "
        "vix_above_high / vix_below_low = volatility regime gate; missing_features = data gap.",
        "",
    ]
    return "\n".join(lines)
