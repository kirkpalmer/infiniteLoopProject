# Session Notes — June 10, 2026: Persistence & Dashboard Refactor

Root-cause fixes for "iterations are not happening" and "the website keeps breaking."
All changes verified by `strategy-lab/tests/test_oracle_persistence.py` (5 tests passing).

---

## Root causes found

1. **`.env` was never loaded.** No `load_dotenv()` existed anywhere. Unless `DATABASE_URL`
   happened to be set in the shell, `OracleRegistry()` failed at server startup, logged one
   quiet warning, and the server silently ran in "ephemeral mode" — zero DB writes, zero
   cross-session memory for Hermes.
2. **Dashboard dropped every run after the first.** The frontend deduped iterations by
   iteration number, which restarts at 1 every run — so all iterations from run 2 onward
   were silently discarded by the UI.
3. **Three different iteration record shapes** (dataclass / short-key WS dict / long-key DB
   dict) with hand-built conversions caused key-drift bugs: `accepted_count` read
   `"accepted"` where the key was `"ok"` (always 0); `/api/strategies` read `h["accuracy"]`
   where the key was `"acc"` (KeyError → page broke).
4. **Hermes parse failures counted toward convergence patience** — 8 bad JSON responses in
   a row ended the loop "converged" having evaluated nothing.
5. **DB seeding was dead code** — server always passed `initial_strategy`, and history was
   never hydrated into the dashboard at startup.

## What changed

| Area | Change |
|---|---|
| `strategy-lab/config.py` (new) | Loads project-root `.env` once; logs a secret-free config report. Imported first by `server.py`. |
| `strategy-lab/oracle/records.py` (new) | THE canonical iteration wire format. `record_to_ws()` / `db_iteration_to_ws()` are the only ways to build dashboard iteration dicts. `n` = global sequence (unique across runs/sessions), `run_iter` = run-local number. |
| `strategy/oracle_registry.py` | Rewritten: shared `BaseOracleRegistry` + Postgres backend + **SQLite fallback** (`strategy-lab/data/oracle_history.db`). `open_oracle_registry()` factory returns whichever is reachable — history is never silently lost. Added `ping()`, `counts()`, `get_tried_values()` (every value ever tested, with accuracy, for Hermes prompts). |
| `oracle/hermes_loop.py` | Parse failures get their own counter (`MAX_PARSE_FAILURES=6`) + corrective retry prompt; they no longer eat convergence patience. Duplicate proposals (value already tried, incl. prior sessions) skip the backtest and tell Hermes what it scored. `new_value` coerced to float. `finish_run()` guaranteed via try/finally. Prompts now include the per-param tried-values map. |
| `server.py` | Loads config; opens registry via factory (never None); seeds Oracle from best-ever persisted params at startup; hydrates last 200 iterations into the dashboard; global iteration sequence numbers; chat-driven param tests are persisted too (run notes "chat tests"); new **`/api/health`** endpoint; fixed key-drift bugs. |
| `dashboard/index.html` | Topbar now shows live **DB chip** (Postgres ✓ / SQLite fallback / NOT PERSISTING) and **Hermes chip** (model ✓ / offline). Per-run progress counter (`RUN_DONE`) instead of total history length. Boot also pulls `/api/status` and `/api/health` (every 30 s). |
| `strategy-lab/legacy/` | Old pre-Oracle pipeline retired: `loop.py`, `dashboard.py`, `phase1b_dashboard.py`. Do not extend. |
| `tests/test_oracle_persistence.py` (new) | Registry round-trip, sqlite fallback, wire-format identity, loop persistence, cross-session duplicate skipping. Run with `python -m pytest tests/ -v` from `strategy-lab/`. |

## How cross-session learning now works

1. Every evaluated iteration (loop AND chat tests) is written to the registry immediately.
2. On every new run, the loop loads: full tried-values map (per param: value → accuracy →
   accepted), recent cross-session history, and best-ever params.
