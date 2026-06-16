"""
strategy-lab/server.py — FastAPI backend for the InfiniteLoop dashboard.

Serves the dashboard SPA, streams Oracle Hermes loop iterations via WebSocket,
and exposes REST endpoints for control and data.

Run from the strategy-lab directory:
    python server.py            # http://localhost:8000
    python server.py --port 8080

Architecture:
- Data pipeline runs once on startup (loads SPX/VIX, builds feature vectors)
- Oracle state (best strategy, iteration history) is kept in memory
- Hermes loop runs in a background asyncio task
- All connected WebSocket clients receive each iteration as a JSON message
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Path setup — allow running from strategy-lab/ directly
# ---------------------------------------------------------------------------
HERE = Path(__file__).parent
DASHBOARD_DIR = HERE.parent / "dashboard"
sys.path.insert(0, str(HERE))

# Load .env BEFORE any module reads os.getenv (DATABASE_URL, OLLAMA_*, ...)
import config  # noqa: E402,F401

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("infiniteloop.server")

# ---------------------------------------------------------------------------
# App state (in-memory)
# ---------------------------------------------------------------------------
from oracle.classifier import OracleStrategy, OracleParams, ORACLE_OPTIMIZABLE_PARAMS
from oracle.backtest import run_oracle_backtest, OracleResults, run_oracle_oos_backtest, confidence_buckets
from oracle.walkforward import run_walk_forward
from oracle.report import build_report
from oracle.outcomes import label_outcomes, merge_outcomes_into_features
from oracle.hermes_loop import run_oracle_loop, MAX_ITERATIONS
from oracle.optimize import run_oracle_sweep, DEFAULT_TRIALS
from oracle.records import IterationRecord, record_to_ws, db_iteration_to_ws
from hermes.client import HermesClient
from data.market_data import MarketDataClient
from data.loader import load_day_features
from strategy.eligibility import EligibilityGate, DEFAULT_GATE
from strategy.oracle_registry import BaseOracleRegistry, open_oracle_registry
from tradeagent.simulator import TradeParams, compare_policies

DATA_START = "2018-01-01"   # Oracle training window (FirstRateData era).
                            # Effective start is bounded by the SPX daily CSV —
                            # currently 2020-01-02; re-download back to 2018 to use it all.
DATA_END   = "2026-06-10"   # last day in the FirstRateData file

class AppState:
    def __init__(self) -> None:
        self.ready: bool = False
        self.startup_error: str | None = None
        self.features_is: Any = None
        self.features_oos: Any = None
        self.oracle: OracleStrategy = OracleStrategy()
        self.baseline_results: OracleResults | None = None
        self.current_results: OracleResults | None = None
        self.iter_history: list[dict] = []
        self.loop_task: asyncio.Task | None = None
        self.loop_running: bool = False
        self.loop_stop_event: asyncio.Event = asyncio.Event()
        self.ws_clients: set[WebSocket] = set()
        # Conversational chat state
        self.chat_history: list[dict] = []   # [{role, content}], last N turns kept
        self.strategy_concepts: list[dict] = []  # logged ideas for future implementation
        # Persistent iteration registry — always set after startup (postgres or sqlite)
        self.oracle_registry: BaseOracleRegistry | None = None
        self.registry_backend: str = "none"   # "postgres" | "sqlite" | "none"
        self.iter_seq: int = 0                # global sequence number across runs/sessions
        self.last_sweep: dict | None = None   # importances etc. from the most recent sweep
        self.client: Any = None               # MarketDataClient (set during startup)
        self.hermes: HermesClient = HermesClient()   # shared Hermes client

    def next_seq(self) -> int:
        self.iter_seq += 1
        return self.iter_seq

STATE = AppState()

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="InfiniteLoop Dashboard API", version="1.0.0")

# ---------------------------------------------------------------------------
# Data pipeline startup
# ---------------------------------------------------------------------------

async def _load_pipeline() -> None:
    """Load market data and build Oracle feature vectors. Runs once on startup."""
    try:
        LOGGER.info("Loading market data pipeline...")
        client = MarketDataClient()
        STATE.client = client   # kept for the trade agent (intraday paths, SPX opens)
        LOGGER.info(client.coverage_report())

        LOGGER.info("Building day feature vectors (%s -> %s)...", DATA_START, DATA_END)
        features_raw = load_day_features(client, DATA_START, DATA_END)
        LOGGER.info("  %d feature rows loaded", len(features_raw))

        spx_daily = client.get_spx_daily(DATA_START, DATA_END)
        vix = client.vix
        outcomes = label_outcomes(spx_daily, vix)

        features = merge_outcomes_into_features(features_raw, outcomes)
        LOGGER.info("  %d rows after merge with VIX outcomes", len(features))

        if len(features) < 100:
            raise RuntimeError(f"Only {len(features)} rows after merge — too few for optimization")

        # OOS split: last 20% locked away
        split_idx = int(len(features) * 0.80)
        STATE.features_is  = features.iloc[:split_idx].copy()
        STATE.features_oos = features.iloc[split_idx:].copy()

        LOGGER.info(
            "  IS: %d rows | OOS: %d rows (locked)",
            len(STATE.features_is), len(STATE.features_oos)
        )

        # Connect the registry — postgres if reachable, sqlite fallback otherwise.
        # NEVER None: history must always persist somewhere.
        STATE.registry_backend, STATE.oracle_registry = open_oracle_registry()
        LOGGER.info("✓ OracleRegistry connected (backend=%s)", STATE.registry_backend)

        # Best-effort: also ensure the Phase 2 tables exist when on Postgres
        if STATE.registry_backend == "postgres":
            try:
                from strategy.registry import StrategyRegistry
                StrategyRegistry().initialize_schema()
            except Exception as exc:
                LOGGER.warning("Could not initialize strategy tables: %s", exc)

        # Seed Oracle from the best params ever found — this is the
        # cross-session compounding: each server start resumes from the best
        # known configuration instead of factory defaults.
        try:
            best_ever = STATE.oracle_registry.get_best_params_ever()
            if best_ever:
                STATE.oracle = OracleStrategy.from_params(best_ever)
                LOGGER.info("✓ Oracle seeded from best-ever persisted params")
        except Exception as exc:
            LOGGER.warning("Could not seed Oracle from registry: %s", exc)

        # Hydrate iteration history so the dashboard shows past sessions
        try:
            past = STATE.oracle_registry.load_cross_session_history(limit=200)
            STATE.iter_history = [
                db_iteration_to_ws(row, seq) for seq, row in enumerate(past, start=1)
            ]
            STATE.iter_seq = len(STATE.iter_history)
            LOGGER.info("✓ Hydrated %d past iterations from registry", len(STATE.iter_history))
        except Exception as exc:
            LOGGER.warning("Could not hydrate iteration history: %s", exc)

        # Baseline backtest (after seeding, so the dashboard shows the real current state)
        STATE.baseline_results = run_oracle_backtest(STATE.features_is, STATE.oracle, "outcome")
        STATE.current_results  = STATE.baseline_results
        LOGGER.info(
            "Baseline accuracy: %.1f%% (UP=%.1f%% DOWN=%.1f%% NEU=%.1f%%)",
            STATE.baseline_results.overall_accuracy * 100,
            STATE.baseline_results.up_accuracy    * 100,
            STATE.baseline_results.down_accuracy   * 100,
            STATE.baseline_results.neutral_accuracy * 100,
        )

        STATE.ready = True
        LOGGER.info("✓ Pipeline ready")

    except Exception as exc:
        STATE.startup_error = str(exc)
        LOGGER.exception("Pipeline startup failed: %s", exc)


@app.on_event("startup")
async def startup_event() -> None:
    asyncio.create_task(_load_pipeline())


# ---------------------------------------------------------------------------
# WebSocket manager
# ---------------------------------------------------------------------------

async def _broadcast(msg: dict) -> None:
    """Send a JSON message to all connected WebSocket clients."""
    if not STATE.ws_clients:
        return
    text = json.dumps(msg)
    dead: set[WebSocket] = set()
    for ws in list(STATE.ws_clients):
        try:
            await ws.send_text(text)
        except Exception:
            dead.add(ws)
    STATE.ws_clients -= dead


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    STATE.ws_clients.add(ws)
    LOGGER.info("WebSocket client connected (total=%d)", len(STATE.ws_clients))
    # Send current state immediately on connect
    await ws.send_text(json.dumps({"type": "state", "data": _build_state_payload()}))
    try:
        while True:
            # Keep connection alive by waiting for messages (ping/pong)
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        STATE.ws_clients.discard(ws)
        LOGGER.info("WebSocket client disconnected (total=%d)", len(STATE.ws_clients))


# ---------------------------------------------------------------------------
# REST — status
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def get_status() -> JSONResponse:
    return JSONResponse(_build_state_payload())


@app.get("/api/health")
async def get_health() -> JSONResponse:
    """
    Operational health: which persistence backend is live, whether Hermes is
    reachable, and how much history exists. The dashboard surfaces this so
    persistence can never silently disappear again.
    """
    db_info: dict = {"backend": STATE.registry_backend, "ok": False, "runs": 0, "iterations": 0}
    if STATE.oracle_registry is not None:
        try:
            counts = await asyncio.get_event_loop().run_in_executor(
                None, STATE.oracle_registry.counts
            )
            db_info.update(ok=True, **counts)
        except Exception as exc:
            db_info["error"] = str(exc)

    hermes = HermesClient()
    hermes_ok = await asyncio.get_event_loop().run_in_executor(None, hermes.is_available)

    return JSONResponse({
        "ready": STATE.ready,
        "db": db_info,
        "hermes": {"ok": hermes_ok, "model": hermes.model, "base_url": hermes.base_url},
        "env": config.report(),
        "loop_running": STATE.loop_running,
        "session_iterations": len(STATE.iter_history),
    })


def _flatten_confusion(confusion: dict) -> dict:
    """Flatten nested confusion dict {actual: {predicted: count}} to {actual_predicted: count}."""
    flat: dict = {}
    if not confusion:
        return flat
    for actual, preds in confusion.items():
        if isinstance(preds, dict):
            for predicted, count in preds.items():
                flat[f"{actual}_{predicted}"] = count
    return flat


# Iteration wire format lives in oracle/records.py (record_to_ws / db_iteration_to_ws).
# Never hand-build iteration dicts here — key drift between server and dashboard
# is what previously broke the UI.


def _build_state_payload() -> dict:
    results = STATE.current_results
    params  = STATE.oracle.get_params()
    return {
        "ready":        STATE.ready,
        "startup_error": STATE.startup_error,
        "loop_running": STATE.loop_running,
        "registry_backend": STATE.registry_backend,
        "params":       params,
        "iter_count":   len(STATE.iter_history),
        "iter_history": STATE.iter_history[-50:],   # last 50 for initial load
        "is_rows":      len(STATE.features_is) if STATE.features_is is not None else 0,
        "oos_rows":     len(STATE.features_oos) if STATE.features_oos is not None else 0,
        "accuracy": {
            "overall":    results.overall_accuracy  if results else None,
            "up":         results.up_accuracy       if results else None,
            "down":       results.down_accuracy     if results else None,
            "neutral":    results.neutral_accuracy  if results else None,
            "skip_rate":  results.skip_rate         if results else None,
            "trade_days": results.trade_days        if results else None,
            "total_days": results.total_days        if results else None,
            "avg_confidence": results.avg_confidence if results else None,
            "up_count":   results.up_count          if results else 0,
            "down_count": results.down_count        if results else 0,
            "neutral_count": results.neutral_count  if results else 0,
        },
        # Flatten nested confusion dict to UP_UP, UP_DOWN, ... for the dashboard
        "confusion": _flatten_confusion(results.confusion) if results else {},
    }


# ---------------------------------------------------------------------------
# REST — Oracle loop control
# ---------------------------------------------------------------------------

class RunLoopRequest(BaseModel):
    iterations: int = 20
    target_accuracy: float = 0.65
    locked_params: list[str] = []


class StopResponse(BaseModel):
    stopped: bool


@app.post("/api/oracle/run")
async def run_oracle(req: RunLoopRequest) -> JSONResponse:
    if not STATE.ready:
        raise HTTPException(503, detail="Data pipeline not ready yet")

    # Auto-clear stale loop_running flag if the task is no longer alive
    # (happens when the browser refreshes and orphans the previous task)
    if STATE.loop_running:
        task_alive = STATE.loop_task is not None and not STATE.loop_task.done()
        if task_alive:
            raise HTTPException(409, detail="Loop is already running")
        else:
            LOGGER.warning("loop_running was True but task is done — auto-clearing stale flag")
            STATE.loop_running = False

    STATE.loop_stop_event.clear()
    STATE.loop_task = asyncio.create_task(
        _run_loop_task(req.iterations, req.target_accuracy, req.locked_params)
    )
    return JSONResponse({"started": True, "iterations": req.iterations})


@app.post("/api/oracle/stop")
async def stop_oracle() -> JSONResponse:
    STATE.loop_stop_event.set()
    return JSONResponse({"stopped": True})


@app.post("/api/oracle/reset")
async def reset_oracle_loop() -> JSONResponse:
    """Force-clear the loop_running flag. Use when the UI shows a stuck loop after a page refresh."""
    was_running = STATE.loop_running
    if STATE.loop_task and not STATE.loop_task.done():
        STATE.loop_task.cancel()
    STATE.loop_running = False
    STATE.loop_stop_event.set()
    LOGGER.info("Loop state force-reset (was_running=%s)", was_running)
    return JSONResponse({"reset": True, "was_running": was_running})


async def _run_loop_task(
    max_iter: int, target_accuracy: float, locked_params: list[str]
) -> None:
    """Background task: runs the Oracle Hermes loop, broadcasting each iteration."""
    STATE.loop_running = True
    run_records: list[dict] = []   # iterations from THIS run only (for per-run stats)
    await _broadcast({"type": "loop_started", "data": {"max_iterations": max_iter}})

    # Capture the event loop BEFORE entering the thread executor.
    # asyncio.get_event_loop() fails inside worker threads — use
    # run_coroutine_threadsafe with the captured loop instead.
    loop = asyncio.get_event_loop()

    def on_iteration(iteration: int, record: IterationRecord, best: OracleStrategy) -> None:
        # Global sequence number — unique across runs AND sessions, so the
        # dashboard never silently drops records from a second run (its dedupe
        # key is "n", and run-local iteration numbers restart at 1 every run).
        rec_dict = record_to_ws(record, STATE.next_seq())
        STATE.iter_history.append(rec_dict)
        run_records.append(rec_dict)
        try:
            STATE.current_results = run_oracle_backtest(STATE.features_is, best, "outcome")
        except Exception as exc:
            LOGGER.error("run_oracle_backtest failed in on_iteration: %s", exc, exc_info=True)
            return
        r = STATE.current_results
        conf_flat = _flatten_confusion(r.confusion)
        LOGGER.debug(
            "Iteration %d confusion flat keys=%d sample UP_UP=%s",
            iteration, len(conf_flat), conf_flat.get("UP_UP", "MISSING")
        )
        asyncio.run_coroutine_threadsafe(
            _broadcast({
                "type": "iteration",
                "data": {
                    "record": rec_dict,
                    "params": best.get_params(),
                    "accuracy": {
                        "overall":    r.overall_accuracy,
                        "up":         r.up_accuracy,
                        "down":       r.down_accuracy,
                        "neutral":    r.neutral_accuracy,
                        "skip_rate":  r.skip_rate,
                        "avg_confidence": r.avg_confidence,
                        "up_count":   r.up_count,
                        "down_count": r.down_count,
                        "neutral_count": r.neutral_count,
                    },
                    "confusion": conf_flat,
                },
            }),
            loop,
        )
        # Check stop event
        if STATE.loop_stop_event.is_set():
            raise StopIteration("User requested stop")

    try:
        best, history = await loop.run_in_executor(
            None,
            lambda: run_oracle_loop(
                STATE.features_is,
                initial_strategy=STATE.oracle,
                max_iterations=max_iter,
                outcome_col="outcome",
                on_iteration=on_iteration,
                oracle_registry=STATE.oracle_registry,
                run_notes="server-initiated run",
            )
        )
        # Always deploy the all-time best params, not just this run's best.
        # A run may explore a worse region and finish below the historical peak.
        best_ever_params = (
            STATE.oracle_registry.get_best_params_ever()
            if STATE.oracle_registry is not None
            else None
        )
        if best_ever_params is not None:
            STATE.oracle = OracleStrategy.from_params(best_ever_params)
            LOGGER.info("Hermes loop complete — deploying all-time best params from DB")
        else:
            STATE.oracle = best
        STATE.current_results = run_oracle_backtest(STATE.features_is, STATE.oracle, "outcome")
        await _broadcast({
            "type": "loop_complete",
            "data": {
                "params": STATE.oracle.get_params(),
                "accuracy": {
                    "overall":  STATE.current_results.overall_accuracy,
                    "up":       STATE.current_results.up_accuracy,
                    "down":     STATE.current_results.down_accuracy,
                    "neutral":  STATE.current_results.neutral_accuracy,
                },
                "accepted_count": sum(1 for r in run_records if r.get("ok")),
                "total_iterations": len(run_records),
            },
        })
    except StopIteration:
        await _broadcast({"type": "loop_stopped", "data": {"reason": "user_requested"}})
    except RuntimeError as exc:
        # Hermes not available etc.
        await _broadcast({"type": "loop_error", "data": {"error": str(exc)}})
        LOGGER.error("Loop task error: %s", exc)
    except Exception as exc:
        await _broadcast({"type": "loop_error", "data": {"error": str(exc)}})
        LOGGER.exception("Loop task unexpected error: %s", exc)
    finally:
        STATE.loop_running = False


# ---------------------------------------------------------------------------
# REST — Optuna sweep (numeric threshold optimization)
# ---------------------------------------------------------------------------

class SweepRequest(BaseModel):
    trials: int = DEFAULT_TRIALS


@app.post("/api/oracle/sweep")
async def run_sweep(req: SweepRequest) -> JSONResponse:
    """Start an Optuna TPE sweep over the 9 direction thresholds (IS data only).
    Shares the run/stop machinery with the Hermes loop — only one may run."""
    if not STATE.ready:
        raise HTTPException(503, detail="Data pipeline not ready yet")
    if STATE.loop_running:
        task_alive = STATE.loop_task is not None and not STATE.loop_task.done()
        if task_alive:
            raise HTTPException(409, detail="A loop or sweep is already running")
        STATE.loop_running = False

    STATE.loop_stop_event.clear()
    STATE.loop_task = asyncio.create_task(_run_sweep_task(req.trials))
    return JSONResponse({"started": True, "trials": req.trials})


async def _run_sweep_task(n_trials: int) -> None:
    """Background task: runs the Optuna sweep, broadcasting each trial as a
    normal iteration record so the existing dashboard chart/log update live."""
    STATE.loop_running = True
    run_records: list[dict] = []
    await _broadcast({"type": "loop_started", "data": {"max_iterations": n_trials, "mode": "sweep"}})

    loop = asyncio.get_event_loop()

    def on_trial(trial_num: int, record: IterationRecord, best: OracleStrategy) -> None:
        rec_dict = record_to_ws(record, STATE.next_seq())
        STATE.iter_history.append(rec_dict)
        run_records.append(rec_dict)
        asyncio.run_coroutine_threadsafe(
            _broadcast({
                "type": "iteration",
                "data": {
                    "record": rec_dict,
                    "params": best.get_params(),
                    "accuracy": {
                        "overall": record.accuracy,
                        "up": record.up_accuracy,
                        "down": record.down_accuracy,
                        "neutral": record.neutral_accuracy,
                        "skip_rate": record.skip_rate,
                    },
                },
            }),
            loop,
        )
        if STATE.loop_stop_event.is_set():
            raise StopIteration("User requested stop")

    try:
        result = await loop.run_in_executor(
            None,
            lambda: run_oracle_sweep(
                STATE.features_is,
                n_trials=n_trials,
                outcome_col="outcome",
                oracle_registry=STATE.oracle_registry,
                on_trial=on_trial,
                seed_params=STATE.oracle.get_params(),
                run_notes="server-initiated optuna sweep",
                hermes_client=STATE.hermes,
            ),
        )
        # Always deploy the all-time best params across every sweep ever run,
        # not just this sweep's winner. A sweep may explore a worse region.
        best_ever_params = (
            STATE.oracle_registry.get_best_params_ever()
            if STATE.oracle_registry is not None
            else None
        )
        if best_ever_params is not None:
            STATE.oracle = OracleStrategy.from_params(best_ever_params)
            LOGGER.info("Sweep complete — deploying all-time best params from DB")
        else:
            STATE.oracle = OracleStrategy.from_params(result.best_params)
        STATE.current_results = run_oracle_backtest(STATE.features_is, STATE.oracle, "outcome")
        STATE.last_sweep = {
            "when": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "n_trials": result.n_trials,
            "failed_trials": result.n_failed,
            "importances": result.param_importances,
        }
        await _broadcast({
            "type": "loop_complete",
            "data": {
                "mode": "sweep",
                "params": STATE.oracle.get_params(),
                "accuracy": {
                    "overall":  STATE.current_results.overall_accuracy,
                    "up":       STATE.current_results.up_accuracy,
                    "down":     STATE.current_results.down_accuracy,
                    "neutral":  STATE.current_results.neutral_accuracy,
                },
                "accepted_count": sum(1 for r in run_records if r.get("ok")),
                "total_iterations": len(run_records),
                "param_importances": result.param_importances,
                "failed_trials": result.n_failed,
            },
        })
    except Exception as exc:
        await _broadcast({"type": "loop_error", "data": {"error": str(exc)}})
        LOGGER.exception("Sweep task error: %s", exc)
    finally:
        STATE.loop_running = False


# ---------------------------------------------------------------------------
# REST — OOS validation
# ---------------------------------------------------------------------------

@app.post("/api/oracle/oos")
async def run_oos() -> JSONResponse:
    if not STATE.ready:
        raise HTTPException(503, detail="Data pipeline not ready yet")
    if STATE.features_oos is None or len(STATE.features_oos) < 20:
        raise HTTPException(400, detail="Not enough OOS data")

    is_results, oos_results = await asyncio.get_event_loop().run_in_executor(  # type: ignore[attr-defined]
        None,
        lambda: run_oracle_oos_backtest(
            STATE.features_is._append(STATE.features_oos),
            STATE.oracle,
            oos_fraction=0.20,
            outcome_col="outcome",
        )
    )
    drift = abs(is_results.overall_accuracy - oos_results.overall_accuracy)
    return JSONResponse({
        "is": {
            "overall":  is_results.overall_accuracy,
            "up":       is_results.up_accuracy,
            "down":     is_results.down_accuracy,
            "neutral":  is_results.neutral_accuracy,
            "trade_days": is_results.trade_days,
            "total_days": is_results.total_days,
            "skip_rate": is_results.skip_rate,
            "skip_reasons": is_results.skip_reasons,
        },
        "oos": {
            "overall":  oos_results.overall_accuracy,
            "up":       oos_results.up_accuracy,
            "down":     oos_results.down_accuracy,
            "neutral":  oos_results.neutral_accuracy,
            "trade_days": oos_results.trade_days,
            "total_days": oos_results.total_days,
            "skip_rate": oos_results.skip_rate,
            "skip_reasons": oos_results.skip_reasons,
        },
        "drift":       drift,
        "pass":        drift < 0.10,
        "confusion_oos": oos_results.confusion,
        "confidence_buckets_oos": confidence_buckets(oos_results.raw),
    })


# ---------------------------------------------------------------------------
# REST — Walk-forward validation
# ---------------------------------------------------------------------------

@app.get("/api/oracle/walkforward")
async def get_walk_forward(folds: int = 6) -> JSONResponse:
    """
    Evaluate the current Oracle params across N consecutive time windows
    spanning the FULL history (IS + OOS). Fixed params, no re-tuning — this
    answers 'is the edge stable across regimes, or did one window carry it?'
    """
    if not STATE.ready:
        raise HTTPException(503, detail="Data pipeline not ready yet")
    folds = max(3, min(folds, 12))

    full = STATE.features_is._append(STATE.features_oos)
    report = await asyncio.get_event_loop().run_in_executor(
        None, lambda: run_walk_forward(full, STATE.oracle, n_folds=folds, outcome_col="outcome")
    )
    return JSONResponse(report.to_dict())


# ---------------------------------------------------------------------------
# REST — Per-day signals (confidence per day — input for the trade agent)
# ---------------------------------------------------------------------------

@app.get("/api/oracle/days")
async def get_daily_signals(window: str = "oos", limit: int = 250) -> JSONResponse:
    """
    Per-day Oracle output: call, actual, correct, confidence, per-class scores,
    lean, and skip reason. window = 'oos' (default) or 'is' (most recent rows).

    The confidence column is the future trade agent's primary input: it decides
    whether to trade at all, and eventually how aggressively.
    """
    if not STATE.ready:
        raise HTTPException(503, detail="Data pipeline not ready yet")

    frame = STATE.features_oos if window == "oos" else STATE.features_is.tail(limit)
    if frame is None or frame.empty:
        return JSONResponse({"days": [], "window": window})

    results = await asyncio.get_event_loop().run_in_executor(
        None, lambda: run_oracle_backtest(frame, STATE.oracle, "outcome")
    )
    raw = results.raw
    days = []
    for _, row in raw.tail(limit).iterrows():
        days.append({
            "date":        str(row["date"].date() if hasattr(row["date"], "date") else row["date"]),
            "call":        row["predicted"],
            "actual":      row["actual"],
            "correct":     bool(row["correct"]),
            "skipped":     bool(row["skipped"]),
            "skip_reason": row.get("skip_reason", ""),
            "confidence":  round(float(row["confidence"]), 4),
            "up_score":    round(float(row["up_score"]), 4),
            "down_score":  round(float(row["down_score"]), 4),
            "neutral_score": round(float(row["neutral_score"]), 4),
            "lean":        row.get("lean", "NONE"),
        })
    return JSONResponse({
        "days": days,
        "window": window,
        "skip_reasons": results.skip_reasons,
        "skip_rate": round(results.skip_rate, 4),
        "confidence_buckets": confidence_buckets(raw),
    })


# ---------------------------------------------------------------------------
# REST — Eligibility gate
# ---------------------------------------------------------------------------

@app.get("/api/oracle/eligibility")
async def get_eligibility():
    """Evaluate Oracle eligibility gate against current IS (and OOS if available) results."""
    if STATE.oracle is None or STATE.features_is is None:
        return JSONResponse({"error": "No Oracle loaded. Run backtest first."}, status_code=400)

    loop = asyncio.get_event_loop()

    # Run IS backtest
    is_results: OracleResults = await loop.run_in_executor(
        None,
        lambda: run_oracle_backtest(STATE.features_is, STATE.oracle, "outcome"),
    )

    # Try OOS backtest if we have enough data
    oos_results = None
    if STATE.features_oos is not None and len(STATE.features_oos) > 10:
        try:
            oos_results = await loop.run_in_executor(
                None,
                lambda: run_oracle_backtest(STATE.features_oos, STATE.oracle, "outcome"),
            )
        except Exception as exc:
            LOGGER.warning("OOS backtest failed in eligibility endpoint: %s", exc)

    result = DEFAULT_GATE.evaluate(is_results, oos_results)
    return JSONResponse(result.to_dict())


# ---------------------------------------------------------------------------
# REST — Hermes chat
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str


_CHAT_SYSTEM = """\
You are Oracle, the direction classification agent for InfiniteLoop — a 0DTE SPX options \
trading system. You classify each trading day UP, DOWN, or NEUTRAL based on pre-market and \
early-session order flow features (gap, ORB breakout, delta bias, VWAP slope, VIX filter).

