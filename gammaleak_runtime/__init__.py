"""GammaLeak runtime helpers — currently only `io_logs` (the per-symbol CSV
disk writer).

The other former members (`math_stats`, `aggressor`, `verdict`, `oi_chain`,
`global_indices`) were relocated to `analytics/`, `orderflow/`, and `signals/`
during the post-monolith refactor, and the shim re-exports here have been
removed now that no caller relies on them.

`io_logs` stays in this package because it doesn't fit cleanly into any
of the new buckets (engine / core / analytics / signals / orderflow / ui).
Could be moved to a future `persistence/` package or to `ui/io_logs.py` later.
"""
