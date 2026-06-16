"""
tradeagent — Phase 2 Trade Agent for InfiniteLoop.

Sits downstream of Oracle. For each day Oracle calls UP or DOWN (NEUTRAL =
stand aside in v1), the trade agent selects a defined-risk vertical spread:

  UP   -> sell bull put spread (short OTM put, long further OTM put)
  DOWN -> sell bear call spread (short OTM call, long further OTM call)

Strike distance is expressed in EXPECTED-MOVE MULTIPLES, interpolated by
Oracle's confidence (high confidence -> tighter strikes -> more premium).
Contract count is NEVER optimized — it is derived from the hard risk rules
(max loss <= 10% of equity; 1 contract until equity > $15,000).

Modules:
  simulator.py — path-based spread P&L simulation + portfolio backtest
"""
