"""Scope resolution and the `status` report (Spec 5.4, 20.23, Task 18).

`resolve_scope` is the single interpreter of a free-text scope argument
(ticket key, work item display id, client name/alias, or `client/project`)
shared by every scoped query/CLI subcommand. `status_report` is the
one-shot "where do things stand" view: open remaining work, blockers,
waiting items, Jira status/staleness, recent activity over a rolling
workday window, stale work, and open contradictions.

Every "still open" predicate here is `remaining.OPEN_STATES` (Ruling 28) --
never a locally restated state list.
"""
import difflib
from datetime import date, timedelta

from work_ledger import config, entities, remaining, timeutil
from work_ledger.query import daily as query_daily

__all__ = ["ScopeNotFound", "resolve_scope", "status_report"]


class ScopeNotFound(Exception):
    """Raised by `resolve_scope` when `scope` matches no client, project,
    or work item -- the message lists near matches, if any were found."""


def _find_work_item(conn, scope: str):
    return conn.execute(
        "SELECT id, title, external_key, display_id FROM work_items "
        "WHERE deleted_at IS NULL AND (external_key=? OR display_id=?) "
        "ORDER BY created_at LIMIT 1",
        (scope, scope),
    ).fetchone()


def _find_project_under_client(conn, client_id: str, name: str):
    slug = entities.slugify(name)
    return conn.execute(
        "SELECT * FROM projects WHERE client_id=? AND slug=? AND deleted_at IS NULL",
        (client_id, slug),
    ).fetchone()


def _near_matches(conn, scope: str) -> "list[str]":
    candidates = []
    for row in conn.execute("SELECT name FROM clients WHERE deleted_at IS NULL").fetchall():
        candidates.append(row["name"])
    for row in conn.execute(
        "SELECT external_key, display_id FROM work_items WHERE deleted_at IS NULL"
    ).fetchall():
        if row["external_key"]:
            candidates.append(row["external_key"])
        if row["display_id"]:
            candidates.append(row["display_id"])
    return difflib.get_close_matches(scope, candidates, n=5, cutoff=0.6)


def resolve_scope(conn, scope: "str | None") -> dict:
    """Interpret a free-text scope argument.

    Resolution order (first match wins): exact ticket key/display id ->
    work item; alias/slug -> client; `client/project` -> project.
    `scope=None` resolves to the whole-ledger scope (`kind='all'`).
    Raises `ScopeNotFound` (message lists near matches, if any) otherwise.
    """
    if scope is None or scope.strip() == "":
        return {"kind": "all", "id": None, "label": "all"}

    text = scope.strip()

    work_item = _find_work_item(conn, text)
    if work_item is not None:
        label = work_item["external_key"] or work_item["display_id"]
        return {"kind": "work_item", "id": work_item["id"], "label": label}

    if "/" not in text:
        client_row = entities.get_client_by_name(conn, text)
        if client_row is not None:
            return {"kind": "client", "id": client_row["id"], "label": client_row["name"]}
    else:
        client_part, _, project_part = text.partition("/")
        client_row = entities.get_client_by_name(conn, client_part.strip())
        if client_row is not None:
            project_row = _find_project_under_client(conn, client_row["id"], project_part.strip())
            if project_row is not None:
                return {
                    "kind": "project",
                    "id": project_row["id"],
                    "label": f"{client_row['name']}/{project_row['name']}",
                }

    near = _near_matches(conn, text)
    if near:
        raise ScopeNotFound(f"no scope found matching {text!r}; did you mean: {', '.join(near)}")
    raise ScopeNotFound(f"no scope found matching {text!r}")


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


def _open_work_items(conn, filters: dict) -> "list[dict]":
    from work_ledger.query import scoped as query_scoped

    rows = remaining.open_items(conn, **filters)
    items = [query_scoped.item_summary(conn, row) for row in rows]
    for item in items:
        if item["state"] in ("blocked", "waiting"):
            item["explanation"] = _latest_explanation(conn, item["id"], item["state"])
    return items


def _jira_where(filters: dict) -> "tuple[str, list]":
    clauses = ["deleted_at IS NULL", "kind='jira'"]
    params: list = []
    if filters["work_item_id"] is not None:
        clauses.append("id=?")
        params.append(filters["work_item_id"])
    elif filters["project_id"] is not None:
        clauses.append("project_id=?")
        params.append(filters["project_id"])
    elif filters["client_id"] is not None:
        clauses.append("client_id=?")
        params.append(filters["client_id"])
    return " AND ".join(clauses), params


def _jira_status(conn, filters: dict, notes: list) -> "list[dict]":
    where, params = _jira_where(filters)
    rows = conn.execute(
        f"SELECT * FROM work_items WHERE {where} ORDER BY updated_at DESC", params
    ).fetchall()

    stale_after_hours = config.get("jira.stale_after_hours", 12)
    now = timeutil.parse_iso(timeutil.utc_now_iso())
    conn_names: dict = {}

    entries = []
    for row in rows:
        label = row["external_key"] or row["display_id"]
        stale_flag = bool(row["jira_stale"])
        entries.append({
            "work_item": label,
            "title": row["title"],
            "status": row["jira_status"],
            "status_category": row["jira_status_category"],
            "assignee": row["jira_assignee"],
            "fetched_at": row["jira_fetched_at"],
            "stale": stale_flag,
        })

        fetched_old = False
        if row["jira_fetched_at"]:
            age_hours = (now - timeutil.parse_iso(row["jira_fetched_at"])).total_seconds() / 3600.0
            fetched_old = age_hours > stale_after_hours
        if stale_flag or fetched_old:
            connection_id = row["connection_id"]
            conn_label = connection_id
            if connection_id is not None:
                if connection_id not in conn_names:
                    crow = conn.execute(
                        "SELECT name FROM connections WHERE id=?", (connection_id,)
                    ).fetchone()
                    conn_names[connection_id] = crow["name"] if crow is not None else connection_id
                conn_label = conn_names[connection_id]
            fetched_label = row["jira_fetched_at"] or "never fetched"
            notes.append(
                f"Jira data for {conn_label or label} ({label}) is stale as of {fetched_label}"
            )
    return entries


