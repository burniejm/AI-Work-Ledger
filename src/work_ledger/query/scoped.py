"""Scoped queries: remaining work, blockers, stale work items, evidence
lookup, and the low-confidence inbox (Spec 5.4, 20.23, Task 18).

Every "still open" predicate here is `remaining.OPEN_STATES` (Ruling 28,
via `remaining.open_items`) -- never a locally restated state list.
Evidence rows are returned as stored -- commit shas, file paths, and
already-redacted, length-capped excerpts -- never full file contents
(Global Constraints: evidence storage).
"""
from datetime import date

from work_ledger import remaining, timeutil

__all__ = [
    "remaining_report", "blocked_report", "stale_report",
    "evidence_report", "inbox_report", "item_summary", "open_contradictions",
]


def item_summary(conn, row) -> dict:
    """Enrich a `remaining_items` row with its client/project/work-item labels."""
    client_name = None
    if row["client_id"]:
        r = conn.execute("SELECT name FROM clients WHERE id=?", (row["client_id"],)).fetchone()
        client_name = r["name"] if r is not None else None

    project_name = None
    if row["project_id"]:
        r = conn.execute("SELECT name FROM projects WHERE id=?", (row["project_id"],)).fetchone()
        project_name = r["name"] if r is not None else None

    work_item_label = None
    if row["work_item_id"]:
        r = conn.execute(
            "SELECT external_key, display_id FROM work_items WHERE id=?", (row["work_item_id"],)
        ).fetchone()
        if r is not None:
            work_item_label = r["external_key"] or r["display_id"]

    return {
        "id": row["id"],
        "title": row["title"],
        "detail": row["detail"],
        "state": row["state"],
        "kind": row["kind"],
        "confidence": row["confidence"],
        "mention_count": row["mention_count"],
        "work_item_id": row["work_item_id"],
        "work_item": work_item_label,
        "client_id": row["client_id"],
        "client": client_name,
        "project_id": row["project_id"],
        "project": project_name,
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
    }


def _scope_filters(resolved: dict) -> dict:
    filters = {"work_item_id": None, "client_id": None, "project_id": None}
    if resolved["kind"] == "work_item":
        filters["work_item_id"] = resolved["id"]
    elif resolved["kind"] == "project":
        filters["project_id"] = resolved["id"]
    elif resolved["kind"] == "client":
        filters["client_id"] = resolved["id"]
    return filters


def _latest_explanation(conn, item_id: str, to_state: str):
    row = conn.execute(
        "SELECT explanation FROM remaining_item_transitions "
        "WHERE remaining_item_id=? AND to_state=? ORDER BY occurred_at DESC LIMIT 1",
        (item_id, to_state),
    ).fetchone()
    return row["explanation"] if row is not None else None


def open_contradictions(conn, filters: dict) -> "list[dict]":
    """Currently-open (`status='open'`) contradictions in scope (Spec 5.4:
    "current contradictions" means open ones)."""
    clauses = ["co.deleted_at IS NULL", "co.status='open'"]
    params: list = []
    if filters.get("work_item_id") is not None:
        clauses.append("co.work_item_id=?")
        params.append(filters["work_item_id"])
    elif filters.get("project_id") is not None:
        clauses.append("wi.project_id=?")
        params.append(filters["project_id"])
    elif filters.get("client_id") is not None:
        clauses.append("wi.client_id=?")
        params.append(filters["client_id"])
    rows = conn.execute(
        "SELECT co.* FROM contradictions co LEFT JOIN work_items wi ON wi.id = co.work_item_id "
        f"WHERE {' AND '.join(clauses)} ORDER BY co.detected_at",
        params,
    ).fetchall()
    return [
        {
            "id": row["id"],
            "rule": row["rule"],
            "ledger_claim": row["ledger_claim"],
            "external_claim": row["external_claim"],
            "work_item_id": row["work_item_id"],
            "detected_at": row["detected_at"],
        }
        for row in rows
    ]


