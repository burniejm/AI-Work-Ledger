"""Append-only capture_checkpoints writes.

`checkpoint_key` is unique (migration 003), so a replayed hook payload
building the same key is a silent no-op via `INSERT OR IGNORE` rather than
a duplicate row.
"""
from work_ledger import entities, ids, timeutil
from work_ledger.db import connection


def checkpoint(conn, *, session_id, hook_event, key, status, detail=None,
               started_at, duration_ms=None, hook_mode: bool = False) -> None:
    """`hook_mode=True` threads through to `entities.current_device` and
    `connection.transaction` so a hook's checkpoint write gives up on lock
    contention within the hook budget rather than the full non-hook retry
    schedule."""
    # current_device() opens (and commits) its own transaction, so it must
    # complete before this function opens its own (connection.transaction
    # is not reentrant -- see entities.py's module docstring).
    device_id = entities.current_device(conn, hook_mode=hook_mode)
    now = timeutil.utc_now_iso()
    with connection.transaction(conn, hook_mode=hook_mode):
        conn.execute(
            "INSERT OR IGNORE INTO capture_checkpoints("
            "id, session_id, hook_event, checkpoint_key, status, detail, started_at, "
            "finished_at, duration_ms, origin_device_id, created_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ids.new_id(), session_id, hook_event, key, status, detail, started_at,
             now, duration_ms, device_id, now),
        )