def _last_activity_at(conn, filters: dict):
    candidates = []
    for table, column in (
        ("activities", "started_at"),
        ("remaining_items", "last_seen_at"),
        ("decisions", "decided_at"),
    ):
        clauses = ["deleted_at IS NULL"]
        params: list = []
        if filters["work_item_id"] is not None:
            clauses.append("work_item_id=?")
            params.append(filters["work_item_id"])
        elif filters["project_id"] is not None:
            clauses.append("project_id=?")
            params.append(filters["project_id"])
        elif filters["client_id"] is not None:
            clauses.append("client_id=?")
            params.append(filters["client_id"])
        row = conn.execute(
            f"SELECT MAX({column}) AS latest FROM {table} WHERE {' AND '.join(clauses)}", params
        ).fetchone()
        if row is not None and row["latest"] is not None:
            candidates.append(row["latest"])
    return max(candidates) if candidates else None


def _filter_by_scope(nodes: "list[dict]", id_key: str, resolved: dict) -> "list[dict]":
    if resolved["kind"] == "all":
        return nodes
    return [n for n in nodes if n.get(id_key) == resolved["id"]]


def _recent_window(conn, resolved: dict, *, tz: str, rollover_hour: int, workdays: int) -> dict:
    today = timeutil.workday_of(timeutil.utc_now_iso(), tz, rollover_hour)
    end_date = date.fromisoformat(today)
    start_date = end_date - timedelta(days=workdays - 1)
    report = query_daily.range_report(
        conn, start_date.isoformat(), end_date.isoformat(), tz=tz, rollover_hour=rollover_hour,
    )
    if resolved["kind"] == "all":
        return report

    # `_build_report` computed `notes` (Jira staleness) and
    # `excluded_low_confidence_count` over the *whole* ledger, before the
    # client tree below gets filtered down to `resolved`'s scope -- neither
    # is recoverable from the (already-grouped) output for just this scope,
    # so surface them as "not computed for this narrower scope" (`None`)
    # rather than silently keeping whole-ledger figures under a scoped
    # report, or reporting a misleading `0`/`[]`. `status_report`'s own
    # top-level `notes` (Jira staleness for work items *in scope*) is the
    # scope-correct source for that signal.
    report["notes"] = None
    report["excluded_low_confidence_count"] = None

    clients = report.get("clients", [])
    if resolved["kind"] == "client":
        report["clients"] = [c for c in clients if c.get("client_id") == resolved["id"]]
    elif resolved["kind"] == "project":
        filtered = []
        for client in clients:
            projects = [p for p in client.get("projects", []) if p.get("project_id") == resolved["id"]]
            if projects:
                filtered.append({**client, "projects": projects})
        report["clients"] = filtered
    elif resolved["kind"] == "work_item":
        filtered = []
        for client in clients:
            new_projects = []
            for project in client.get("projects", []):
                work_items = [
                    w for w in project.get("work_items", []) if w.get("work_item_id") == resolved["id"]
                ]
                if work_items:
                    new_projects.append({**project, "work_items": work_items})
            if new_projects:
                filtered.append({**client, "projects": new_projects})
        report["clients"] = filtered
    return report


def status_report(conn, scope: "str | None", *, tz: str, rollover_hour: int, workdays: int = 7) -> dict:
    """The "where do things stand" report for `scope` (or the whole ledger
    when `scope` is None)."""
    from work_ledger.query import scoped as query_scoped

    resolved = resolve_scope(conn, scope)
    filters = _scope_filters(resolved)

    open_work = _open_work_items(conn, filters)
    blockers = [i for i in open_work if i["state"] == "blocked"]
    waiting = [i for i in open_work if i["state"] == "waiting"]

    notes: list = []
    jira = _jira_status(conn, filters, notes)

    recent = _recent_window(conn, resolved, tz=tz, rollover_hour=rollover_hour, workdays=workdays)

    stale_days = config.get("reports.stale_after_days", 14)
    stale_all = query_scoped.stale_report(conn, days=stale_days, tz=tz, rollover_hour=rollover_hour)
    stale_items = _filter_by_scope(stale_all["items"], "work_item_id", resolved)

    contradictions = query_scoped.open_contradictions(conn, filters)

    return {
        "kind": "status",
        "scope": resolved,
        "open_work": open_work,
        "blockers": blockers,
        "waiting": waiting,
        "jira": jira,
        "last_activity_at": _last_activity_at(conn, filters),
        "recent": recent,
        "window_workdays": workdays,
        "stale_items": stale_items,
        "contradictions": contradictions,
        "notes": notes,
        "generated_at": timeutil.utc_now_iso(),
    }
