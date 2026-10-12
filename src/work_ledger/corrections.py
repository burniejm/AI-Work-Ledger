"""Corrections: an append-only audit trail with an undo expressed as new rows.

Ruling 7 (binding): the `corrections` table is written by INSERT only, never
UPDATE, never DELETE, for the life of this module. There is no
`UPDATE corrections ...` statement anywhere below. `undo()` replays a group
in reverse and writes one new inverse `corrections` row per original, each
carrying `undo_of_correction_id` pointing at the row it inverts; whether a
correction "is undone" is answered by `is_undone()`, a lookup against that
column -- never by reading a mutated `undone_at` (the column stays reserved
and always NULL).

Every write goes through `db.connection.transaction` (BEGIN IMMEDIATE with
the standard bounded busy retry). Per the non-reentrancy note in
`entities.py`, `entities.current_device` is always called *before* opening
a transaction here. Because `connection.transaction` cannot nest, `group()`
itself opens no transaction of its own -- each caller wraps the statements
that need atomicity in its own `with connection.transaction(conn):` block.
When a learned alias needs to land atomically with its own audit row
(`move_activity`), the write goes through `entities._add_alias_in_txn` (an
internal helper that assumes an already-open transaction and an
already-resolved `device_id`) inside that block, rather than through the
transaction-opening `entities.add_alias`.

`CORRECTABLE_FIELDS` is a module-level dict literal whose keys are string
literals and whose values are `frozenset({...})` literals of string
literals -- Task 38's SQL guard (Ruling 9) parses this file with `ast` and
asserts exactly that shape, which is what makes this module safe to
allowlist. Both `table` and `field` in `apply_field_update` (and `table` in
`soft_delete_record`) are validated against this allowlist (or, for
soft-delete, `entities.SOFT_DELETABLE`) before either name is interpolated
into an UPDATE statement; every value is still a bound parameter.

`source_events` is never a correction target -- it simply is not a key of
`CORRECTABLE_FIELDS` and is not in `entities.SOFT_DELETABLE`, so any attempt
to route it through this module raises `ValueError`.
"""
import collections
import contextlib
import json
import sqlite3

from work_ledger import entities, ids, timeutil, work_items
from work_ledger.db import connection
from work_ledger.infer import stage_a as _stage_a

__all__ = [
    "CORRECTABLE_FIELDS",
    "AlreadyUndone",
    "CorrectionGroup",
    "group",
    "apply_field_update",
    "move_activity",
    "soft_delete_record",
    "undo",
    "is_undone",
    "undone_ids",
    "history",
]

# Per-table allowlist of fields a correction may overwrite. Verbatim from
# the task brief; Task 38's SQL guard depends on this exact literal shape.
# `idempotency_key` was added for Important #2 (final review): `move_activity`
# rewrites it to match the activity's new attribution (see below), and that
# rewrite is recorded as an ordinary `corrections` row so `undo()` -- which
# validates every field it inverts against this same allowlist -- can invert
# it too.
CORRECTABLE_FIELDS = {
    "activities": frozenset({"work_item_id","client_id","project_id","kind","summary","outcome",
                             "started_at","ended_at","stated_duration_minutes","confidence","in_inbox",
                             "idempotency_key"}),
    "remaining_items": frozenset({"work_item_id","client_id","project_id","title","detail","state","kind","confidence","in_inbox"}),
    "work_items": frozenset({"client_id","project_id","title","description","local_state",
                             "ai_usage_score","ai_usage_confidence","ai_usage_reason"}),
    "clients": frozenset({"name","notes"}),
    "projects": frozenset({"client_id","name"}),
    "decisions": frozenset({"work_item_id","statement","rationale","decided_at","confidence"}),
    "contradictions": frozenset({"status","resolution_note"}),
}


class AlreadyUndone(Exception):
    """Raised by `undo()` when every row in the target group already has an inverse row."""


