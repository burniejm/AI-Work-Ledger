"""Jira refresh policy: staleness, bounded REST refresh, identity attribution
(Spec §11.1, §13, §19, §20.9/§20.19; Ruling 8; Ruling 30(d); DESIGN gap G2).

This module is where a **direct** Jira connection (`adapter='jira-rest'`)
actually gets refreshed. It is the one Jira-adjacent module a hook is
allowed to call (Ruling 8, allowlist item (d), "bounded REST Jira refresh"):
`hooks_runtime._handle_session_start` calls `refresh_all_rest` inside its
own guarded/bounded block, so a Jira outage never blocks local capture
(Global Constraints).

DESIGN gap G2 -- a hook or a plain Python process has no channel to the MCP
servers Claude Code has connected -- means `refresh_all_rest` can only ever
touch REST connections. `adapter='mcp'` connections are never contacted here:
they are skipped (named in `skipped_mcp`) and their Jira-backed work items
are marked stale, so the model-mediated loop (`worklog jira plan-refresh` /
`worklog jira ingest`, `jira.mcp`) picks them back up on its own schedule.

Every string this module persists into `work_items` passes through
`redact.redact` first (Ruling 15) -- a REST response is still remote,
network-carried text, even though it is not model-relayed the way an MCP
ingest payload is.

Identity attribution (Spec §11.1): `attribute_identity` is the single gate
between "an issue Jira reports" and "work the user may claim as their own".
It returns `True` only when the issue's assignee matches a `user_identities`
row scoped to the same connection -- never on a bare account-id match
against a different connection, and never in the absence of any configured
identity.
"""
import time

from work_ledger import connections, contradictions, redact, timeutil, work_items
from work_ledger.db import connection as dbc
from work_ledger.jira import factory
from work_ledger.jira.adapter import IssueResult
# One owner of the staleness-window helpers (Important 3, code review): these
# are jira.mcp's, reused here rather than re-defined a third/fourth time. No
# cycle -- jira.mcp does not import jira.refresh.
from work_ledger.jira.mcp import _stale_cutoff
from work_ledger.jira.rest import (
    InvalidIssueKey,
    IssueNotFound,
    JiraAuthError,
    JiraUnavailable,
    NotFound,
)

__all__ = [
    "stale_work_items",
    "refresh_rest",
    "refresh_all_rest",
    "mark_stale",
    "attribute_identity",
]

# Every string field this module writes into work_items is capped the same
# way jira.mcp.ingest caps model-relayed fields -- a REST response is
# remote, unbounded-length text, not a value we control the shape of.
_MAX_FIELD_CHARS = 512


def stale_work_items(conn, *, connection_id=None, limit: int) -> "list":
    """Jira-backed work items that need a refresh: `jira_stale=1`, never
    fetched, or fetched longer ago than `jira.stale_after_hours`. Ordered by
    most-recently-active first (`updated_at DESC`) so a bounded refresh
    spends its budget on the work the user is most likely mid-way through,
    rather than on whatever happens to sort first alphabetically."""
    now_iso = timeutil.utc_now_iso()
    clauses = [
        "kind='jira'", "deleted_at IS NULL", "merged_into_id IS NULL",
        "external_key IS NOT NULL",
        "(jira_stale=1 OR jira_fetched_at IS NULL OR jira_fetched_at < ?)",
    ]
    params: list = [_stale_cutoff(now_iso)]
    if connection_id is not None:
        clauses.append("connection_id=?")
        params.append(connection_id)
    params.append(max(0, int(limit)))
    return conn.execute(
        f"SELECT * FROM work_items WHERE {' AND '.join(clauses)} "
        "ORDER BY updated_at DESC LIMIT ?",
        params,
    ).fetchall()


def mark_stale(conn, *, connection_id=None, hook_mode: bool = False) -> int:
    """Flag Jira-backed work items stale (optionally scoped to one
    connection). Returns the number of rows actually flipped from
    `jira_stale=0` -- an already-stale row is not recounted, so a repeated
    call against an already-marked connection reports 0 rather than
    over-stating what just happened.

    `hook_mode=True` threads through to `db.connection.transaction` for the
    hook's bounded busy-retry schedule (the hook-time caller, `refresh_all_rest`,
    passes `hook_mode=True`)."""
    now = timeutil.utc_now_iso()
    clauses = ["kind='jira'", "deleted_at IS NULL", "merged_into_id IS NULL", "jira_stale=0"]
    params: list = []
    if connection_id is not None:
        clauses.append("connection_id=?")
        params.append(connection_id)
    with dbc.transaction(conn, hook_mode=hook_mode):
        cur = conn.execute(
            f"UPDATE work_items SET jira_stale=1, updated_at=? WHERE {' AND '.join(clauses)}",
            [now, *params],
        )
        return cur.rowcount