def remaining_report(conn, scope=None, *, state_filter=None, include_inbox: bool = False) -> dict:
    """Open remaining items (Ruling 28: `remaining.OPEN_STATES`, including
    `reopened`) in `scope`, optionally narrowed further to one `state`."""
    from work_ledger.query import status

    resolved = status.resolve_scope(conn, scope)
    filters = _scope_filters(resolved)
    rows = remaining.open_items(conn, include_inbox=include_inbox, **filters)
    items = [item_summary(conn, row) for row in rows]
    if state_filter is not None:
        items = [item for item in items if item["state"] == state_filter]
    return {"kind": "remaining", "scope": resolved, "items": items}


def blocked_report(conn, scope=None) -> dict:
    """Open items in state `blocked` or `waiting`, with the explanation from
    their latest matching transition. A `reopened` item subsequently
    transitioned to `blocked`/`waiting` appears here like any other
    (Ruling 28: filtered through `remaining.OPEN_STATES` first)."""
    from work_ledger.query import status

    resolved = status.resolve_scope(conn, scope)
    filters = _scope_filters(resolved)
    rows = remaining.open_items(conn, **filters)
    items = []
    for row in rows:
        if row["state"] not in ("blocked", "waiting"):
            continue
        entry = item_summary(conn, row)
        entry["explanation"] = _latest_explanation(conn, row["id"], row["state"])
        items.append(entry)
    return {"kind": "blocked", "scope": resolved, "items": items}


def _work_item_last_activity(conn, work_item_id: str):
    candidates = []
    for table, column in (
        ("activities", "started_at"),
        ("remaining_items", "last_seen_at"),
        ("decisions", "decided_at"),
    ):
        row = conn.execute(
            f"SELECT MAX({column}) AS latest FROM {table} WHERE deleted_at IS NULL AND work_item_id=?",
            (work_item_id,),
        ).fetchone()
        if row is not None and row["latest"] is not None:
            candidates.append(row["latest"])
    return max(candidates) if candidates else None


def stale_report(conn, *, days: int = 14, tz: str, rollover_hour: int) -> dict:
    """Work items with open remaining work whose last activity is older
    than `days` (Ruling 28: `remaining.OPEN_STATES` defines "open")."""
    open_rows = remaining.open_items(conn)
    work_item_ids = sorted({row["work_item_id"] for row in open_rows if row["work_item_id"] is not None})

    today = timeutil.workday_of(timeutil.utc_now_iso(), tz, rollover_hour)
    today_date = date.fromisoformat(today)

    items = []
    for work_item_id in work_item_ids:
        last_activity = _work_item_last_activity(conn, work_item_id)
        if last_activity is None:
            continue
        last_workday = timeutil.workday_of(last_activity, tz, rollover_hour)
        elapsed = (today_date - date.fromisoformat(last_workday)).days
        if elapsed < days:
            continue

        row = conn.execute(
            "SELECT wi.*, c.name AS client_name, p.name AS project_name FROM work_items wi "
            "LEFT JOIN clients c ON c.id = wi.client_id "
            "LEFT JOIN projects p ON p.id = wi.project_id "
            "WHERE wi.id=?",
            (work_item_id,),
        ).fetchone()
        label = (row["external_key"] or row["display_id"]) if row is not None else None
        items.append({
            "work_item_id": work_item_id,
            "work_item": label,
            "title": row["title"] if row is not None else None,
            "client_id": row["client_id"] if row is not None else None,
            "client": row["client_name"] if row is not None else None,
            "project_id": row["project_id"] if row is not None else None,
            "project": row["project_name"] if row is not None else None,
            "last_activity_at": last_activity,
            "days_idle": elapsed,
        })

    items.sort(key=lambda i: i["days_idle"], reverse=True)
    return {"kind": "stale", "days": days, "items": items}


def _resolve_evidence_entry(conn, entry: str):
    activity = conn.execute(
        "SELECT id FROM activities WHERE id=? AND deleted_at IS NULL", (entry,)
    ).fetchone()
    if activity is not None:
        return "activity", activity["id"]

    remaining_item = conn.execute(
        "SELECT id FROM remaining_items WHERE id=? AND deleted_at IS NULL", (entry,)
    ).fetchone()
    if remaining_item is not None:
        return "remaining_item", remaining_item["id"]

    work_item = conn.execute(
        "SELECT id FROM work_items WHERE deleted_at IS NULL AND (external_key=? OR display_id=?) "
        "ORDER BY created_at LIMIT 1",
        (entry, entry),
    ).fetchone()
    if work_item is not None:
        return "work_item", work_item["id"]

    return None, None


