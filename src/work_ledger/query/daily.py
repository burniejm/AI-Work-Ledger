"""Daily and range history reports, grouped by client -> project -> work item
(Spec 5.3, Task 17).

Read-only: one parameterized `SELECT` per section (activities, remaining
items, blockers, decisions, contradictions), each filtered by its own
event-time column falling inside the `[start_utc, end_utc)` half-open
window computed by `timeutil.workday_bounds`/the caller. Grouping,
inbox/confidence filtering, and jira-staleness note-building all happen in
Python -- SQL never branches on user, model, or Jira text (Global
Constraints).

`in_inbox=1` rows (low confidence) are excluded from the grouped tree by
default and rolled into `excluded_low_confidence_count`; `include_inbox`
folds them back in. `include_evidence` is the only thing that attaches
`activity_evidence` rows to an activity entry (Spec 20.22: daily reports
default to summaries).
"""
import sqlite3

from work_ledger import timeutil

__all__ = ["day_report", "range_report"]

_UNASSIGNED_KEY = "__unassigned__"


def _key(id_):
    return id_ if id_ is not None else _UNASSIGNED_KEY


def _ensure_client(clients: dict, client_id, client_name) -> dict:
    key = _key(client_id)
    node = clients.get(key)
    if node is None:
        node = {
            "client": client_name if client_id is not None else "Unassigned",
            "client_id": client_id,
            "projects": {},
        }
        clients[key] = node
    return node


def _ensure_project(client_node: dict, project_id, project_name) -> dict:
    key = _key(project_id)
    projects = client_node["projects"]
    node = projects.get(key)
    if node is None:
        node = {
            "project": project_name if project_id is not None else "Unassigned",
            "project_id": project_id,
            "work_items": {},
        }
        projects[key] = node
    return node


def _ensure_work_item(project_node: dict, work_item_id, *, external_key, display_id, title,
                       jira_status, jira_status_category, jira_assignee, jira_fetched_at,
                       jira_stale, connection_id, stale_connections: dict,
                       ai_usage=None) -> dict:
    key = _key(work_item_id)
    work_items = project_node["work_items"]
    node = work_items.get(key)
    if node is None:
        label = external_key or display_id
        jira = None
        if jira_status is not None or jira_status_category is not None:
            jira = {
                "status": jira_status,
                "status_category": jira_status_category,
                "assignee": jira_assignee,
                "fetched_at": jira_fetched_at,
                "stale": bool(jira_stale),
            }
        node = {
            "work_item": label if work_item_id is not None else "Unassigned",
            "work_item_id": work_item_id,
            "title": title,
            "jira": jira,
            "ai_usage": ai_usage,
            "activities": [],
            "remaining_discovered": [],
            "blockers": [],
            "decisions": [],
            "contradictions": [],
        }
        work_items[key] = node
        if jira_stale and jira_fetched_at and connection_id:
            stale_connections.setdefault(connection_id, jira_fetched_at)
    return node


