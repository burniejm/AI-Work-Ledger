"""Remaining-item lifecycle: creation, reinforcement, and validated state transitions.

Every write goes through `db.connection.transaction` (BEGIN IMMEDIATE with
the standard bounded busy retry). Per the non-reentrancy note in
`entities.py`, `entities.current_device` is always called *before* opening
this module's own transaction.

Every transition records previous state, new state, timestamp, source,
confidence, explanation, and an evidence reference (`source_event_id` /
`activity_id`) in the append-only `remaining_item_transitions` table
(Spec Section 9).

Ruling 28: `OPEN_STATES` (which includes `reopened`) is the single
definition of "still open" for a remaining item. `query.scoped` (Task 18),
`query.status` (Task 18), and `contradictions.rule_jira_done_open_remaining`
(Task 30) all import this tuple rather than restating the list.
"""
import sqlite3

from work_ledger import entities, ids, timeutil
from work_ledger.db import connection

__all__ = [
    "STATES",
    "ALLOWED_TRANSITIONS",
    "OPEN_STATES",
    "InvalidTransition",
    "create",
    "reinforce",
    "transition",
    "history",
    "open_items",
]

STATES = ("open", "in_progress", "blocked", "waiting", "completed", "superseded", "cancelled", "reopened")

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "open": {"in_progress", "blocked", "waiting", "completed", "cancelled", "superseded"},
    "in_progress": {"blocked", "waiting", "completed", "cancelled", "superseded", "open"},
    "blocked": {"open", "in_progress", "completed", "cancelled", "superseded"},
    "waiting": {"open", "in_progress", "completed", "cancelled", "superseded"},
    "completed": {"reopened"},
    "cancelled": {"reopened"},
    "superseded": {"reopened"},
    "reopened": {"in_progress", "blocked", "waiting", "completed", "cancelled", "superseded", "open"},
}

# Ruling 28: the single source of truth for "still open" predicates.
OPEN_STATES = ("open", "in_progress", "blocked", "waiting", "reopened")

# States that resolve an item (transition.transition sets resolved_at on these).
_RESOLVING_STATES = ("completed", "cancelled", "superseded")


class InvalidTransition(Exception):
    """Raised when a requested `to_state` is not reachable from the item's current state."""


def create(conn, *, title: str, normalized_key: str, work_item_id=None, client_id=None,
           project_id=None, detail=None, kind: str = "remaining", state: str = "open",
           confidence: str, source: str, occurred_at: str, source_event_id=None,
           activity_id=None, explanation=None) -> str:
    """Create a new remaining item and write its initial transition (from_state=None)."""
    device_id = entities.current_device(conn)
    now = timeutil.utc_now_iso()
    item_id = ids.new_id()
    with connection.transaction(conn):
        conn.execute(
            "INSERT INTO remaining_items(id, work_item_id, client_id, project_id, title, detail, "
            "normalized_key, state, kind, mention_count, first_seen_at, last_seen_at, resolved_at, "
            "confidence, source, in_inbox, origin_device_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,1,?,?,NULL,?,?,?,?,?,?)",
            (item_id, work_item_id, client_id, project_id, title, detail, normalized_key, state,
             kind, occurred_at, occurred_at, confidence, source, int(confidence == "low"),
             device_id, now, now),
        )
        transition_id = ids.new_id()
        conn.execute(
            "INSERT INTO remaining_item_transitions(id, remaining_item_id, from_state, to_state, "
            "occurred_at, source, confidence, explanation, source_event_id, activity_id, "
            "origin_device_id, created_at) VALUES (?,?,NULL,?,?,?,?,?,?,?,?,?)",
            (transition_id, item_id, state, occurred_at, source, confidence, explanation,
             source_event_id, activity_id, device_id, now),
        )
    return item_id


