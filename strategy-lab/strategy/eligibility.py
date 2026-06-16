"""
strategy/eligibility.py — Oracle eligibility gate.

Defines the pass/fail criteria an Oracle strategy must meet before it
is considered ready for paper trading (Phase 2). This module is the
arbiter — Oracle doesn't decide if it passed; this gate does.

Hermes's optimization goal is to reach this gate, not just "improve accuracy."
The gate is intentionally conservative: a strategy that passes here has
demonstrated both in-sample performance and out-of-sample generalization.

Usage:
    gate = EligibilityGate()
    result = gate.evaluate(is_results, oos_results)
    if result.passed:
        registry.promote(strategy_id, "eligible")
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Criterion definitions
# ---------------------------------------------------------------------------

@dataclass
class Criterion:
    name: str
    description: str
    value: float
    threshold: float
    passed: bool
    direction: str = "above"   # "above" → value must be >= threshold
                                # "below" → value must be <= threshold

    @property
    def gap(self) -> float:
        """How far from the threshold (positive = good headroom, negative = failing)."""
        if self.direction == "above":
            return self.value - self.threshold
        return self.threshold - self.value

    def to_dict(self) -> dict:
        return {
            "name":        self.name,
            "description": self.description,
            "value":       round(self.value, 4),
            "threshold":   round(self.threshold, 4),
            "passed":      self.passed,
            "direction":   self.direction,
            "gap":         round(self.gap, 4),
        }


@dataclass
class EligibilityResult:
    passed: bool
    criteria: list[Criterion]
    is_results_summary: dict = field(default_factory=dict)
    oos_results_summary: dict = field(default_factory=dict)

    @property
    def failing(self) -> list[Criterion]:
        return [c for c in self.criteria if not c.passed]

    @property
    def passing(self) -> list[Criterion]:
        return [c for c in self.criteria if c.passed]

    def to_dict(self) -> dict:
        return {
            "passed":    self.passed,
            "criteria":  [c.to_dict() for c in self.criteria],
            "failing":   [c.name for c in self.failing],
            "score":     f"{len(self.passing)}/{len(self.criteria)}",
            "is":        self.is_results_summary,
            "oos":       self.oos_results_summary,
        }


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

@dataclass
class EligibilityGate:
    """
    Pass/fail gate for Oracle direction strategies.

    All thresholds are intentionally conservative — they represent the
    minimum bar for a strategy to be worth risking real capital on.
    Adjust only with strong evidence from live paper-trading data.

    IS  = in-sample (80% of historical data, used during Hermes optimization)
    OOS = out-of-sample (last 20%, never seen during optimization)
    """
    # PRECISION thresholds (in-sample). Precision = "when Oracle makes this
    # call, how often is it right?" — the trade-signal hit rate. We only trade
    # directional calls, so precision (not recall) is what capital rides on.
    min_dir_precision:    float = 0.60   # 60% of directional calls correct
    min_up_precision:     float = 0.60   # bull-put signal quality
    min_down_precision:   float = 0.55   # bear-call signal quality (harder class)

    # OOS thresholds (slightly looser — OOS is inherently noisier)
    min_oos_dir_precision:  float = 0.55
    min_oos_down_precision: float = 0.50  # must not flip below random out-of-sample

    # Generalization check (on the metric we trade)
    max_oos_drift:        float = 0.10   # IS vs OOS directional-precision gap

    # Sample size & coverage
    min_trade_days:       int   = 200    # active classification days
    min_directional_calls: int  = 150    # enough actual TRADE signals to matter

    def evaluate(
        self,
        is_results,        # OracleResults
        oos_results=None,  # OracleResults | None — if None, OOS criteria are skipped
    ) -> EligibilityResult:
        """
        Evaluate all criteria against the provided results.

        If oos_results is None, OOS criteria are marked as pending (not failed).
        This allows the eligibility panel to show IS progress before OOS is run.
        """
        criteria: list[Criterion] = []

        def add(name, desc, value, threshold, direction="above") -> None:
            if direction == "above":
                passed = value >= threshold
            else:
                passed = value <= threshold
            criteria.append(Criterion(name, desc, value, threshold, passed, direction))

        # --- In-sample criteria (precision = trade-signal hit rate) ---
        add(
            "IS Directional Precision",
            "When Oracle calls UP or DOWN on training data, how often it's right",
            is_results.directional_precision,
            self.min_dir_precision,
        )
        add(
            "IS UP Precision",
            "UP calls correct (bull-put spread signal quality)",
            is_results.up_precision,
            self.min_up_precision,
        )
        add(
            "IS DOWN Precision",
            "DOWN calls correct (bear-call spread signal quality)",
            is_results.down_precision,
            self.min_down_precision,
        )
        add(
            "Trade Days",
            "Days Oracle made any call (not SKIP) — sample size check",
            float(is_results.trade_days),
            float(self.min_trade_days),
        )
        add(
            "Directional Calls",
            "Actual trade signals (UP/DOWN calls) — enough to matter",
            float(is_results.directional_calls),
            float(self.min_directional_calls),
        )

        # --- OOS criteria (only if oos_results provided) ---
        if oos_results is not None:
            add(
                "OOS Directional Precision",
                "Trade-signal hit rate on held-out data (never seen during optimization)",
                oos_results.directional_precision,
                self.min_oos_dir_precision,
            )
            add(
                "OOS DOWN Precision",
                "DOWN-call quality on held-out data — must not collapse OOS",
                oos_results.down_precision,
                self.min_oos_down_precision,
            )
            drift = abs(is_results.directional_precision - oos_results.directional_precision)
            add(
                "IS/OOS Precision Drift",
                "Gap between IS and OOS directional precision — guards against overfitting",
                drift,
                self.max_oos_drift,
                direction="below",
            )

        passed = all(c.passed for c in criteria)

        is_summary = is_results.summary_dict() if is_results else {}
        oos_summary = oos_results.summary_dict() if oos_results else {}

        return EligibilityResult(
            passed=passed,
            criteria=criteria,
            is_results_summary=is_summary,
            oos_results_summary=oos_summary,
        )

    def thresholds_dict(self) -> dict:
        """Return all thresholds as a dict — used in Hermes prompts."""
        return {
            "min_overall_accuracy": self.min_overall_accuracy,
            "min_up_accuracy":      self.min_up_accuracy,
            "min_down_accuracy":    self.min_down_accuracy,
            "min_neutral_accuracy": self.min_neutral_accuracy,
            "min_oos_overall":      self.min_oos_overall,
            "min_oos_down":         self.min_oos_down,
            "max_oos_drift":        self.max_oos_drift,
            "min_trade_days":       self.min_trade_days,
            "max_skip_rate":        self.max_skip_rate,
        }


# ---------------------------------------------------------------------------
# Singleton gate (shared across server and loop)
# ---------------------------------------------------------------------------

DEFAULT_GATE = EligibilityGate()