You are conversing with Kirk, the system architect. Be analytical and specific. You may:
  1. Discuss ideas, explain your reasoning, analyse why per-class accuracy is low, etc.
  2. Suggest a SPECIFIC parameter change to test — include it as test_suggestion.
  3. Log a broader feature/strategy IDEA that needs new code — include it as strategy_concept.

PARAMETERS YOU CONTROL (and ONLY these):
  gap_threshold_pct, orb_breakout_pct, delta_bias_threshold,
  neutral_band_pct, vwap_slope_threshold, vol_filter_high, vol_filter_low,
  prev_day_return_threshold, prev_day_vwap_threshold,
  min_confidence (directional calls below this confidence are skipped),
  min_score_separation (|up-down| score gap below this becomes NEUTRAL)

NEVER touch: forced_exit_hour, max_loss_pct, daily_halt_pct, spread sizing, strike selection.

Respond with valid JSON ONLY — no prose outside the JSON:
{
  "response": "<conversational reply to Kirk, 2-5 sentences>",
  "test_suggestion": {                        <- OPTIONAL: only when a specific param change is proposed
    "param_to_change": "<param_name>",
    "new_value": <number>,
    "reasoning": "<why this might improve direction accuracy>"
  },
  "strategy_concept": {                       <- OPTIONAL: only when a new feature/idea needs logging
    "name": "<short concept name>",
    "description": "<what it does>",
    "expected_edge": "<why it might improve accuracy>",
    "implementation_notes": "<what files/features would need to change>"
  }
}
Include test_suggestion ONLY when Kirk explicitly asks to test/try a specific param value, \
or when you are proposing one yourself. Include strategy_concept ONLY when the idea requires \
new features or data beyond current parameters.\
"""


def _build_chat_prompt(message: str) -> str:
    """Build the full Hermes prompt with live system context, persisted
    optimization history (cross-session), and conversation history."""
    r = STATE.current_results
    ctx_lines = []
    if r:
        ctx_lines.append(
            f"Current IS accuracy: {r.overall_accuracy:.1%} "
            f"(UP={r.up_accuracy:.1%} DOWN={r.down_accuracy:.1%} NEUTRAL={r.neutral_accuracy:.1%}) "
            f"| skip={r.skip_rate:.1%} | trade_days={r.trade_days} "
            f"| avg_confidence={r.avg_confidence:.2f} | iterations={len(STATE.iter_history)}"
        )
        ctx_lines.append(
            f"Class distribution (actual): UP={r.up_count} DOWN={r.down_count} NEUTRAL={r.neutral_count}"
        )
    ctx_lines.append(f"Current parameters: {json.dumps(STATE.oracle.get_params())}")

    # ── Optimization history (hydrated from the registry — spans ALL sessions) ──
    recent = STATE.iter_history[-15:]
    if recent:
        hist_lines = []
        for t in recent:
            acc = t.get("acc")
            acc_str = f"{acc:.4f}" if isinstance(acc, (int, float)) else "?"
            status = "ACCEPTED" if t.get("ok") else "rejected"
            hist_lines.append(
                f"  #{t.get('n')}: {t.get('p')} {t.get('o')} -> {t.get('v')} | acc={acc_str} | {status}"
            )
        ctx_lines.append(
            f"Last {len(recent)} optimization iterations (persisted across sessions):\n"
            + "\n".join(hist_lines)
        )
        accepted_total = sum(1 for t in STATE.iter_history if t.get("ok"))
        ctx_lines.append(
            f"Totals: {len(STATE.iter_history)} iterations recorded, {accepted_total} accepted improvements."
        )

    if STATE.oracle_registry is not None:
        try:
            runs = STATE.oracle_registry.get_run_summary()[:5]
            if runs:
                run_lines = [
                    f"  run {x['run_id']}: {x['total_iterations'] or 0} iters, "
                    f"{x['accepted_count']} accepted, best_acc={x['best_accuracy']}, notes={x['notes'] or '-'}"
                    for x in runs
                ]
                ctx_lines.append("Recent optimization runs (newest first):\n" + "\n".join(run_lines))
            tried = STATE.oracle_registry.get_tried_values()
            if tried:
                tried_summary = {p: len(v) for p, v in tried.items()}
                ctx_lines.append(f"Values tested per parameter so far: {json.dumps(tried_summary)}")
        except Exception as exc:
            LOGGER.warning("Could not load registry context for chat: %s", exc)

    history_lines = []
    for turn in STATE.chat_history[-8:]:
        speaker = "Kirk" if turn["role"] == "user" else "Oracle"
        history_lines.append(f"{speaker}: {turn['content']}")

    parts = [
        _CHAT_SYSTEM,
        "\n\nSYSTEM CONTEXT:\n" + "\n".join(ctx_lines),
    ]
    if history_lines:
        parts.append("\n\nCONVERSATION SO FAR:\n" + "\n".join(history_lines))
    parts.append(f"\n\nKirk: {message}")
    return "".join(parts)


@app.post("/api/hermes/chat")
async def hermes_chat(req: ChatRequest) -> JSONResponse:
    hermes = HermesClient()
    if not hermes.is_available():
        raise HTTPException(503, detail="Hermes (Ollama) is not running")
    if not STATE.ready:
        raise HTTPException(503, detail="Data pipeline not ready yet")

    loop = asyncio.get_event_loop()
    # Prompt building hits the registry (DB) — keep it off the event loop
    prompt = await loop.run_in_executor(None, _build_chat_prompt, req.message)
    raw = await loop.run_in_executor(None, lambda: hermes.generate_json(prompt))

    response_text: str = raw.get("response", "")
    if not response_text:
        # Hermes returned plain text instead of JSON — use it as-is
        response_text = str(raw)

    test_result: dict | None = None
    concept_logged: str | None = None

    # ── Handle test suggestion ─────────────────────────────────────────────
    sug = raw.get("test_suggestion")
    if isinstance(sug, dict):
        param = sug.get("param_to_change", "")
        new_val = sug.get("new_value")
        if param in ORACLE_OPTIMIZABLE_PARAMS and new_val is not None:
            try:
                from copy import deepcopy as _deepcopy
                candidate_params = _deepcopy(STATE.oracle.get_params())
                old_val = candidate_params.get(param)
                candidate_params[param] = new_val
                candidate = OracleStrategy.from_params(candidate_params)
                cand_results = await loop.run_in_executor(
                    None, lambda: run_oracle_backtest(STATE.features_is, candidate, "outcome")
                )
                current_acc = STATE.current_results.overall_accuracy if STATE.current_results else 0.0
                delta = cand_results.overall_accuracy - current_acc
                accepted = delta > 0.002   # same threshold as Hermes loop

                if accepted:
                    STATE.oracle = candidate
                    STATE.current_results = cand_results
                    # Push updated state to all WS clients
                    await _broadcast({"type": "state", "data": _build_state_payload()})
                    LOGGER.info(
                        "Chat test ACCEPTED: %s %s -> %s | accuracy %.2f%% -> %.2f%%",
                        param, old_val, new_val, current_acc * 100, cand_results.overall_accuracy * 100,
                    )
                else:
                    LOGGER.info(
                        "Chat test REJECTED: %s %s -> %s | accuracy %.2f%% -> %.2f%%",
                        param, old_val, new_val, current_acc * 100, cand_results.overall_accuracy * 100,
                    )

                test_result = {
                    "param":        param,
                    "old_value":    old_val,
                    "new_value":    new_val,
                    "old_accuracy": round(current_acc, 4),
                    "new_accuracy": round(cand_results.overall_accuracy, 4),
                    "delta":        round(delta, 4),
                    "accepted":     accepted,
                    "up":           round(cand_results.up_accuracy, 4),
                    "down":         round(cand_results.down_accuracy, 4),
                    "neutral":      round(cand_results.neutral_accuracy, 4),
                }

                # Persist chat tests too — they are real experiments and must be
                # part of Hermes' cross-session memory like any loop iteration.
                if STATE.oracle_registry is not None:
                    try:
                        chat_run_id = getattr(STATE, "chat_run_id", None)
                        if chat_run_id is None:
                            chat_run_id = await loop.run_in_executor(
                                None,
                                lambda: STATE.oracle_registry.create_run(
                                    STATE.oracle.get_params(), notes="chat tests"
                                ),
                            )
                            STATE.chat_run_id = chat_run_id
                        STATE.chat_test_count = getattr(STATE, "chat_test_count", 0) + 1
                        await loop.run_in_executor(
                            None,
                            lambda: STATE.oracle_registry.save_iteration(
                                run_id=chat_run_id,
                                iteration=STATE.chat_test_count,
                                param_changed=param,
                                old_value=old_val,
                                new_value=new_val,
                                overall_accuracy=cand_results.overall_accuracy,
                                up_accuracy=cand_results.up_accuracy,
                                down_accuracy=cand_results.down_accuracy,
                                neutral_accuracy=cand_results.neutral_accuracy,
                                skip_rate=cand_results.skip_rate,
                                accepted=accepted,
                                hermes_reasoning=f"[chat] {sug.get('reasoning', '')}",
                                full_params=candidate.get_params(),
                            ),
                        )
                    except Exception as exc:
                        LOGGER.error("Failed to persist chat test: %s", exc)
            except Exception as exc:
                LOGGER.warning("Chat test suggestion failed: %s", exc)
                test_result = {"error": str(exc)}

    # -- Handle strategy concept
    concept = raw.get("strategy_concept")
    if isinstance(concept, dict) and concept.get("name"):
        concept["logged_at"] = datetime.now().isoformat()
        concept["source"] = "conversation"
        STATE.strategy_concepts.append(concept)
        concept_logged = concept.get("name")
        LOGGER.info("Strategy concept logged: %s", concept_logged)

    # -- Store in chat history
    STATE.chat_history.append({"role": "user", "content": req.message})
    STATE.chat_history.append({"role": "assistant", "content": response_text})
    if len(STATE.chat_history) > 30:
        STATE.chat_history = STATE.chat_history[-30:]

    return JSONResponse({
        "response":      response_text,
        "test_result":   test_result,
        "concept_logged": concept_logged,
    })


@app.delete("/api/hermes/chat")
async def clear_chat_history() -> JSONResponse:
    """Clear Oracle conversation history."""
    STATE.chat_history.clear()
    return JSONResponse({"cleared": True})


@app.get("/api/hermes/analysis")
async def hermes_opening_analysis() -> JSONResponse:
    """
    Generate Oracle's opening analysis when the user navigates to the Hermes chat tab.

    Hermes reviews current accuracy metrics and gives an honest assessment:
    - Which classes are performing well vs. struggling
    - Whether meaningful improvement still seems possible
    - One concrete next thing to try
    - Overall verdict: promising / needs work / plateaued
    """
    r = STATE.current_results
    if r is None or r.trade_days == 0:
        return JSONResponse({"analysis": None, "reason": "no_data"})

    iters = len(STATE.iter_history)
    accepted = sum(1 for rec in STATE.iter_history if rec.get("ok"))
    last_accepted_iter = max(
        (rec["n"] for rec in STATE.iter_history if rec.get("ok")), default=0
    )
    stalled_for = iters - last_accepted_iter

    # Compact view of the last persisted iterations (spans past sessions)
    recent_lines = []
    for t in STATE.iter_history[-10:]:
        acc = t.get("acc")
        acc_str = f"{acc:.4f}" if isinstance(acc, (int, float)) else "?"
        recent_lines.append(
            f"  #{t.get('n')}: {t.get('p')} {t.get('o')} -> {t.get('v')} | acc={acc_str} | "
            + ("ACCEPTED" if t.get("ok") else "rejected")
        )
    recent_block = "\n".join(recent_lines) if recent_lines else "  (no iterations recorded yet)"

    analysis_prompt = f"""\
