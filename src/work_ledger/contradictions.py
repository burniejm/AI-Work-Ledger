"""The contradiction engine (DESIGN §8, Task 30).

Six named rules compare the ledger's own record against Jira's fields (or,
for `possible_duplicate`, one ledger record against another) and surface a
disagreement as a `contradictions` row: `ledger_claim` is what the ledger
believes, `external_claim` is what the other side says.

Five rules (`jira_done_open_remaining`, `jira_open_work_complete`,
`assignee_mismatch`, `removed_requirement`, `manual_vs_evidence`) are
*scanned*: `evaluate` runs each one over the current data, opens
contradictions for every fresh candidate, and resolves currently-open ones
whose candidate no longer appears. `possible_duplicate` is *event-driven*
instead -- `infer.dedupe._open_possible_duplicate` opens it inline, at the
moment a new remaining-item mention lands in the 0.60-0.85 similarity band
(DESIGN §7) -- so `evaluate` never opens or resolves it; nothing about a
title-similarity band holds or stops holding on its own later.

Every "still open" predicate here is `remaining.OPEN_STATES` (Ruling 28,
via `remaining.open_items`) -- reopened counts. `evaluate` never writes to
`work_items` (Spec §3.3, §20.24): it only reads Jira fields already
populated by the (separate) Jira refresh path and writes `contradictions`
rows.

`uq_contradictions_open` (a partial unique index on
`(rule, IFNULL(work_item_id,''), IFNULL(subject_id,''))  WHERE status='open'`)
is the single source of truth for "already open" -- `open_contradiction`
relies on it via `INSERT OR IGNORE` rather than a SELECT-then-INSERT race.
"""
from datetime import timedelta

from work_ledger import config, entities, ids, remaining, timeutil
from work_ledger.db import connection

__all__ = [
    "RULES",
    "open_contradiction",
    "resolve",
    "resolve_for_merge",
    "evaluate",
    "list_open",
    "rule_jira_done_open_remaining",
    "rule_jira_open_work_complete",
    "rule_assignee_mismatch",
    "rule_removed_requirement",
    "rule_manual_vs_evidence",
]

RULES: "tuple[str, ...]" = (
    "jira_done_open_remaining",
    "jira_open_work_complete",
    "assignee_mismatch",
    "removed_requirement",
    "manual_vs_evidence",
    "possible_duplicate",
)

# Stage A's `_outcome` templates only ever write "committed", "tests passing",
# or "tests failing" (infer/stage_a.py); manual/Stage B outcomes are freer
# text, so this is matched as a case-insensitive substring, not an exact set.
_COMPLETION_MARKERS = (
    "committed", "tests passing", "completed", "fixed", "resolved",
    "done", "shipped", "merged", "closed",
)


def _iso_minus(now_iso: str, days: int) -> str:
    dt = timeutil.parse_iso(now_iso) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _work_item_ids_clause(work_item_ids, column: str = "id"):
    """Returns (extra_clause_or_None, extra_params, short_circuit).

    `short_circuit=True` means the caller scoped to an empty list -- no
    row can ever match, so the caller should return `[]` without querying.
    """
    if work_item_ids is None:
        return None, [], False
    if not work_item_ids:
        return None, [], True
    placeholders = ",".join("?" for _ in work_item_ids)
    return f"{column} IN ({placeholders})", list(work_item_ids), False


def _jira_is_stale(row) -> bool:
    """Mirrors `query.status._jira_status`'s staleness predicate exactly:
    the `jira_stale` flag, OR a fetch present but older than
    `jira.stale_after_hours`. A row with `jira_stale=0` and no
    `jira_fetched_at` at all (e.g. a work item Jira has never touched, but
    that nothing has flagged as stale) is deliberately NOT stale here --
    `query.status` only checks fetch *age* when a fetch timestamp exists,
    it never treats an absent one as automatically old."""
    if row["jira_stale"]:
        return True
    fetched_at = row["jira_fetched_at"]
    if not fetched_at:
        return False
    stale_after_hours = config.get("jira.stale_after_hours", 12)
    now = timeutil.parse_iso(timeutil.utc_now_iso())
    age_hours = (now - timeutil.parse_iso(fetched_at)).total_seconds() / 3600.0
    return age_hours > stale_after_hours