class CorrectionGroup:
    """A handle shared by every `corrections` row written for one logical correction.

    `record()` performs a plain INSERT -- it opens no transaction of its
    own, so it may be called either inside a caller-managed
    `with connection.transaction(conn):` block or, for a row recorded after
    that block has already committed (e.g. the learned-alias row in
    `move_activity`), on its own.
    """

    def __init__(self, conn: sqlite3.Connection, group_id: str, actor: str, reason: str, device_id: str):
        self.conn = conn
        self.id = group_id
        self.actor = actor
        self.reason = reason
        self.device_id = device_id

    def record(self, target_table: str, target_id: str, field, old_value, new_value, operation: str) -> None:
        now = timeutil.utc_now_iso()
        self.conn.execute(
            "INSERT INTO corrections(id, correction_group_id, target_table, target_id, field, "
            "old_value_json, new_value_json, operation, reason, actor, applied_at, "
            "undo_of_correction_id, origin_device_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)",
            (ids.new_id(), self.id, target_table, target_id, field,
             json.dumps(old_value), json.dumps(new_value), operation,
             self.reason, self.actor, now, self.device_id, now),
        )


@contextlib.contextmanager
def group(conn: sqlite3.Connection, *, actor: str = "user", reason: str):
    """Yield a `CorrectionGroup` sharing one `correction_group_id`.

    Does not open a `connection.transaction` block itself (see module
    docstring) -- callers manage their own transaction boundaries around
    whichever statements need atomicity.
    """
    device_id = entities.current_device(conn)
    yield CorrectionGroup(conn, ids.new_id(), actor, reason, device_id)


def apply_field_update(conn: sqlite3.Connection, table: str, row_id: str, field: str, new_value,
                        *, reason: str, actor: str = "user") -> str:
    """Overwrite one allowlisted field on one allowlisted table, with audit.

    `table` must be a key of `CORRECTABLE_FIELDS` and `field` must be in
    that table's allowed set; otherwise raises `ValueError` before any SQL
    referencing either name is built.
    """
    if table not in CORRECTABLE_FIELDS or field not in CORRECTABLE_FIELDS[table]:
        raise ValueError(f"{table}.{field} is not a correctable field")

    with group(conn, actor=actor, reason=reason) as g:
        with connection.transaction(conn):
            row = conn.execute(f"SELECT {field} FROM {table} WHERE id=?", (row_id,)).fetchone()
            if row is None:
                raise ValueError(f"no such row: {table}.{row_id!r}")
            old_value = row[0]
            now = timeutil.utc_now_iso()
            conn.execute(f"UPDATE {table} SET {field}=?, updated_at=? WHERE id=?", (new_value, now, row_id))
            g.record(table, row_id, field, old_value, new_value, "update")
            _invalidate_summary_cache(conn)
    return g.id


# A minimal stand-in for `infer.stage_a.ActivityDraft` -- `_idempotency_key`
# only ever reads `.client_id`/`.project_id`/`.work_item_id` off whatever it
# is given, so a 3-field namedtuple is enough to reuse the exact function
# rather than duplicating its hash format (Important #2).
_KeyAttribution = collections.namedtuple(
    "_KeyAttribution", ("client_id", "project_id", "work_item_id")
)


def _local_item_title(conn: sqlite3.Connection, client_id, project_id) -> str:
    """Scope label for a correction's auto-created local item: the project's
    name if it resolved one, else the client's. Deterministic per scope, so
    repeated corrections into the same client/project converge on the same
    item via `work_items.find_reusable_local`'s title match rather than
    spawning a new one each time."""
    if project_id is not None:
        row = conn.execute("SELECT name FROM projects WHERE id=?", (project_id,)).fetchone()
    else:
        row = conn.execute("SELECT name FROM clients WHERE id=?", (client_id,)).fetchone()
    scope = row["name"] if row is not None else "work"
    return f"{scope}: general work"