3. Hermes prompts include all of it; the parser/duplicate-guard enforce it even when
   Hermes ignores instructions.
4. On server restart, Oracle seeds from the best-ever persisted params and the dashboard
   chart shows past sessions.

## Addendum (June 11): Dual-layer optimization — Optuna for numbers, LLM for structure

Decision: LLM-guided coordinate ascent is the wrong tool for tuning 9 numeric
thresholds. The single-variable rule's intent (attribution) is preserved by
logging every trial; overfitting protection comes from the OOS lock + active-day
minimums, not step size.

- **`oracle/optimize.py`** — Optuna TPE sweep over all 9 thresholds. IS data only.
  Objective = **macro accuracy** (mean of UP/DOWN/NEUTRAL accuracy — raw accuracy
  rewards ignoring minority classes). Guardrails: skip_rate ≤ 40%, ≥ 200 active
  days (auto-lowered for small datasets). `neutral_band_pct` searched as a
  fraction of `gap_threshold_pct` so the constraint holds by construction.
  Seeds trial 0 from best-known params. Every trial → `oracle_iterations`
  (param_changed="sweep"). Reports fANOVA param importances.
- **`POST /api/oracle/sweep`** + "⚡ Run Sweep" button — shares run/stop machinery
  with the Hermes loop; trials stream over the same WebSocket; best params are
  adopted as the live Oracle on completion.
- **Role split going forward**: Optuna = numeric tuning; Hermes/chat = in-app
  analyst; Claude (Cowork sessions) = strategist for structural/feature changes,
  informed by sweep landscape + confusion matrices. CLI: `python -m oracle.optimize --trials 300`.
- New deps: `optuna`, `scikit-learn` (fANOVA importances).
- Tests: `tests/test_optimize.py` (5 tests — persistence, seeding, stop, seed
  inversion, macro-vs-raw objective). Suite total: 11 passing.

## Addendum (June 11, later): LOOKAHEAD BIAS fixed + two dead params wired in

Investigating a Hermes suggestion exposed three real defects:

1. **Lookahead bias (critical):** `data/loader.py` computed the `orb_breakout`
   feature from the DAILY CLOSE (`current_close > orb_high`). The feature is
   supposed to be known ~45 min into the session — using the close leaked the
   day's outcome into the feature set and inflated every backtest/sweep
   accuracy number. Fixed: breakout is now measured at `post_orb_close`, the
   price `ORB_CONFIRM_MINUTES` (15) after the ORB window closes. **All accuracy
   numbers from before this fix are inflated and must be re-established by a
   fresh sweep.**
2. **`orb_breakout_pct` was a dead parameter** — declared in OracleParams,
   searched by the sweep, suggested by Hermes, but never read by the
   classifier. Now live: breakout requires clearing the ORB level by this
   margin (computed in `classifier._orb_direction()` from `post_orb_close`).
3. **`vwap_slope_threshold` was also dead.** Now live: new feature
   `vwap_slope_first30` (relative VWAP slope across the first 30 min), used as
   a second tiebreaker in `_rule_direction` when gap/ORB/delta and prior-day
   signals are all ambiguous.

Also fixed: corrupted function name `split_train_oosit_train_oos` → `split_train_oos`.
Old cached feature frames without the new columns degrade gracefully (classifier
falls back to the legacy `orb_breakout` string).

Tests: `tests/test_classifier_orb.py` (param liveness, no-lookahead, fallback).
Suite total: 15 passing.

Chat fine-tuning workflow (already built, just needs the right phrasing): tell
Oracle in the Hermes Chat tab "test <param> at <value>" — the server backtests
immediately, accepts if it beats current by >0.2%, and persists the result to
the registry either way.

## Addendum (June 11, evening): Walk-forward validation + per-day confidence