def _stale_note(row) -> str:
    """DESIGN §8 lifecycle: a stale Jira side is *qualified*, never
    suppressed -- this is appended to the external claim, not used to skip
    the rule."""
    if not _jira_is_stale(row):
        return ""
    fetched = row["jira_fetched_at"] or "never fetched"
    return f" (Jira data is stale as of {fetched})"


def _status_claim(row) -> str:
    return f"Jira status {row['jira_status']}" + _stale_note(row)


def _done_claim(row) -> str:
    claim = f"Jira status {row['jira_status']}"
    if row["jira_resolution"]:
        claim += f" (resolution {row['jira_resolution']})"
    return claim + _stale_note(row)


def _assignee_claim(row) -> str:
    return f"Assigned to {row['jira_assignee']}" + _stale_note(row)


def open_contradiction(conn, *, rule: str, work_item_id=None, subject_table=None,
                        subject_id=None, ledger_claim: str, external_claim: str,
                        hook_mode: bool = False) -> "str | None":
    """Open a contradiction, or return `None` when one matching
    `(rule, work_item_id, subject_id)` is already open.

    `INSERT OR IGNORE` against `uq_contradictions_open` is the single
    source of truth for "already open" -- no SELECT-then-INSERT race.

    `hook_mode=True` threads through to `db.connection.transaction` for the
    hook's bounded busy-retry schedule (Minor #5: `evaluate`'s SessionStart
    path threads it here uniformly with every other hook write) -- including
    `entities.current_device`'s own transaction, which per the
    non-reentrancy note must run before this function opens its own.
    Default `False` for CLI/model-loop callers (`infer.dedupe`,
    `jira.mcp.ingest`).
    """
    device_id = entities.current_device(conn, hook_mode=hook_mode)
    now = timeutil.utc_now_iso()
    new_id = ids.new_id()
    with connection.transaction(conn, hook_mode=hook_mode):
        cur = conn.execute(
            "INSERT OR IGNORE INTO contradictions(id, rule, work_item_id, subject_table, subject_id, "
            "ledger_claim, external_claim, status, detected_at, origin_device_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_id, rule, work_item_id, subject_table, subject_id, ledger_claim, external_claim,
             "open", now, device_id, now, now),
        )
        if cur.rowcount == 0:
            return None
    return new_id