def move_activity(conn: sqlite3.Connection, activity_id: str, *, work_item_id=None, client_id=None,
                   project_id=None, reason: str, learn_alias: bool = False) -> str:
    """Re-attribute an activity's work item/client/project, with audit.

    Only fields actually supplied *and different* from the current value
    are written and recorded. When `learn_alias=True` *and* the supplied
    `client_id`/`project_id` actually differs from the activity's prior
    value (a no-op re-supply of the current value never learns an alias),
    an `aliases` row mapping the old attribution's name to the new target
    is written (`source='correction'`) in the *same transaction* as its
    audit row, so `undo()` removes it too.

    Important #2 (final review): a `source='stage_a'` activity's
    `idempotency_key` is `sha256("stage_a|<session>|<client>|<project>|
    <work_item>")` (`infer.stage_a._idempotency_key`). If a move leaves that
    key at its old attribution, a later Stage A re-run whose attribution
    inputs changed (e.g. `--learn-alias` teaching a new client/project
    mapping) computes a key that matches no row and INSERTs a duplicate
    activity instead of updating this one. So when the move actually changes
    the attribution on a `stage_a`-sourced, session-backed activity, its
    `idempotency_key` is rewritten to match -- via this same function, so
    the two can never drift into different formats -- and recorded as an
    ordinary `activities.idempotency_key` correction in the same group
    (`undo()` inverts it like any other field, since it is now in
    `CORRECTABLE_FIELDS`).

    Auto-attaching a local item: a client/project-only correction on an
    activity that has no work item (`work_item_id is None` on both the call
    and the row) would otherwise leave it in a permanently title-less
    "Unassigned" bucket forever -- nothing else ever revisits it. So when the
    correction's resolved client/project isn't entirely null, this gets or
    creates a local item for that scope (`work_items.get_or_create_local`,
    the same call Stage A makes at capture time) and uses it as the move
    target, exactly as if `--to-work-item` had named it. This has to happen
    *before* `group()`/the transaction below: `get_or_create_local` opens its
    own `connection.transaction`, which cannot nest inside another.
    """
    if work_item_id is None:
        pre_row = conn.execute(
            "SELECT work_item_id, client_id, project_id FROM activities WHERE id=?", (activity_id,)
        ).fetchone()
        if pre_row is None:
            raise ValueError(f"no such activity: {activity_id!r}")
        if pre_row["work_item_id"] is None:
            final_client_id = client_id if client_id is not None else pre_row["client_id"]
            final_project_id = project_id if project_id is not None else pre_row["project_id"]
            if final_client_id is not None or final_project_id is not None:
                work_item_id, _created = work_items.get_or_create_local(
                    conn, _local_item_title(conn, final_client_id, final_project_id),
                    client_id=final_client_id, project_id=final_project_id)

    with group(conn, actor="user", reason=reason) as g:
        with connection.transaction(conn):
            row = conn.execute(
                "SELECT work_item_id, client_id, project_id, source, session_row_id, "
                "idempotency_key FROM activities WHERE id=?", (activity_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"no such activity: {activity_id!r}")

            changes = []
            moved_work_items = set()
            if work_item_id is not None and work_item_id != row["work_item_id"]:
                changes.append(("work_item_id", row["work_item_id"], work_item_id))
                moved_work_items.add(work_item_id)
                if row["work_item_id"] is not None:
                    moved_work_items.add(row["work_item_id"])
            if client_id is not None and client_id != row["client_id"]:
                changes.append(("client_id", row["client_id"], client_id))
            if project_id is not None and project_id != row["project_id"]:
                changes.append(("project_id", row["project_id"], project_id))

            if changes:
                now = timeutil.utc_now_iso()
                sets = [f"{field}=?" for field, _, _ in changes]
                params = [new for _, _, new in changes]

                new_idempotency_key = None
                if row["source"] == "stage_a" and row["session_row_id"] is not None:
                    session_row = conn.execute(
                        "SELECT session_id FROM sessions WHERE id=?", (row["session_row_id"],)
                    ).fetchone()
                    if session_row is not None:
                        final_attribution = _KeyAttribution(
                            client_id=client_id if client_id is not None else row["client_id"],
                            project_id=project_id if project_id is not None else row["project_id"],
                            work_item_id=work_item_id if work_item_id is not None else row["work_item_id"],
                        )
                        candidate_key = _stage_a._idempotency_key(
                            session_row["session_id"], final_attribution
                        )
                        if candidate_key != row["idempotency_key"]:
                            new_idempotency_key = candidate_key

                if new_idempotency_key is not None:
                    sets.append("idempotency_key=?")
                    params.append(new_idempotency_key)

                sets.append("updated_at=?")
                params.append(now)
                params.append(activity_id)
                conn.execute(f"UPDATE activities SET {', '.join(sets)} WHERE id=?", params)
                for field, old, new in changes:
                    g.record("activities", activity_id, field, old, new, "move")
                if new_idempotency_key is not None:
                    g.record("activities", activity_id, "idempotency_key",
                             row["idempotency_key"], new_idempotency_key, "move")

            _invalidate_summary_cache(conn)

        # Only learn an alias when the attribution actually changed
        # (client_id/project_id differs from the activity's prior value) --
        # re-supplying the current value with learn_alias=True is a no-op,
        # never a self-referential alias.
        alias_kind = old_ref_id = alias_target_id = None
        if learn_alias:
            if client_id is not None and row["client_id"] is not None and client_id != row["client_id"]:
                alias_kind, old_ref_id, alias_target_id = "client", row["client_id"], client_id
            elif project_id is not None and row["project_id"] is not None and project_id != row["project_id"]:
                alias_kind, old_ref_id, alias_target_id = "project", row["project_id"], project_id

        if alias_kind is not None:
            # alias_kind is always one of the two literals above ("client" or
            # "project"), never taken from user/model/Jira text, so this
            # f-string interpolation of a table name is safe.
            name_row = conn.execute(f"SELECT name FROM {alias_kind}s WHERE id=?", (old_ref_id,)).fetchone()
            if name_row is not None:
                # The alias write and its audit row must be atomic with each
                # other (Important 2): entities.add_alias opens its own
                # transaction, so we use the non-transactional
                # _add_alias_in_txn helper inside our own block instead,
                # reusing the device id `group()` already resolved.
                with connection.transaction(conn):
                    alias_id = entities._add_alias_in_txn(
                        conn, alias_kind, name_row["name"], alias_kind, alias_target_id,
                        source="correction", device_id=g.device_id,
                    )
                    g.record("aliases", alias_id, None, None, None, "create")

    # A move changes which sessions' evidence backs each ticket, so both
    # sides' AI-usage scores are stale. Recompute after the correction's own
    # transactions have committed; the recompute skips any score a live
    # correction pins. Local import: infer.ai_usage reaches back into
    # infer.stage_a, which this module already imports.
    if moved_work_items:
        from work_ledger.infer import ai_usage as _ai_usage
        for stale_item_id in sorted(moved_work_items):
            _ai_usage.recompute_for_work_item(conn, stale_item_id)

    return g.id


def soft_delete_record(conn: sqlite3.Connection, table: str, row_id: str, *, reason: str) -> str:
    """Soft-delete one row on one allowlisted table, with audit.

    `table` must be in `entities.SOFT_DELETABLE`; otherwise raises
    `ValueError` before any SQL referencing it is built.
    """
    if table not in entities.SOFT_DELETABLE:
        raise ValueError(f"table {table!r} is not soft-deletable")

    with group(conn, actor="user", reason=reason) as g:
        with connection.transaction(conn):
            row = conn.execute(f"SELECT deleted_at FROM {table} WHERE id=?", (row_id,)).fetchone()
            if row is None:
                raise ValueError(f"no such row: {table}.{row_id!r}")
            old_deleted_at = row["deleted_at"]
            now = timeutil.utc_now_iso()
            conn.execute(f"UPDATE {table} SET deleted_at=? WHERE id=?", (now, row_id))
            g.record(table, row_id, "deleted_at", old_deleted_at, now, "soft_delete")
            _invalidate_summary_cache(conn)
    return g.id


def _invert_one(conn: sqlite3.Connection, row: sqlite3.Row, new_group_id: str, device_id: str, now: str) -> None:
    op = row["operation"]
    table = row["target_table"]
    target_id = row["target_id"]
    field = row["field"]
    old_value = json.loads(row["old_value_json"]) if row["old_value_json"] is not None else None
    new_value = json.loads(row["new_value_json"]) if row["new_value_json"] is not None else None

    if op == "create":
        if table not in entities.SOFT_DELETABLE:
            raise ValueError(f"cannot undo creation of {table!r}: not soft-deletable")
        conn.execute(f"UPDATE {table} SET deleted_at=? WHERE id=?", (now, target_id))
        inverse_op, inverse_field, inverse_old, inverse_new = "soft_delete", "deleted_at", None, now
    elif op in ("soft_delete", "restore"):
        if table not in entities.SOFT_DELETABLE:
            raise ValueError(f"cannot undo {op} of {table!r}: not soft-deletable")
        conn.execute(f"UPDATE {table} SET deleted_at=? WHERE id=?", (old_value, target_id))
        inverse_op = "restore" if op == "soft_delete" else "soft_delete"
        inverse_field, inverse_old, inverse_new = field, new_value, old_value
    elif field is not None:
        if table not in CORRECTABLE_FIELDS or field not in CORRECTABLE_FIELDS[table]:
            raise ValueError(f"cannot undo correction on non-correctable field {table}.{field}")
        conn.execute(f"UPDATE {table} SET {field}=?, updated_at=? WHERE id=?", (old_value, now, target_id))
        inverse_op, inverse_field, inverse_old, inverse_new = op, field, new_value, old_value
    else:
        raise ValueError(f"cannot invert correction operation {op!r} on {table} with no field")

    conn.execute(
        "INSERT INTO corrections(id, correction_group_id, target_table, target_id, field, "
        "old_value_json, new_value_json, operation, reason, actor, applied_at, "
        "undo_of_correction_id, origin_device_id, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ids.new_id(), new_group_id, table, target_id, inverse_field,
         json.dumps(inverse_old), json.dumps(inverse_new), inverse_op,
         f"undo of {row['id']}", row["actor"], now, row["id"], device_id, now),
    )