You are Oracle, the direction classifier for an InfiniteLoop 0DTE SPX options trading system.
Kirk has just opened the Hermes chat to check in on your current performance.
Give him an honest, analytical opening assessment — no fluff.

CURRENT PERFORMANCE SNAPSHOT:
  Overall IS accuracy : {r.overall_accuracy:.1%}
  UP accuracy         : {r.up_accuracy:.1%}  (n={r.up_count} days)
  DOWN accuracy       : {r.down_accuracy:.1%}  (n={r.down_count} days)
  NEUTRAL accuracy    : {r.neutral_accuracy:.1%}  (n={r.neutral_count} days)
  Skip rate           : {r.skip_rate:.1%}
  Avg confidence      : {r.avg_confidence:.2f}
  Trade days (IS)     : {r.trade_days}

OPTIMISATION HISTORY:
  Iterations run      : {iters}
  Improvements accepted: {accepted}
  Iterations since last improvement: {stalled_for}

LAST ITERATIONS (persisted across sessions):
{recent_block}

CURRENT PARAMETERS:
{json.dumps(STATE.oracle.get_params(), indent=2)}

Assess the following in 3-5 sentences:
1. Which direction class is the weakest and why (be specific about what the numbers suggest).
2. Whether further improvement via parameter tuning alone still seems possible, or if the gains
   are plateauing and a feature/code change would be needed.