def _source_event_meta(conn, source_event_id):
    if source_event_id is None:
        return None
    row = conn.execute(
        "SELECT se.repository_id, se.git_branch, se.occurred_at, se.event_type, "
        "s.transcript_path FROM source_events se "
        "LEFT JOIN sessions s ON s.id = se.session_row_id "
        "WHERE se.id=?",
        (source_event_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "repository_id": row["repository_id"],
        "git_branch": row["git_branch"],
        "occurred_at": row["occurred_at"],
        "event_type": row["event_type"],
        "transcript_path": row["transcript_path"],
    }


def _evidence_for_activity(conn, activity_id: str) -> "list[dict]":
    rows = conn.execute(
        "SELECT * FROM activity_evidence WHERE activity_id=? ORDER BY created_at", (activity_id,)
    ).fetchall()
    return [
        {
            "id": row["id"],
            "type": row["evidence_type"],
            "ref": row["ref"],
            "excerpt": row["excerpt"],
            "occurred_at": row["occurred_at"],
            "source_event": _source_event_meta(conn, row["source_event_id"]),
        }
        for row in rows
    ]


def evidence_report(conn, entry: str) -> dict:
    """Evidence for `entry` -- an activity id, a remaining-item id, or a
    `LOCAL-NNNN`/ticket-key work-item reference. Never returns file
    contents: only refs (paths, commit shas), redacted excerpts (<=512
    chars, as stored), and source-event metadata."""
    kind, resolved_id = _resolve_evidence_entry(conn, entry)
    if kind is None:
        raise ValueError(f"no activity, remaining item, or work item found for {entry!r}")

    if kind == "activity":
        evidence = _evidence_for_activity(conn, resolved_id)
    elif kind == "remaining_item":
        transitions = conn.execute(
            "SELECT * FROM remaining_item_transitions WHERE remaining_item_id=? ORDER BY occurred_at",
            (resolved_id,),
        ).fetchall()
        evidence = []
        for t in transitions:
            if t["activity_id"]:
                evidence.extend(_evidence_for_activity(conn, t["activity_id"]))
            elif t["source_event_id"]:
                evidence.append({
                    "id": t["id"],
                    "type": "transition",
                    "ref": None,
                    "excerpt": t["explanation"],
                    "occurred_at": t["occurred_at"],
                    "source_event": _source_event_meta(conn, t["source_event_id"]),
                })
    else:  # work_item
        activity_rows = conn.execute(
            "SELECT id FROM activities WHERE work_item_id=? AND deleted_at IS NULL ORDER BY started_at",
            (resolved_id,),
        ).fetchall()
        evidence = []
        for a in activity_rows:
            evidence.extend(_evidence_for_activity(conn, a["id"]))

    return {"kind": "evidence", "entry": entry, "resolved_kind": kind, "resolved_id": resolved_id,
            "evidence": evidence}


def inbox_report(conn, *, limit: int = 50) -> dict:
    """`in_inbox=1` activities and remaining items with their
    `confidence_reason` (Global Constraints: `low` confidence is excluded
    from normal reports; this is where it lives instead)."""
    activity_rows = conn.execute(
        "SELECT * FROM activities WHERE in_inbox=1 AND deleted_at IS NULL "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    activities = [
        {
            "id": row["id"],
            "kind": row["kind"],
            "summary": row["summary"],
            "started_at": row["started_at"],
            "confidence": row["confidence"],
            "confidence_reason": row["confidence_reason"],
            "work_item_id": row["work_item_id"],
        }
        for row in activity_rows
    ]

    remaining_rows = conn.execute(
        "SELECT * FROM remaining_items WHERE in_inbox=1 AND deleted_at IS NULL "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    remaining_items = [item_summary(conn, row) for row in remaining_rows]

    return {"kind": "inbox", "activities": activities, "remaining_items": remaining_items}