def _capped(value):
    if isinstance(value, str) and len(value) > _MAX_FIELD_CHARS:
        return value[:_MAX_FIELD_CHARS]
    return value


def _redacted(value):
    """Ruling 15: every string this module persists is redacted first. Not
    `redact.redact_obj` -- that blanks a value by its *key* name (which
    would swallow a Jira `key`/`project.key`), where here we only ever
    redact scalar values already pulled out by name."""
    if isinstance(value, str):
        return redact.redact(value)[0]
    return value


def _normalized_updated(value):
    """Jira Cloud stamps `updated` as `...+0000`; the ledger stores UTC
    ISO-8601 with a `Z` and millisecond precision. Unparseable input is
    dropped rather than stored in a foreign shape (mirrors
    `jira.mcp._normalized_updated`)."""
    import re
    from datetime import timezone
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    match = re.search(r"([+-]\d{2})(\d{2})$", text)
    if match:
        text = text[: match.start()] + f"{match.group(1)}:{match.group(2)}"
    try:
        moment = timeutil.parse_iso(text)
    except (ValueError, TypeError):
        return None
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _apply_issue(conn, connection_id: str, work_item_id: str, key: str,
                  issue: IssueResult, now: str, *, hook_mode: bool = False) -> None:
    """Write one refreshed issue's fields into its `work_items` row.
    Clears `jira_stale` -- this row is now current as of `now`.

    `hook_mode` threads through to `work_items.upsert_external` and to this
    function's own transaction (Important 2, code review)."""
    summary = issue.summary if isinstance(issue.summary, str) and issue.summary.strip() else None
    if summary is not None:
        work_items.upsert_external(conn, connection_id, key, title=_capped(_redacted(summary)),
                                    hook_mode=hook_mode)
    with dbc.transaction(conn, hook_mode=hook_mode):
        conn.execute(
            # COALESCE on jira_updated_at: a response that omits/malforms
            # `updated` must not erase a good timestamp an earlier fetch
            # established (mirrors jira.mcp.ingest).
            "UPDATE work_items SET jira_status=?, jira_status_category=?, jira_resolution=?, "
            "jira_assignee=?, jira_updated_at=COALESCE(?, jira_updated_at), jira_fetched_at=?, "
            "jira_stale=0, updated_at=? WHERE id=?",
            (_capped(_redacted(issue.status)), _capped(_redacted(issue.status_category)),
             _capped(_redacted(issue.resolution)), _capped(_redacted(issue.assignee_account_id)),
             _normalized_updated(issue.updated), now, now, work_item_id),
        )


def refresh_rest(conn, connection_row, *, limit: int, now: str,
                  deadline: "float | None" = None, hook_mode: bool = False) -> dict:
    """Refresh one `adapter='jira-rest'` connection's stale work items.

    Returns `{"updated": n, "failed": n, "health": state}` (Spec §19,
    §20.9/§20.19), plus `"partial": True` when `deadline` cut the run short
    (omitted otherwise). Every fetch is a plain `GET` through
    `jira.factory.build_adapter` -> `JiraRestAdapter` (Jira is read-only).

    Bounded and guarded (Ruling 8): at most `limit` issues are fetched, and
    the loop stops at the FIRST connection-level failure (an auth error or
    an outage) rather than re-discovering the same failure once per stale
    issue -- a degraded/expired connection's already-fetched history is left
    exactly as it was; nothing is deleted or blanked on a failed refresh. A
    single issue coming back 404/malformed-key is not a connection-level
    failure (Jira itself, not the connection, is unhappy about that one
    issue) and does not stop the loop.

    `deadline` is a `time.monotonic()` value (mirrors
    `capture.sessions.reconcile_transcript`): checked before fetching each
    issue (and before the initial no-work-stale probe), so a caller with a
    tight wall-clock budget -- a hook -- stops cleanly between items rather
    than only being bounded by `limit`. This cannot preempt a single
    in-flight request once started (Python cannot interrupt a blocking
    socket call from outside); the remaining exposure past `deadline` is
    therefore at most one issue's `jira.request_timeout_seconds`, not
    `limit` of them. `hook_mode` threads through to every write this
    function makes (`_apply_issue`, `connections.set_health`,
    `contradictions.evaluate` -- Minor #5, final review) for the hook's
    bounded busy-retry schedule.
    """
    connection_id = connection_row["id"]
    adapter = factory.build_adapter(conn, connection_row)

    if deadline is not None and time.monotonic() >= deadline:
        return {"updated": 0, "failed": 0, "health": connection_row["health_state"], "partial": True}

    rows = stale_work_items(conn, connection_id=connection_id, limit=limit)

    if not rows:
        # Nothing stale to fetch. Reporting "healthy" here without ever
        # contacting Jira would let a genuinely outaged/expired connection
        # sit at its last-known-good health state indefinitely just because
        # nothing happened to need a refresh -- one cheap probe call keeps
        # `connections.health_state` (and therefore `worklog doctor`/
        # `worklog connections`) honest even when there is no issue to fetch.
        health = adapter.health()
        connections.set_health(conn, connection_id, health.state, health.detail, hook_mode=hook_mode)
        return {"updated": 0, "failed": 0, "health": health.state}

    updated = 0
    failed = 0
    state = "healthy"
    detail = None
    touched: "list[str]" = []
    partial = False

    for row in rows:
        if deadline is not None and time.monotonic() >= deadline:
            partial = True
            break
        key = row["external_key"]
        try:
            issue = adapter.get_issue(key)
        except JiraAuthError as exc:
            failed += 1
            state, detail = "expired", str(exc)
            break
        except JiraUnavailable as exc:
            failed += 1
            state, detail = "degraded", str(exc)
            break
        except (IssueNotFound, NotFound, InvalidIssueKey) as exc:
            failed += 1
            continue
        else:
            _apply_issue(conn, connection_id, row["id"], key, issue, now, hook_mode=hook_mode)
            updated += 1
            if row["id"] not in touched:
                touched.append(row["id"])

    connections.set_health(conn, connection_id, state, detail, hook_mode=hook_mode)

    if touched:
        contradictions.evaluate(conn, work_item_ids=touched, hook_mode=hook_mode)

    result = {"updated": updated, "failed": failed, "health": state}
    if partial:
        result["partial"] = True
    return result