First honest OOS run (post-lookahead-fix): IS 58.6%, OOS 48.7%, drift 9.8%
("pass" but borderline), and critically only **39 active OOS days** (~80%
skipped) — the headline problem to diagnose. UP held 61.5% OOS; NEUTRAL was
at chance (33.3%).

New tooling built in response:

- **Skip-reason tracking** — `OracleSignal.skip_reason`
  (missing_features / vix_above_high / vix_below_low), surfaced through
  `OracleResults.skip_reasons`, the OOS endpoint, walk-forward folds, and the
  daily signals view. Answers "why is Oracle abstaining?" — distinguishes
  vol-filter curve-fitting from data gaps.
- **Walk-forward validation** — `oracle/walkforward.py` +
  `GET /api/oracle/walkforward?folds=6` + dashboard card. Evaluates FIXED
  params across consecutive time windows over the full history. Folds with
  <15 active days are flagged thin and excluded from the mean. Verdicts:
  stable / unstable_across_regimes / mostly_thin_folds / at_least_one_fold_at_chance.
- **Per-day confidence (trade agent input)** — `GET /api/oracle/days?window=oos`
  + "Daily Signals" dashboard card: date, call, actual, ✓/✗, confidence,
  per-class scores, lean, skip reason — plus **accuracy by confidence
  quartile** (`confidence_buckets()` in oracle/backtest.py). If accuracy rises
  with confidence, the Phase 2 trade agent gates/sizes on it.

NEUTRAL decision (discussed, not yet changed): keep the label — "inside the
expected move" IS the iron-condor-day definition. Near-term trading plan is
class-selective: trade UP/DOWN verticals where edge shows; treat a NEUTRAL
call as "stand aside" until NEUTRAL accuracy/confidence justifies condors.

Tests: tests/test_walkforward.py (5 tests). Suite total: 20 passing.

## Addendum (June 11, night): Conviction gates + data plan

Walk-forward results (191 usable days only!): verdict unstable_across_regimes,
mean 63.6% ± 9.0%; NEUTRAL never predicted (0% all folds — sweep crushed
neutral_band); confidence buckets: <0.5 → 7.7% accuracy (incl. exact 0.50/0.50
coin-flip ties), ≥0.5 → 60–71% (~65% combined on 26 OOS days); UP edge durable
(63–80% every fold), DOWN decaying (78%→50% over 4 years); fold-3 75% skip =
vol_filter_low walling off the low-VIX 2023 regime.

Two new sweepable params (both default 0.0 = old behavior):
- `min_score_separation` — directional call with |up−down| score gap below this
  becomes a NEUTRAL call (tests "ambiguous = sideways day"; revives NEUTRAL).
- `min_confidence` — UP/DOWN calls below this confidence are SKIPPED
  (skip_reason="low_confidence"). NEUTRAL calls exempt. MAX_SKIP_RATE guardrail
  prevents the sweep from abstaining its way to a score.

Data decision: buy continuous 1-min ES history (FirstRateData, 15yr,
continuous + individual contracts; ~2008-present). ES futures, NOT the SPX
index dataset — loader needs overnight session + volume for delta/orderflow
features, which a cash index lacks. TODO when file arrives: `data/firstrate.py`
importer feeding the same intraday-bars store as barchart_csv.py.

Tests: 3 new gate tests in test_classifier_orb.py. Suite total: 23 passing.

## Addendum (June 11, late): Phase 2 begins — Trade Agent simulator + report download

Post-gate sweep results: walk-forward verdict **stable** (66.2% ± 3.5, was
unstable 63.6% ± 9.0); poison confidence bucket eliminated (worst bucket now
57.1%, was 7.7%); NEUTRAL calls revived via min_score_separation. Eligibility
7/9 — failing only sample-size criteria (97 active days < 200; skip 36.2% >
20%), both resolved by the data purchase.

