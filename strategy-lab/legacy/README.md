# Legacy (pre-Oracle) pipeline — retired June 2026

These files are from the original single-loop ORB pipeline and are NOT part of
the current system. The live pipeline is:

    server.py  →  oracle/hermes_loop.py  →  strategy/oracle_registry.py  →  dashboard/index.html

- `loop.py`             — old CLI discovery loop (ORBDirectionStrategy, no per-iteration persistence)
- `dashboard.py`        — old terminal/HTML dashboard renderer used by loop.py
- `phase1b_dashboard.py`— old standalone dashboard experiment

Kept for reference only. Do not extend; delete once the Oracle pipeline is validated.