3. One specific thing you would try next — either a param change or a structural idea.
4. A one-line overall verdict: is this a promising direction model, needs more work, or plateaued?

Return plain conversational text (NOT JSON). Be direct and honest — this is a diagnostic, not a pep talk.
"""

    try:
        hermes = HermesClient()
        if not hermes.is_available():
            return JSONResponse({"analysis": None, "reason": "hermes_unavailable"})

        raw = await asyncio.get_event_loop().run_in_executor(
            None, lambda: hermes.generate(analysis_prompt)
        )
        analysis_text = raw.strip() if isinstance(raw, str) else raw.get("response", str(raw)).strip()
        return JSONResponse({"analysis": analysis_text})
    except Exception as exc:
        LOGGER.warning("Hermes opening analysis failed: %s", exc)
        return JSONResponse({"analysis": None, "reason": str(exc)})


@app.get("/api/strategies/concepts")
async def get_strategy_concepts() -> JSONResponse:
    """Return all strategy concepts logged via chat."""
    return JSONResponse({"concepts": STATE.strategy_concepts})


# ---------------------------------------------------------------------------
# REST — Strategies
# ---------------------------------------------------------------------------

@app.get("/api/strategies")
async def get_strategies() -> JSONResponse:
    """Return strategy list. Falls back to current Oracle if DB not configured."""
    strategies = []

    try:
        from strategy.registry import StrategyRegistry
        reg = StrategyRegistry()
        strategies = reg.list_candidates()
        if strategies:
            return JSONResponse({"strategies": strategies})
    except Exception:
        pass

    if STATE.current_results:
        r = STATE.current_results
        strategies = [{
            "id":       "oracle-v1",
            "name":     "Oracle v1 (current)",
            "status":   "active",
            "created":  datetime.now().strftime("%Y-%m-%d"),
            "iterations": len(STATE.iter_history),
            "overall":   r.overall_accuracy,
            "up":        r.up_accuracy,
            "down":      r.down_accuracy,
            "neutral":   r.neutral_accuracy,
            "skip":      r.skip_rate,
            "trade_days": r.trade_days,
            "params":    STATE.oracle.get_params(),
            "confusion": r.confusion,
            "history":   [h.get("acc") for h in STATE.iter_history[-40:]],
        }]

    return JSONResponse({"strategies": strategies})


# ---------------------------------------------------------------------------
# Oracle iteration history & parameter landscape
# ---------------------------------------------------------------------------

@app.get("/api/oracle/runs")
async def get_oracle_runs() -> JSONResponse:
    """Return summary of all oracle runs from the DB."""
    if STATE.oracle_registry is None:
        return JSONResponse({"runs": [], "error": "OracleRegistry not available"})
    try:
        runs = STATE.oracle_registry.get_run_summary()
        return JSONResponse({"runs": runs})
    except Exception as exc:
        return JSONResponse({"runs": [], "error": str(exc)}, status_code=500)


@app.get("/api/oracle/landscape")
async def get_oracle_landscape() -> JSONResponse:
    """
    Return the parameter landscape: every tried value for every param,
    with accuracy and accepted flag. Used by the dashboard scatter graph.
    """
    if STATE.oracle_registry is None:
        return JSONResponse({"landscape": [], "error": "OracleRegistry not available"})
    try:
        landscape = STATE.oracle_registry.get_param_landscape()
        return JSONResponse({"landscape": landscape})
    except Exception as exc:
        return JSONResponse({"landscape": [], "error": str(exc)}, status_code=500)


@app.get("/api/oracle/history")
async def get_oracle_history(limit: int = 100) -> JSONResponse:
    """Return the most recent N iterations across all runs, in the canonical
    dashboard wire format (same shape as WebSocket iteration records)."""
    if STATE.oracle_registry is None:
        return JSONResponse({"history": STATE.iter_history[-limit:], "source": "session"})
    try:
        rows = STATE.oracle_registry.load_cross_session_history(limit=limit)
        history = [db_iteration_to_ws(row, seq) for seq, row in enumerate(rows, start=1)]
        return JSONResponse({"history": history, "source": STATE.registry_backend})
    except Exception as exc:
        return JSONResponse({"history": STATE.iter_history[-limit:], "source": "session", "error": str(exc)})


# ---------------------------------------------------------------------------
# REST — Trade Agent (Phase 2): policy race
# ---------------------------------------------------------------------------

def _build_signal_days(window: str = "oos") -> "pd.DataFrame":
    """One row per day of Oracle output + market context for the trade sim:
    date, call, confidence, skip_reason, spx_open, vix."""
    import pandas as pd
    frame = STATE.features_oos if window == "oos" else STATE.features_is._append(STATE.features_oos)
    results = run_oracle_backtest(frame, STATE.oracle, "outcome")
    raw = results.raw

    spx = STATE.client.spx_daily
    vix = STATE.client.vix
    rows = []
    for _, row in raw.iterrows():
        ts = pd.Timestamp(row["date"]).normalize()
        if ts not in spx.index or ts not in vix.index:
            continue
        rows.append({
            "date": str(ts.date()),
            "call": row["predicted"],
            "confidence": float(row["confidence"]),
            "skip_reason": row.get("skip_reason", "") or "",
            "spx_open": float(spx.loc[ts, "open"]),
            "vix": float(vix.loc[ts, "vix_close"]),
        })
    return pd.DataFrame(rows)


def _spx_scaled_bars(date_str: str):
    """1-min SPX-scaled RTH close path for one day (the trade sim's input)."""
    import pandas as pd
    ts = pd.Timestamp(date_str)
    bars = STATE.client.get_intraday_bars(ts.date(), bar_minutes=1)
    if bars.empty:
        return None
    day_mask = pd.Index(bars.index.date) == ts.date()
    rth = bars.loc[day_mask]
    rth = rth.loc[(rth.index.time >= pd.Timestamp("09:30").time())
                  & (rth.index.time < pd.Timestamp("16:00").time())]
    if rth.empty:
        return None
    spx = STATE.client.spx_daily
    norm = ts.normalize()
    if norm not in spx.index:
        return None
    scale = float(spx.loc[norm, "open"]) / float(rth["open"].iloc[0])
    return (rth["close"] * scale).reset_index(drop=True)


@app.get("/api/trade/compare")
async def trade_policy_compare(window: str = "oos") -> JSONResponse:
    """
    Race the two trade policies on identical days with current TradeParams:
      directional_only — trade only Oracle's UP/DOWN calls
      always_in        — verticals on calls, iron condors on ambiguous days
    Oracle provides bias + confidence; the policy decides the trade.
    """
    if not STATE.ready:
        raise HTTPException(503, detail="Data pipeline not ready yet")
    if window not in ("oos", "full"):
        raise HTTPException(400, detail="window must be 'oos' or 'full'")

    loop = asyncio.get_event_loop()

    def run() -> dict:
        import pandas as pd
        signal_days = _build_signal_days(window)
        params = TradeParams()
        comparison = compare_policies(signal_days, _spx_scaled_bars, params)
        comparison["window"] = window
        comparison["days"] = len(signal_days)

        # --- debug diagnostics (always included so zero-trade issues are visible) ---
        debug: dict = {"signal_days_count": len(signal_days)}
        if not signal_days.empty:
            sample = signal_days.head(5)
            debug["sample_rows"] = sample.to_dict(orient="records")
            bars_ok, bars_none = 0, 0
            for date_str in sample["date"].tolist():
                b = _spx_scaled_bars(str(date_str))
                if b is None or (hasattr(b, "__len__") and len(b) == 0):
                    bars_none += 1
                else:
                    bars_ok += 1
            debug["sample_bars_ok"] = bars_ok
            debug["sample_bars_none"] = bars_none
            # call distribution so we can see UP/DOWN vs NEUTRAL/SKIP ratio
            debug["call_counts"] = signal_days["call"].value_counts().to_dict()
        else:
            # signal_days empty — diagnose why
            spx = STATE.client.spx_daily
            vix = STATE.client.vix
            debug["spx_rows"] = len(spx)
            debug["vix_rows"] = len(vix)
            debug["features_oos_rows"] = len(STATE.features_oos) if STATE.features_oos is not None else 0
            # check a few raw rows from oracle backtest
            try:
                from oracle.backtest import run_oracle_backtest
                raw = run_oracle_backtest(STATE.features_oos, STATE.oracle, "outcome").raw
                debug["raw_rows"] = len(raw)
                if not raw.empty:
                    sample_raw = raw.head(3)
                    filtered_out = 0
                    for _, r in sample_raw.iterrows():
                        ts = pd.Timestamp(r["date"]).normalize()
                        if ts not in spx.index or ts not in vix.index:
                            filtered_out += 1
                    debug["sample_raw_dates"] = [str(pd.Timestamp(r["date"]).date()) for _, r in sample_raw.iterrows()]
                    debug["sample_filtered_out"] = filtered_out
            except Exception as exc:
                debug["raw_error"] = str(exc)
        comparison["debug"] = debug
        return comparison

    result = await loop.run_in_executor(None, run)
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# REST — Downloadable status report
# ---------------------------------------------------------------------------

def _generate_report_sync() -> str:
    """Assemble the full markdown status report (runs in executor — heavy)."""
    is_results = run_oracle_backtest(STATE.features_is, STATE.oracle, "outcome")

    oos_results = None
    if STATE.features_oos is not None and len(STATE.features_oos) > 10:
        oos_results = run_oracle_backtest(STATE.features_oos, STATE.oracle, "outcome")

    try:
        eligibility = DEFAULT_GATE.evaluate(is_results, oos_results).to_dict()
    except Exception as exc:
        LOGGER.warning("Eligibility evaluation failed for report: %s", exc)
        eligibility = None

    try:
        full = STATE.features_is._append(STATE.features_oos)
        walkforward = run_walk_forward(full, STATE.oracle, n_folds=6, outcome_col="outcome").to_dict()
    except Exception as exc:
        LOGGER.warning("Walk-forward failed for report: %s", exc)
        walkforward = None

    days_payload = None
    if oos_results is not None and oos_results.raw is not None and not oos_results.raw.empty:
        days = []
        for _, row in oos_results.raw.iterrows():
            days.append({
                "date":        str(row["date"].date() if hasattr(row["date"], "date") else row["date"]),
                "call":        row["predicted"],
                "actual":      row["actual"],
                "correct":     bool(row["correct"]),
                "skipped":     bool(row["skipped"]),
                "skip_reason": row.get("skip_reason", ""),
                "confidence":  round(float(row["confidence"]), 4),
                "up_score":    round(float(row["up_score"]), 4),
                "down_score":  round(float(row["down_score"]), 4),
                "neutral_score": round(float(row["neutral_score"]), 4),
                "lean":        row.get("lean", "NONE"),
            })
        days_payload = {
            "days": days,
            "skip_rate": round(oos_results.skip_rate, 4),
            "skip_reasons": oos_results.skip_reasons,
        }

    run_summary = None
    if STATE.oracle_registry is not None:
        try:
            run_summary = STATE.oracle_registry.load_cross_session_history(limit=20)
        except Exception:
            pass

    return build_report(
        params=STATE.oracle.get_params(),
        is_results=is_results,
        oos_results=oos_results,
        eligibility=eligibility,
        walkforward=walkforward,
        days_payload=days_payload,
        run_summary=run_summary,
        last_sweep=STATE.last_sweep,
        registry_backend=STATE.registry_backend,
        is_rows=len(STATE.features_is) if STATE.features_is is not None else 0,
        oos_rows=len(STATE.features_oos) if STATE.features_oos is not None else 0,
    )


@app.get("/api/report")
async def get_report() -> Response:
    if not STATE.ready:
        raise HTTPException(status_code=503, detail="Pipeline not ready")
    loop = asyncio.get_event_loop()
    report_text = await loop.run_in_executor(None, _generate_report_sync)
    return Response(
        content=report_text,
        media_type="text/plain",
        headers={"Content-Disposition": "attachment; filename=oracle_report.txt"},
    )


# ---------------------------------------------------------------------------
# REST — Agent (Execution Agent paper trading monitor)
# ---------------------------------------------------------------------------

def _agent_db_query(sql: str, params: tuple = ()) -> list[dict]:
    """Run a read-only query against the shared PostgreSQL database."""
    import psycopg2
    import psycopg2.extras
    import os
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        return []
    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        LOGGER.warning("Agent DB query failed: %s", exc)
        return []


@app.get("/api/agent/trades")
async def get_agent_trades(limit: int = 100) -> JSONResponse:
    """Return recent paper trades from the execution agent."""
    loop = asyncio.get_event_loop()

    def _query():
        rows = _agent_db_query(
            """
            SELECT id, trade_date, direction, spread_type,
                   spread_width, contracts,
                   entry_credit_pts, entry_credit_usd, max_loss_usd,
                   spx_price_at_entry, vix_at_entry, oracle_confidence,
                   exit_debit_pts, exit_debit_usd,
                   realized_pnl_usd, exit_reason, exit_time,
                   status, is_paper, created_at
            FROM trades
            ORDER BY created_at DESC NULLS LAST
            LIMIT %s
            """,
            (limit,),
        )
        for r in rows:
            for k in ("exit_time", "trade_date", "created_at"):
                if r.get(k) is not None and hasattr(r[k], "isoformat"):
                    r[k] = r[k].isoformat()
            for k in ("entry_credit_pts", "entry_credit_usd", "max_loss_usd",
                      "exit_debit_pts", "exit_debit_usd", "realized_pnl_usd",
                      "oracle_confidence", "spx_price_at_entry", "vix_at_entry",
                      "spread_width"):
                if r.get(k) is not None:
                    r[k] = float(r[k])
        return rows

    rows = await loop.run_in_executor(None, _query)
    return JSONResponse({"trades": rows, "count": len(rows)})


@app.get("/api/agent/equity")
async def get_agent_equity(days: int = 90) -> JSONResponse:
    """Return equity snapshots for the equity curve chart."""
    loop = asyncio.get_event_loop()

    def _query():
        rows = _agent_db_query(
            """
            SELECT snapshot_date, equity, daily_pnl, daily_trades, note
            FROM equity_snapshots
            ORDER BY snapshot_date DESC
            LIMIT %s
            """,
            (days,),
        )
        for r in rows:
            if r.get("snapshot_date") is not None and hasattr(r["snapshot_date"], "isoformat"):
                r["snapshot_date"] = r["snapshot_date"].isoformat()
            for k in ("equity", "daily_pnl"):
                if r.get(k) is not None:
                    r[k] = float(r[k])
        return list(reversed(rows))  # chronological order for charts

    rows = await loop.run_in_executor(None, _query)
    return JSONResponse({"equity": rows})


@app.get("/api/agent/events")
async def get_agent_events(limit: int = 50) -> JSONResponse:
    """Return recent portfolio events (skips, halts, etc.) from the execution agent."""
    loop = asyncio.get_event_loop()

    def _query():
        rows = _agent_db_query(
            """
            SELECT id, event_type, event_time, details, created_at
            FROM portfolio_events
            ORDER BY event_time DESC NULLS LAST
            LIMIT %s
            """,
            (limit,),
        )
        for r in rows:
            for k in ("event_time", "created_at"):
                if r.get(k) is not None and hasattr(r[k], "isoformat"):
                    r[k] = r[k].isoformat()
            # details is JSONB — psycopg2 returns a dict; convert strings just in case
            if isinstance(r.get("details"), str):
                try:
                    import json as _json
                    r["details"] = _json.loads(r["details"])
                except Exception:
                    pass
        return rows

    rows = await loop.run_in_executor(None, _query)
    return JSONResponse({"events": rows})




@app.post("/api/promote")
async def promote_strategy(request: Request) -> JSONResponse:
    """
    Promote a strategy candidate to 'active' so the execution agent picks it up.

    Body (JSON):
        strategy_id: int    (optional) -- if omitted, promotes the best candidate
                                          by directional_precision
        trade_params: dict  (optional) -- override default trade params
        notes: str          (optional)

    Returns the promoted strategy row.
    """
    loop = asyncio.get_event_loop()
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    strategy_id: int | None = body.get("strategy_id")
    trade_params: dict | None = body.get("trade_params")
    notes: str | None = body.get("notes")

    def _promote():
        from strategy.registry import StrategyRegistry
        reg = StrategyRegistry()

        # If no strategy_id given, pick the best candidate
        if not strategy_id:
            best = reg.get_best_oracle_params()
            if best is None:
                raise ValueError("No strategy candidates found. Run Oracle and save a candidate first.")
            sid = best["id"]
        else:
            sid = strategy_id

        ok = reg.promote_strategy(sid, trade_params=trade_params, notes=notes)
        if not ok:
            raise ValueError(f"Strategy id={sid} not found in strategies table")

        active = reg.get_active_strategy()
        return active

    try:
        result = await loop.run_in_executor(None, _promote)
        LOGGER.info("Promoted strategy id=%s to active", result.get("id"))
        return JSONResponse({"status": "ok", "strategy": result})
    except ValueError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=400)
    except Exception as exc:
        LOGGER.error("promote_strategy failed: %s", exc)
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)

# ---------------------------------------------------------------------------
# Static files + SPA fallback
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=str(DASHBOARD_DIR), html=True), name="static")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    args = parser.parse_args()
    uvicorn.run("server:app", host=args.host, port=args.port, reload=False)