New: **⬇ Report** button → `GET /api/oracle/report` downloads a full markdown
status report (params, IS/OOS + confusion + skip reasons, eligibility table,
walk-forward folds, confidence buckets, daily OOS signals, sweep importances,
run history). Copy saved to strategy-lab/logs/. Built by `oracle/report.py`.

**Trade Agent v1 design (Kirk-approved):**
- Strike distance in EXPECTED-MOVE MULTIPLES interpolated by Oracle confidence
  (high conf → tight strikes), NOT fixed points — scales across vol regimes.
- Contracts NEVER optimized — derived: 1 contract until equity > $15k, then
  floor(equity × 10% / max_loss); trade skipped if 1 contract exceeds budget.
- UP/DOWN verticals only; NEUTRAL = stand aside until it earns condors.

`tradeagent/simulator.py` — path-based spread simulator: BS repricing every
5 min along actual intraday bars, stop checked before target (conservative),
15:45 forced exit, commissions, portfolio compounding from $5k.
NOTE: legacy `data/options.simulate_spread()` was discovered to be broken —
it never consulted the price path (every trade "won"). Do not use it; the
tradeagent simulator replaces it. BS-with-VIX ignores skew → credits slightly
optimistic; rankings valid, absolute P&L validated in paper trading.

Data: Barchart Premier CAN supply 1-min history (~10 yrs) but caps 10,000
records/request (~1-2 weeks of futures 1-min per download) — FirstRateData
single file remains the low-effort option.

Next: trade-param Optuna sweep + /api/trade endpoints + dashboard card.
Tests: tests/test_tradeagent.py (8). Suite total: 33 passing.

## Addendum (June 12): FirstRateData integrated — 1,612 days live

Kirk purchased FirstRateData ES 1-min ratio-adjusted continuous (2008→2026-06-10,
6.45M rows) → `strategy-lab/data/raw/firstrate/ES_full_1min_continuous_ratio_adjusted.txt`.

- **`data/firstrate.py`** — parses once (~40s), caches to `es_1min_cache.pkl`
  (auto-invalidated on source mtime change). Loads from 2017-06 by default.
- **`MarketDataClient`** prefers FirstRate over Barchart CSVs automatically
  (`es_source` attr); per-day slicing uses sorted-index binary search (the
  old per-call tz_convert would have cost minutes at this scale).
- **TWO LONG-STANDING LOADER BUGS FIXED** (predate FirstRate, affected all
  prior results): (1) every MONDAY was silently dropped — the "previous day"
  logic picked Sunday (Globex-only, no RTH bars) → empty prev_rth → row
  skipped; rebuilt `_session_parts` to pick the latest prior day WITH RTH
  bars; (2) Sunday-night Globex bars were excluded from Monday's overnight
  session — overnight is now everything between prev 16:00 close and
  current 09:30 open. (3) `get_intraday_bars` now reaches back 5 calendar
  days so holiday Mondays also resolve.
- **Server window**: DATA_START=2018-01-01, DATA_END=2026-06-10. Effective
  start bounded by the SPX daily CSV (currently 2020-01-02) — TODO Kirk:
  re-download Barchart SPX daily back to 2018 for ~500 more days.
- **Result**: 1,612 labeled feature days (was 191), Mon-Fri balanced
  (299/333/332/324/324). Classes: UP 810 / DOWN 693 / NEUTRAL 109 (NEUTRAL
  is only 6.8% of days — macro objective weights a rare class; revisit).
  IS 1,289 / OOS 323 locked.
- **Default-param preview on full data**: 57.5% overall, walk-forward STABLE
  (8 folds, ±2.5%, zero thin folds), UP 71-82% in EVERY fold incl. COVID and
  the 2022 bear; DOWN ~43% with defaults; expect sweep params to improve.

Next: Kirk restarts server (startup builds 1,612-day features in ~1 min),
runs full re-sweep (each trial now backtests 1,289 IS days — expect a slower
sweep), walk-forward, daily signals, downloads the report.

## Addendum (June 12, later): Precision is the metric — objective realigned

