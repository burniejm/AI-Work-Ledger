"""Read-only history and status reports over the ledger.

Every module in this package only reads from SQLite (no writes, no
transactions) and returns plain dicts/lists suitable for `json.dumps` or
`query.render.render_report`. Workday bucketing is always computed at query
time via `work_ledger.timeutil` -- never materialized (Global Constraints).
"""