def reinforce(conn, item_id: str, *, occurred_at: str, source_event_id=None, detail=None) -> None:
    """Bump `mention_count`, advance `last_seen_at`, optionally lengthen `detail`.

    Writes no transition.
    """
    now = timeutil.utc_now_iso()
    with connection.transaction(conn):
        row = conn.execute(
            "SELECT last_seen_at, detail FROM remaining_items WHERE id=?", (item_id,)
        ).fetchone()
        new_last_seen = occurred_at if occurred_at > row["last_seen_at"] else row["last_seen_at"]

        sets = ["mention_count = mention_count + 1", "last_seen_at=?", "updated_at=?"]
        params = [new_last_seen, now]
        if detail is not None and (row["detail"] is None or len(detail) > len(row["detail"])):
            sets.append("detail=?")
            params.append(detail)
        params.append(item_id)
        conn.execute(f"UPDATE remaining_items SET {', '.join(sets)} WHERE id=?", params)


def transition(conn, item_id: str, to_state: str, *, occurred_at: str, source: str,
                confidence: str, explanation: str, source_event_id=None, activity_id=None) -> None:
    """Move `item_id` to `to_state`, recording full provenance.

    Raises `InvalidTransition` when `to_state` is not reachable from the
    item's current state. Sets `resolved_at = occurred_at` (event time,
    mirroring first_seen_at/last_seen_at -- not wall-clock time) when
    `to_state` is one of completed/cancelled/superseded, and clears it on
    `reopened`.
    """
    device_id = entities.current_device(conn)
    now = timeutil.utc_now_iso()
    with connection.transaction(conn):
        row = conn.execute(
            "SELECT state FROM remaining_items WHERE id=?", (item_id,)
        ).fetchone()
        from_state = row["state"]
        if to_state not in ALLOWED_TRANSITIONS.get(from_state, set()):
            raise InvalidTransition(f"cannot transition from {from_state!r} to {to_state!r}")

        sets = ["state=?", "updated_at=?"]
        params = [to_state, now]
        if to_state in _RESOLVING_STATES:
            sets.append("resolved_at=?")
            params.append(occurred_at)
        elif to_state == "reopened":
            sets.append("resolved_at=NULL")
        params.append(item_id)
        conn.execute(f"UPDATE remaining_items SET {', '.join(sets)} WHERE id=?", params)

        transition_id = ids.new_id()
        conn.execute(
            "INSERT INTO remaining_item_transitions(id, remaining_item_id, from_state, to_state, "
            "occurred_at, source, confidence, explanation, source_event_id, activity_id, "
            "origin_device_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (transition_id, item_id, from_state, to_state, occurred_at, source, confidence,
             explanation, source_event_id, activity_id, device_id, now),
        )


def history(conn, item_id: str) -> list[sqlite3.Row]:
    """Return this item's transitions, ordered by `occurred_at`."""
    return conn.execute(
        "SELECT * FROM remaining_item_transitions WHERE remaining_item_id=? ORDER BY occurred_at",
        (item_id,),
    ).fetchall()


def open_items(conn, *, work_item_id=None, client_id=None, project_id=None,
                include_inbox: bool = False) -> list[sqlite3.Row]:
    """Return live remaining items whose state is in `OPEN_STATES`.

    Filters by `work_item_id`/`client_id`/`project_id` when supplied, and
    excludes `in_inbox=1` rows unless `include_inbox=True`.
    """
    placeholders = ",".join("?" for _ in OPEN_STATES)
    clauses = ["deleted_at IS NULL", f"state IN ({placeholders})"]
    params = list(OPEN_STATES)

    if work_item_id is not None:
        clauses.append("work_item_id=?")
        params.append(work_item_id)
    if client_id is not None:
        clauses.append("client_id=?")
        params.append(client_id)
    if project_id is not None:
        clauses.append("project_id=?")
        params.append(project_id)
    if not include_inbox:
        clauses.append("in_inbox=0")

    query = f"SELECT * FROM remaining_items WHERE {' AND '.join(clauses)}"
    return conn.execute(query, params).fetchall()