def _activity_entry(row: sqlite3.Row, *, include_evidence: bool,
                     evidence_by_activity: dict, conflict_lookup: dict) -> dict:
    confidence = row["confidence"]
    entry = {
        "id": row["id"],
        "kind": row["kind"],
        "summary": row["summary"],
        "outcome": row["outcome"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "confidence": confidence,
        "inferred": confidence == "medium",
        "stated_duration_minutes": row["stated_duration_minutes"],
    }
    if confidence == "contradictory":
        entry["conflict"] = {"contradictions": conflict_lookup.get(row["id"], [])}
    if include_evidence:
        entry["evidence"] = [
            {"type": ev["evidence_type"], "ref": ev["ref"], "excerpt": ev["excerpt"]}
            for ev in evidence_by_activity.get(row["id"], [])
        ]
    return entry


_WORK_ITEM_JOIN_COLUMNS = (
    "wi.external_key AS wi_external_key, wi.display_id AS wi_display_id, wi.title AS wi_title, "
    "wi.jira_status, wi.jira_status_category, wi.jira_assignee, wi.jira_fetched_at, wi.jira_stale, "
    "wi.ai_usage_score AS wi_ai_usage_score, wi.ai_usage_confidence AS wi_ai_usage_confidence, "
    "wi.ai_usage_reason AS wi_ai_usage_reason, "
    "wi.connection_id AS wi_connection_id"
)


def _attach(clients: dict, stale_connections: dict, row: sqlite3.Row, *,
            client_id, client_name, project_id, project_name, work_item_id) -> dict:
    """Resolve (and create, if needed) the work-item node `row` belongs under."""
    client_node = _ensure_client(clients, client_id, client_name)
    project_node = _ensure_project(client_node, project_id, project_name)
    return _ensure_work_item(
        project_node, work_item_id,
        external_key=row["wi_external_key"], display_id=row["wi_display_id"], title=row["wi_title"],
        jira_status=row["jira_status"], jira_status_category=row["jira_status_category"],
        jira_assignee=row["jira_assignee"], jira_fetched_at=row["jira_fetched_at"],
        jira_stale=row["jira_stale"], connection_id=row["wi_connection_id"],
        stale_connections=stale_connections,
        ai_usage=(None if row["wi_ai_usage_score"] is None else
                  {"score": row["wi_ai_usage_score"],
                   "confidence": row["wi_ai_usage_confidence"],
                   "reason": row["wi_ai_usage_reason"]}),
    )


def _finalize_clients(clients: dict) -> "list[dict]":
    def sort_key(item):
        node_key, node = item
        label = node.get("client") or node.get("project") or node.get("work_item") or ""
        return (1 if node_key == _UNASSIGNED_KEY else 0, str(label).lower())

    result = []
    for _, client_node in sorted(clients.items(), key=sort_key):
        projects_out = []
        for _, project_node in sorted(client_node["projects"].items(), key=sort_key):
            work_items_out = [
                dict(wi_node)
                for _, wi_node in sorted(project_node["work_items"].items(), key=sort_key)
            ]
            projects_out.append({
                "project": project_node["project"],
                "project_id": project_node["project_id"],
                "work_items": work_items_out,
            })
        result.append({
            "client": client_node["client"],
            "client_id": client_node["client_id"],
            "projects": projects_out,
        })
    return result


def _build_report(conn, start_utc: str, end_utc: str, *,
                   include_evidence: bool, include_inbox: bool) -> dict:
    clients: dict = {}
    stale_connections: dict = {}
    excluded = 0

    activity_rows = conn.execute(
        "SELECT a.*, c.name AS client_name, p.name AS project_name, " + _WORK_ITEM_JOIN_COLUMNS + " "
        "FROM activities a "
        "LEFT JOIN clients c ON c.id = a.client_id "
        "LEFT JOIN projects p ON p.id = a.project_id "
        "LEFT JOIN work_items wi ON wi.id = a.work_item_id "
        "WHERE a.deleted_at IS NULL AND a.started_at >= ? AND a.started_at < ? "
        "ORDER BY a.started_at",
        (start_utc, end_utc),
    ).fetchall()

    included_activities = []
    contradictory_ids = []
    for row in activity_rows:
        if row["in_inbox"] and not include_inbox:
            excluded += 1
            continue
        included_activities.append(row)
        if row["confidence"] == "contradictory":
            contradictory_ids.append(row["id"])

    evidence_by_activity: dict = {}
    if include_evidence and included_activities:
        ids_ = [row["id"] for row in included_activities]
        placeholders = ",".join("?" for _ in ids_)
        rows = conn.execute(
            f"SELECT * FROM activity_evidence WHERE activity_id IN ({placeholders}) ORDER BY created_at",
            ids_,
        ).fetchall()
        for ev in rows:
            evidence_by_activity.setdefault(ev["activity_id"], []).append(ev)

    conflict_lookup: dict = {}
    if contradictory_ids:
        placeholders = ",".join("?" for _ in contradictory_ids)
        rows = conn.execute(
            "SELECT * FROM contradictions WHERE deleted_at IS NULL AND subject_table='activities' "
            f"AND subject_id IN ({placeholders})",
            contradictory_ids,
        ).fetchall()
        for row in rows:
            conflict_lookup.setdefault(row["subject_id"], []).append({
                "rule": row["rule"], "ledger_claim": row["ledger_claim"], "external_claim": row["external_claim"],
            })

    for row in included_activities:
        wi_node = _attach(
            clients, stale_connections, row,
            client_id=row["client_id"], client_name=row["client_name"],
            project_id=row["project_id"], project_name=row["project_name"],
            work_item_id=row["work_item_id"],
        )
        wi_node["activities"].append(_activity_entry(
            row, include_evidence=include_evidence,
            evidence_by_activity=evidence_by_activity, conflict_lookup=conflict_lookup,
        ))

    for kind, field in (("remaining", "remaining_discovered"), ("blocker", "blockers")):
        rows = conn.execute(
            "SELECT ri.*, c.name AS client_name, p.name AS project_name, " + _WORK_ITEM_JOIN_COLUMNS + " "
            "FROM remaining_items ri "
            "LEFT JOIN clients c ON c.id = ri.client_id "
            "LEFT JOIN projects p ON p.id = ri.project_id "
            "LEFT JOIN work_items wi ON wi.id = ri.work_item_id "
            "WHERE ri.deleted_at IS NULL AND ri.kind=? AND ri.first_seen_at >= ? AND ri.first_seen_at < ? "
            "ORDER BY ri.first_seen_at",
            (kind, start_utc, end_utc),
        ).fetchall()
        for row in rows:
            if row["in_inbox"] and not include_inbox:
                excluded += 1
                continue
            wi_node = _attach(
                clients, stale_connections, row,
                client_id=row["client_id"], client_name=row["client_name"],
                project_id=row["project_id"], project_name=row["project_name"],
                work_item_id=row["work_item_id"],
            )
            wi_node[field].append({"id": row["id"], "title": row["title"], "state": row["state"]})

    decision_rows = conn.execute(
        "SELECT d.*, c.name AS client_name, p.name AS project_name, " + _WORK_ITEM_JOIN_COLUMNS + " "
        "FROM decisions d "
        "LEFT JOIN clients c ON c.id = d.client_id "
        "LEFT JOIN projects p ON p.id = d.project_id "
        "LEFT JOIN work_items wi ON wi.id = d.work_item_id "
        "WHERE d.deleted_at IS NULL AND d.decided_at >= ? AND d.decided_at < ? "
        "ORDER BY d.decided_at",
        (start_utc, end_utc),
    ).fetchall()
    for row in decision_rows:
        wi_node = _attach(
            clients, stale_connections, row,
            client_id=row["client_id"], client_name=row["client_name"],
            project_id=row["project_id"], project_name=row["project_name"],
            work_item_id=row["work_item_id"],
        )
        wi_node["decisions"].append({"id": row["id"], "statement": row["statement"]})

    # Only status='open' contradictions are "current" (Spec 5.4); Task 18's
    # scoped/status reports use the same filter (query.scoped.open_contradictions),
    # so this keeps day/range reports consistent with them.
    contradiction_rows = conn.execute(
        "SELECT co.*, wi.client_id AS wi_client_id, wi.project_id AS wi_project_id, "
        "c.name AS client_name, p.name AS project_name, " + _WORK_ITEM_JOIN_COLUMNS + " "
        "FROM contradictions co "
        "LEFT JOIN work_items wi ON wi.id = co.work_item_id "
        "LEFT JOIN clients c ON c.id = wi.client_id "
        "LEFT JOIN projects p ON p.id = wi.project_id "
        "WHERE co.deleted_at IS NULL AND co.status='open' "
        "AND co.detected_at >= ? AND co.detected_at < ? "
        "ORDER BY co.detected_at",
        (start_utc, end_utc),
    ).fetchall()
    for row in contradiction_rows:
        wi_node = _attach(
            clients, stale_connections, row,
            client_id=row["wi_client_id"], client_name=row["client_name"],
            project_id=row["wi_project_id"], project_name=row["project_name"],
            work_item_id=row["work_item_id"],
        )
        wi_node["contradictions"].append({
            "id": row["id"], "rule": row["rule"],
            "ledger_claim": row["ledger_claim"], "external_claim": row["external_claim"],
        })

    notes = []
    if stale_connections:
        conn_ids = list(stale_connections.keys())
        placeholders = ",".join("?" for _ in conn_ids)
        name_rows = conn.execute(
            f"SELECT id, name FROM connections WHERE id IN ({placeholders})", conn_ids
        ).fetchall()
        names = {r["id"]: r["name"] for r in name_rows}
        for connection_id, fetched_at in stale_connections.items():
            notes.append(f"Jira data for {names.get(connection_id, connection_id)} is stale as of {fetched_at}")

    clients_out = _finalize_clients(clients)
    _attach_minutes(conn, clients_out, start_utc, end_utc)

    return {
        "clients": clients_out,
        "excluded_low_confidence_count": excluded,
        "notes": notes,
        "generated_at": timeutil.utc_now_iso(),
    }


def _attach_minutes(conn, clients_out: "list[dict]", start_utc: str, end_utc: str) -> None:
    """Per-item and per-client active minutes for the report window.

    A client's total pools every item's raw intervals before merging, so a
    stretch of one session that fed two of the client's tickets is counted
    once at the client level (each item still shows its own full share).
    """
    from work_ledger.query import timespent

    for client_node in clients_out:
        client_intervals = []
        for project_node in client_node.get("projects") or []:
            for wi_node in project_node.get("work_items") or []:
                work_item_id = wi_node.get("work_item_id")
                if work_item_id is None:
                    wi_node["minutes"] = 0
                    continue
                intervals = timespent.intervals_for_work_item(
                    conn, work_item_id, start_utc=start_utc, end_utc=end_utc)
                wi_node["minutes"] = timespent.total_minutes(
                    timespent.merge_intervals(intervals))
                client_intervals.extend(intervals)
        client_node["minutes"] = timespent.total_minutes(
            timespent.merge_intervals(client_intervals))


def day_report(conn, workday: str, *, tz: str, rollover_hour: int,
               include_evidence: bool = False, include_inbox: bool = False) -> dict:
    """Group activities/remaining/blockers/decisions/contradictions for one workday."""
    start_utc, end_utc = timeutil.workday_bounds(workday, tz, rollover_hour)
    base = _build_report(conn, start_utc, end_utc, include_evidence=include_evidence,
                          include_inbox=include_inbox)
    return {
        "kind": "day",
        "workday": workday,
        "range": {"start_utc": start_utc, "end_utc": end_utc},
        **base,
    }


def range_report(conn, start_workday: str, end_workday: str, *, tz: str, rollover_hour: int,
                  include_evidence: bool = False, include_inbox: bool = False) -> dict:
    """Same grouping as `day_report`, merged across every workday in [start_workday, end_workday]."""
    start_utc, _ = timeutil.workday_bounds(start_workday, tz, rollover_hour)
    _, end_utc = timeutil.workday_bounds(end_workday, tz, rollover_hour)
    base = _build_report(conn, start_utc, end_utc, include_evidence=include_evidence,
                          include_inbox=include_inbox)
    return {
        "kind": "range",
        "start_workday": start_workday,
        "end_workday": end_workday,
        "range": {"start_utc": start_utc, "end_utc": end_utc},
        **base,
    }