Full-data sweep exposed a flaw in the macro-accuracy objective: with NEUTRAL
only ~7% of days, the optimizer gamed macro by spamming NEUTRAL calls (496
calls vs 66 actual NEUTRAL days IS), tanking headline accuracy to 44%. BUT
the confusion matrices revealed the real story — PRECISION of directional
calls (the only days we trade) was excellent: **OOS UP 80.5% (62/77), DOWN
77.6% (52/67) → 79% hit rate on 144 held-out trade signals**, drift 2.6%,
confidence buckets 86.7%/74.3% above the floor. Recall ≠ what capital rides on.

Changes (all tested, 34 passing):
- `OracleResults` now exposes up/down/directional **precision** + call counts
  (computed from the confusion matrix) in summary_dict and the report.
- **Sweep objective = directional-call precision**, with guardrails: ≥8% of
  days must produce a trade signal, each direction ≥1% of days, plus the
  existing skip-rate/active-day floors. SweepResult.best_score.
- **Eligibility gate rebuilt around precision**: IS dir ≥60%, UP ≥60%,
  DOWN ≥55%; ≥150 directional calls; OOS dir ≥55%, OOS DOWN ≥50%; IS/OOS
  precision drift ≤10%. NEUTRAL criterion dropped (v1 stands aside on
  NEUTRAL); skip-rate criterion replaced by the directional-call floor
  (conviction-gating abstention is deliberate, not failure).
- Applying the new gate to the June 12 report numbers by hand: **8/8 PASS.**

## Addendum (June 12, night): Policy race — Kirk's "trade every day" validated

Architecture clarified (Kirk): **Oracle outputs bias + confidence (information,
not trade signals); the Trade Agent decides the day's best trade from it.**

Built: iron-condor path simulation (`simulate_condor_day`), trade policies
(`choose_trade`: directional_only vs always_in), `compare_policies`,
`GET /api/trade/compare?window=oos|full`, dashboard "💰 Policy Race" card with
equity-curve chart. always_in = verticals on UP/DOWN calls, condors at
condor_em_mult×EM (default 1.2) on ambiguous days; hard-skips only VIX-gate
and data-gap days. Sizing: MAX_CONTRACTS=25 absolute cap added after the
uncapped 10%-compounding produced trillion-dollar fantasy curves; added
expectancy_per_contract (compounding-免 edge measure). 38 tests passing.

**Race results (Kirk's June-12 sweep params, 5-wide spreads):**
- OOS (423 days): directional_only $5k→$9.9k (PF 4.2); always_in $5k→$135k
  (96% win, PF 7.6, maxDD 11%).
- Full history (2,114 days, 25-contract cap): directional_only $5k→$190k
  (PF 3.3, maxDD 20.7%, $31/contract); **always_in $5k→$1.79M (96% win,
  PF 6.9, maxDD 6.1%, $41/contract)** — condors 1,331/1,370 wins, and the
  condor income SMOOTHS the curve (lower drawdown than selective trading).

CAVEATS (do not skip): flat-vol Black-Scholes (VIX×1.15) ignores skew; zero
slippage; perfect fills at theoretical values; 96% win rates compound any
per-trade pricing optimism. Relative ranking (always_in > directional_only)
is trustworthy; absolute dollars are not. Paper trading (Phase 2's 30-day
requirement) is the validator. Next: trade-param sweep (em mults, width,
PT/stop, condor_em_mult), walk-forward on trade P&L, then paper wiring.

## Operational notes

- If the DB chip shows **SQLite fallback**, Postgres (Railway) was unreachable at startup —
  history is still safe locally, but check `DATABASE_URL` in `.env`.
- If the Hermes chip shows **offline**, start Ollama (`ollama serve`) and ensure the
  `hermes3` model is pulled. The loop refuses to start without it (by design).
- `/api/health` returns: registry backend + row counts, Hermes availability + model, and
  the env config report.