def refresh_all_rest(conn, *, limit: int, deadline: "float | None" = None,
                      hook_mode: bool = False) -> dict:
    """The hook-time entry point (Ruling 8, allowlist item (d)): refresh
    every enabled `adapter='jira-rest'` connection, bounded to `limit`
    issues each.

    `adapter='mcp'` connections are never contacted (DESIGN gap G2 -- a hook
    has no channel to an MCP server): each is recorded in `skipped_mcp` by
    name, and its Jira-backed work items are marked stale so the
    model-mediated loop (`worklog jira plan-refresh`/`ingest`) knows to pick
    them back up.

    `deadline` (a `time.monotonic()` value) is checked before starting each
    connection, and is threaded into `refresh_rest`, which additionally
    checks it before each issue fetch -- so a caller with a tight wall-clock
    budget (a hook) stops cleanly rather than working through every
    connection's full `limit` regardless of elapsed time. `hook_mode`
    threads through to every write `mark_stale`/`refresh_rest` make.

    Return contract (Ruling 30(d), stated in full): `{"updated": n,
    "failed": n, "connections": {name: health_state, ...}, "skipped_mcp":
    [connection_name, ...]}`, plus `"partial": True` (omitted otherwise)
    when `deadline` cut the run short -- either between connections or
    inside one connection's own refresh. `skipped_mcp` is always present,
    `[]` when there are none.
    """
    now = timeutil.utc_now_iso()
    updated_total = 0
    failed_total = 0
    conn_health: "dict[str, str]" = {}
    skipped_mcp: "list[str]" = []
    partial = False

    for row in connections.list_connections(conn):
        if deadline is not None and time.monotonic() >= deadline:
            partial = True
            break
        if row["adapter"] == "mcp":
            skipped_mcp.append(row["name"])
            mark_stale(conn, connection_id=row["id"], hook_mode=hook_mode)
            continue
        if row["adapter"] != "jira-rest":
            continue
        result = refresh_rest(conn, row, limit=limit, now=now, deadline=deadline, hook_mode=hook_mode)
        updated_total += result["updated"]
        failed_total += result["failed"]
        conn_health[row["name"]] = result["health"]
        if result.get("partial"):
            partial = True

    out = {
        "updated": updated_total,
        "failed": failed_total,
        "connections": conn_health,
        "skipped_mcp": skipped_mcp,
    }
    if partial:
        out["partial"] = True
    return out


def attribute_identity(conn, connection_id: str, issue: IssueResult) -> bool:
    """`True` only when `issue`'s assignee matches a `user_identities` row
    configured for this exact connection (Spec §11.1: only activity
    attributable to a configured user identity may be described as the
    user's work). No assignee, no connection-scoped identity, or an
    identity configured for a *different* connection all read as `False` --
    this is deliberately conservative, never inferring identity from a bare
    account-id coincidence.
    """
    if not issue.assignee_account_id:
        return False
    row = conn.execute(
        "SELECT id FROM user_identities WHERE kind='jira' AND connection_id=? AND value=? "
        "AND deleted_at IS NULL",
        (connection_id, issue.assignee_account_id),
    ).fetchone()
    return row is not None