def undo(conn: sqlite3.Connection, correction_group_id: str) -> str:
    """Write one new inverse `corrections` row per original row in the group.

    Never issues `UPDATE corrections ...` (Ruling 7). Rows in the group
    that already have an inverse (per `is_undone`) are skipped rather than
    inverted twice; raises `AlreadyUndone` only when *every* row in the
    group already has one. Returns the new group id.
    """
    device_id = entities.current_device(conn)
    rows = conn.execute(
        "SELECT * FROM corrections WHERE correction_group_id=? ORDER BY applied_at, id",
        (correction_group_id,),
    ).fetchall()
    if not rows:
        raise ValueError(f"no such correction group: {correction_group_id!r}")

    ids_in_group = {row["id"] for row in rows}
    already = undone_ids(conn, correction_group_id)
    if ids_in_group <= already:
        raise AlreadyUndone(f"correction group {correction_group_id!r} is already fully undone")

    new_group_id = ids.new_id()
    now = timeutil.utc_now_iso()
    with connection.transaction(conn):
        for row in reversed(rows):
            if row["id"] in already:
                continue
            _invert_one(conn, row, new_group_id, device_id, now)
        _invalidate_summary_cache(conn)
    return new_group_id


def is_undone(conn: sqlite3.Connection, correction_id: str) -> bool:
    """Derived, not stored: is there a row whose `undo_of_correction_id` matches?"""
    row = conn.execute(
        "SELECT 1 FROM corrections WHERE undo_of_correction_id = ? LIMIT 1", (correction_id,)
    ).fetchone()
    return row is not None


def undone_ids(conn: sqlite3.Connection, correction_group_id: str) -> "set[str]":
    """The ids within `correction_group_id` that already have an inverse row."""
    rows = conn.execute(
        "SELECT id FROM corrections WHERE correction_group_id=?", (correction_group_id,)
    ).fetchall()
    return {row["id"] for row in rows if is_undone(conn, row["id"])}


def history(conn: sqlite3.Connection, table: str, row_id: str) -> "list[sqlite3.Row]":
    """This record's full correction history (originals and inverses), oldest first."""
    return conn.execute(
        "SELECT * FROM corrections WHERE target_table=? AND target_id=? ORDER BY applied_at, id",
        (table, row_id),
    ).fetchall()


def _invalidate_summary_cache(conn: sqlite3.Connection) -> None:
    """Every correction invalidates the cache; scope narrowing is deferred to a later task."""
    conn.execute("DELETE FROM summary_cache")