def resolve(conn, contradiction_id: str, *, note: str, hook_mode: bool = False) -> None:
    """Mark `contradiction_id` resolved with a machine-written `resolution_note`.

    Raises `ValueError` when no `contradictions` row has that id -- a typo'd
    or already-deleted id must never look like a silent success to a caller
    (in particular `worklog contradictions --resolve`).

    `hook_mode=True` threads through to `db.connection.transaction` (Minor
    #5, mirrors `open_contradiction`); default `False` for CLI callers."""
    now = timeutil.utc_now_iso()
    with connection.transaction(conn, hook_mode=hook_mode):
        cur = conn.execute(
            "UPDATE contradictions SET status='resolved', resolved_at=?, resolution_note=?, updated_at=? "
            "WHERE id=?",
            (now, note, now, contradiction_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"no contradiction found with id {contradiction_id!r}")


def resolve_for_merge(conn, source_id: str, target_id: str, *,
                       note: str = "superseded: source work item merged into another") -> None:
    """Coalesce `source_id`'s open contradictions that collide with an
    already-open one on `target_id`, before `work_items.merge_into` repoints
    `contradictions.work_item_id` from `source_id` to `target_id`.

    `uq_contradictions_open` is a partial unique index on
    `(rule, work_item_id, subject_id) WHERE status='open'`: if both the
    source and the target already carry an open contradiction with the same
    `(rule, subject_id)`, repointing the source's row straight onto the
    target would collide with the target's own open row and raise
    `IntegrityError`. Resolving the source's row first removes it from the
    partial index (only `status='open'` rows are covered), so the repoint
    lands cleanly and the target's own open contradiction -- which already
    describes the same disagreement -- stands as the one live record.

    Must be called from inside the caller's own open transaction (it opens
    none of its own, per the non-reentrancy note in `entities.py`); the
    intended caller is `work_items.merge_into`, immediately before it loops
    over `_MERGE_REPOINT_TABLES`.
    """
    target_open = conn.execute(
        "SELECT rule, subject_id FROM contradictions WHERE work_item_id=? AND status='open' "
        "AND deleted_at IS NULL",
        (target_id,),
    ).fetchall()
    if not target_open:
        return
    target_keys = {(row["rule"], row["subject_id"]) for row in target_open}

    source_open = conn.execute(
        "SELECT id, rule, subject_id FROM contradictions WHERE work_item_id=? AND status='open' "
        "AND deleted_at IS NULL",
        (source_id,),
    ).fetchall()
    now = timeutil.utc_now_iso()
    for row in source_open:
        if (row["rule"], row["subject_id"]) in target_keys:
            conn.execute(
                "UPDATE contradictions SET status='resolved', resolved_at=?, resolution_note=?, "
                "updated_at=? WHERE id=?",
                (now, note, now, row["id"]),
            )


def list_open(conn, *, work_item_id=None, client_id=None):
    """Live, currently-open contradictions, optionally scoped to one work
    item or (via a join) one client."""
    clauses = ["co.status='open'", "co.deleted_at IS NULL"]
    params: list = []
    if work_item_id is not None:
        clauses.append("co.work_item_id=?")
        params.append(work_item_id)
    if client_id is not None:
        clauses.append("wi.client_id=?")
        params.append(client_id)
    rows = conn.execute(
        "SELECT co.* FROM contradictions co LEFT JOIN work_items wi ON wi.id = co.work_item_id "
        f"WHERE {' AND '.join(clauses)} ORDER BY co.detected_at",
        params,
    ).fetchall()
    return rows


def rule_jira_done_open_remaining(conn, work_item_ids) -> "list[dict]":
    """`work_items.jira_status_category='done'` AND >=1 remaining item in
    `remaining.OPEN_STATES` (Ruling 28: includes `reopened`)."""
    clause, params, short_circuit = _work_item_ids_clause(work_item_ids)
    if short_circuit:
        return []
    where = ["jira_status_category='done'", "deleted_at IS NULL"]
    if clause:
        where.append(clause)
    rows = conn.execute(
        f"SELECT * FROM work_items WHERE {' AND '.join(where)}", params
    ).fetchall()

    candidates = []
    for row in rows:
        open_items = remaining.open_items(conn, work_item_id=row["id"])
        if not open_items:
            continue
        candidates.append({
            "rule": "jira_done_open_remaining",
            "work_item_id": row["id"],
            "subject_table": None,
            "subject_id": None,
            "ledger_claim": f"{len(open_items)} remaining item(s) still open",
            "external_claim": _done_claim(row),
        })
    return candidates


def rule_jira_open_work_complete(conn, work_item_ids, *, now: "str | None" = None) -> "list[dict]":
    """`jira_status_category` in (new, indeterminate) AND zero remaining
    items in `remaining.OPEN_STATES` (so `reopened` correctly suppresses
    this rule) AND >=1 activity in the last 14 days whose `outcome` matches
    a completion marker."""
    clause, params, short_circuit = _work_item_ids_clause(work_item_ids)
    if short_circuit:
        return []
    where = ["jira_status_category IN ('new','indeterminate')", "deleted_at IS NULL"]
    if clause:
        where.append(clause)
    rows = conn.execute(
        f"SELECT * FROM work_items WHERE {' AND '.join(where)}", params
    ).fetchall()

    now = now or timeutil.utc_now_iso()
    cutoff = _iso_minus(now, 14)

    candidates = []
    for row in rows:
        if remaining.open_items(conn, work_item_id=row["id"]):
            continue
        activities = conn.execute(
            "SELECT outcome FROM activities WHERE work_item_id=? AND deleted_at IS NULL "
            "AND started_at >= ?",
            (row["id"], cutoff),
        ).fetchall()
        if not any(a["outcome"] and any(marker in a["outcome"].lower() for marker in _COMPLETION_MARKERS)
                   for a in activities):
            continue
        candidates.append({
            "rule": "jira_open_work_complete",
            "work_item_id": row["id"],
            "subject_table": None,
            "subject_id": None,
            "ledger_claim": "All known work complete",
            "external_claim": _status_claim(row),
        })
    return candidates


def rule_assignee_mismatch(conn, work_item_ids, *, now: "str | None" = None) -> "list[dict]":
    """`jira_assignee` is set and matches none of the ledger's own
    `user_identities` for that connection, AND the ledger has an activity by
    the user on that item in the last 30 days."""
    clause, params, short_circuit = _work_item_ids_clause(work_item_ids)
    if short_circuit:
        return []
    where = ["jira_assignee IS NOT NULL", "connection_id IS NOT NULL", "deleted_at IS NULL"]
    if clause:
        where.append(clause)
    rows = conn.execute(
        f"SELECT * FROM work_items WHERE {' AND '.join(where)}", params
    ).fetchall()

    now = now or timeutil.utc_now_iso()
    cutoff = _iso_minus(now, 30)

    candidates = []
    for row in rows:
        mine = conn.execute(
            "SELECT id FROM user_identities WHERE kind='jira' AND connection_id=? AND value=? "
            "AND deleted_at IS NULL",
            (row["connection_id"], row["jira_assignee"]),
        ).fetchone()
        if mine is not None:
            continue  # assigned to one of the user's own identities -- no mismatch

        recent = conn.execute(
            "SELECT id FROM activities WHERE work_item_id=? AND deleted_at IS NULL "
            "AND started_at >= ? LIMIT 1",
            (row["id"], cutoff),
        ).fetchone()
        if recent is None:
            continue

        candidates.append({
            "rule": "assignee_mismatch",
            "work_item_id": row["id"],
            "subject_table": None,
            "subject_id": None,
            "ledger_claim": "Recent work attributed to you",
            "external_claim": _assignee_claim(row),
        })
    return candidates


def rule_removed_requirement(conn, work_item_ids) -> "list[dict]":
    """A remaining item references a Jira issue whose refresh returns
    404/`gone`, or a subtask key no longer present in the parent's payload.

    No task before this one persists that signal anywhere in the schema --
    the Jira adapter/refresh layer (DESIGN §10) has not landed yet -- so
    this always returns `[]` for now. It is wired into `RULES` and
    `evaluate` so the rule activates with no further change to this module
    once that data exists.
    """
    return []


def rule_manual_vs_evidence(conn, work_item_ids) -> "list[dict]":
    """A remaining item that a manual statement marked `completed`,
    followed by a later non-manual activity (commit/test/tool evidence) on
    the same work item -- i.e. work continued after the user said it was
    done."""
    clause, params, short_circuit = _work_item_ids_clause(work_item_ids, column="ri.work_item_id")
    if short_circuit:
        return []
    where = ["ri.state='completed'", "ri.deleted_at IS NULL", "ri.work_item_id IS NOT NULL"]
    if clause:
        where.append(clause)
    rows = conn.execute(
        f"SELECT ri.* FROM remaining_items ri WHERE {' AND '.join(where)}", params
    ).fetchall()

    candidates = []
    for row in rows:
        manual_transition = conn.execute(
            "SELECT occurred_at FROM remaining_item_transitions WHERE remaining_item_id=? "
            "AND to_state='completed' AND source='manual' ORDER BY occurred_at DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        if manual_transition is None:
            continue

        evidence = conn.execute(
            "SELECT started_at FROM activities WHERE work_item_id=? AND deleted_at IS NULL "
            "AND source != 'manual' AND started_at > ? ORDER BY started_at ASC LIMIT 1",
            (row["work_item_id"], manual_transition["occurred_at"]),
        ).fetchone()
        if evidence is None:
            continue

        candidates.append({
            "rule": "manual_vs_evidence",
            "work_item_id": row["work_item_id"],
            "subject_table": "remaining_items",
            "subject_id": row["id"],
            "ledger_claim": f"User said completed at {manual_transition['occurred_at']}",
            "external_claim": f"Evidence of continued work at {evidence['started_at']}",
        })
    return candidates


# The rules `evaluate` scans, and whether each one takes a `now` keyword
# (only the two rules with a rolling time window do). `possible_duplicate`
# is deliberately excluded (see module docstring): it is opened inline by
# `infer.dedupe` and never opened or resolved by a scan.
_SCAN_RULES = (
    ("jira_done_open_remaining", rule_jira_done_open_remaining, False),
    ("jira_open_work_complete", rule_jira_open_work_complete, True),
    ("assignee_mismatch", rule_assignee_mismatch, True),
    ("removed_requirement", rule_removed_requirement, False),
    ("manual_vs_evidence", rule_manual_vs_evidence, False),
)


def _open_rows_for_rule(conn, rule: str, work_item_ids):
    clause, params, short_circuit = _work_item_ids_clause(work_item_ids)
    if short_circuit:
        return []
    where = ["status='open'", "deleted_at IS NULL", "rule=?"]
    query_params = [rule] + params
    if clause:
        where.append(clause)
    return conn.execute(
        f"SELECT * FROM contradictions WHERE {' AND '.join(where)}", query_params
    ).fetchall()


def evaluate(conn, *, work_item_ids: "list[str] | None" = None, now: "str | None" = None,
             hook_mode: bool = False) -> dict:
    """Run every scanned rule, opening fresh contradictions and resolving
    ones whose condition no longer holds. Scoped to `work_item_ids` when
    given, else the whole ledger. Never writes to `work_items`.

    `hook_mode=True` threads through to every `open_contradiction`/`resolve`
    write below (Minor #5, final review): `jira.refresh.refresh_rest` runs
    this on the SessionStart hook path and now passes `hook_mode=True` like
    every other hook write, instead of burning the full non-hook retry
    budget. Default `False` for CLI/model-loop callers (`worklog
    contradictions`, `jira.mcp.ingest`).
    """
    now = now or timeutil.utc_now_iso()
    opened = []
    resolved = []

    for rule_name, rule_fn, needs_now in _SCAN_RULES:
        candidates = rule_fn(conn, work_item_ids, now=now) if needs_now else rule_fn(conn, work_item_ids)

        fresh_keys = {(c["work_item_id"], c["subject_id"]) for c in candidates}

        for candidate in candidates:
            new_id = open_contradiction(conn, hook_mode=hook_mode, **candidate)
            if new_id is not None:
                opened.append({"id": new_id, **candidate})

        for row in _open_rows_for_rule(conn, rule_name, work_item_ids):
            key = (row["work_item_id"], row["subject_id"])
            if key in fresh_keys:
                continue
            resolve(conn, row["id"], note=f"{rule_name}: condition no longer holds",
                    hook_mode=hook_mode)
            resolved.append({"id": row["id"], "rule": rule_name, "work_item_id": row["work_item_id"]})

    return {"opened": opened, "resolved": resolved}
