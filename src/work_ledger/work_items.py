"""Work items: local (LOCAL-NNNN display ids) and Jira-backed, link and merge.

Every write goes through `db.connection.transaction` (BEGIN IMMEDIATE with
the standard bounded busy retry). Per the non-reentrancy note in
`entities.py`, `entities.current_device` is always called *before* opening
this module's own transaction.

Ruling 23: `textnorm` (Task 4) is the single owner of the stop-word list, the
stemming map, normalize_key/normalize_title, and token_set. This module re-exports
`normalize_title` and defines no stopword list, stemming map, or
normalization pipeline of its own; `dedupe` (Task 28) imports the same
functions from `textnorm` directly.
"""
from work_ledger import contradictions, entities, ids, timeutil
from work_ledger.db import connection
from work_ledger.textnorm import normalize_title, token_set

__all__ = [
    "next_display_id",
    "create_local",
    "upsert_external",
    "find_reusable_local",
    "get_or_create_local",
    "link",
    "merge_into",
    "link_local_to_jira",
    "normalize_title",
]

# find_reusable_local only ever considers local items in one of these states
# (Step 3 of the brief); 'completed' and 'cancelled' are excluded.
_OPEN_LOCAL_STATES = ("open", "in_progress", "blocked", "waiting")

# Tables merge_into repoints from source_id to target_id. Fixed, module-level
# allowlist — never built from user, model, or Jira text (same pattern as
# entities.SOFT_DELETABLE) — so the f-string interpolation below is safe.
_MERGE_REPOINT_TABLES = ("activities", "remaining_items", "decisions", "contradictions")


def next_display_id(conn, scope: str = "LOCAL") -> str:
    """Atomically allocate the next zero-padded display id for `scope`.

    e.g. 'LOCAL-0192'. Must be called inside the caller's own BEGIN
    IMMEDIATE transaction (it does not open one itself); relies on
    INSERT..ON CONFLICT..RETURNING against display_id_counters so two
    concurrent processes can never receive the same number.
    """
    now = timeutil.utc_now_iso()
    row = conn.execute(
        "INSERT INTO display_id_counters(scope, next_value, updated_at) VALUES (?,1,?) "
        "ON CONFLICT(scope) DO UPDATE SET next_value = next_value + 1, updated_at = excluded.updated_at "
        "RETURNING next_value",
        (scope, now),
    ).fetchone()
    return f"{scope}-{row['next_value']:04d}"


def create_local(conn, title: str, *, client_id=None, project_id=None, description=None,
                  hook_mode: bool = False) -> str:
    """Create a new kind='local' work item with a fresh LOCAL-NNNN display id.

    `hook_mode=True` threads through to `entities.current_device` and
    `connection.transaction` so a hook-path caller (Stage A running inside
    the Stop hook) gives up on lock contention within the hook budget
    instead of burning the full non-hook retry schedule.
    """
    device_id = entities.current_device(conn, hook_mode=hook_mode)
    now = timeutil.utc_now_iso()
    work_item_id = ids.new_id()
    with connection.transaction(conn, hook_mode=hook_mode):
        display_id = next_display_id(conn, "LOCAL")
        conn.execute(
            "INSERT INTO work_items(id, kind, client_id, project_id, display_id, title, description, "
            "origin_device_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (work_item_id, "local", client_id, project_id, display_id, title, description,
             device_id, now, now),
        )
    return work_item_id


def upsert_external(conn, connection_id: str | None, external_key: str, *, title: str,
                     external_id=None, client_id=None, project_id=None,
                     hook_mode: bool = False) -> str:
    """Idempotent on (connection_id, external_key).

    Ruling 18: `connection_id` may be None for a free-text ticket key that no
    connection claims yet (Task 16). Such an orphan row is unique on
    `external_key` alone (`uq_work_items_orphan_key`), so the lookup that
    precedes the insert also checks for a matching orphan row when no exact
    (connection_id, external_key) row exists. When a connection later claims
    the key (this is called again with a non-null connection_id), the
    orphan row is adopted in place — its connection_id is set — rather than
    creating a second row.

    `hook_mode=True` threads through to `entities.current_device` and
    `connection.transaction` (see `create_local`).
    """
    device_id = entities.current_device(conn, hook_mode=hook_mode)
    now = timeutil.utc_now_iso()
    with connection.transaction(conn, hook_mode=hook_mode):
        row = None
        if connection_id is not None:
            row = conn.execute(
                "SELECT id FROM work_items WHERE connection_id=? AND external_key=? "
                "AND deleted_at IS NULL",
                (connection_id, external_key),
            ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT id FROM work_items WHERE connection_id IS NULL AND external_key=? "
                "AND deleted_at IS NULL",
                (external_key,),
            ).fetchone()

        if row is not None:
            sets = ["title=?", "updated_at=?"]
            params = [title, now]
            if connection_id is not None:
                sets.append("connection_id=?")
                params.append(connection_id)
            if external_id is not None:
                sets.append("external_id=?")
                params.append(external_id)
            if client_id is not None:
                sets.append("client_id=?")
                params.append(client_id)
            if project_id is not None:
                sets.append("project_id=?")
                params.append(project_id)
            params.append(row["id"])
            conn.execute(f"UPDATE work_items SET {', '.join(sets)} WHERE id=?", params)
            return row["id"]

        work_item_id = ids.new_id()
        conn.execute(
            "INSERT INTO work_items(id, kind, client_id, project_id, connection_id, external_key, "
            "external_id, title, origin_device_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (work_item_id, "jira", client_id, project_id, connection_id, external_key,
             external_id, title, device_id, now, now),
        )
        return work_item_id


def find_reusable_local(conn, title: str, *, client_id, project_id, min_similarity: float = 0.70) -> str | None:
    """Return an open local item worth reusing instead of creating a new one.

    Considers only `kind='local' AND deleted_at IS NULL AND local_state IN
    (open, in_progress, blocked, waiting)` within the given
    `(client_id, project_id)` scope. Similarity is Jaccard over
    `textnorm.token_set(title)`. Returns the best-scoring candidate at or
    above `min_similarity` (Ruling 5: default 0.70), else None.
    """
    candidate_tokens = token_set(title)
    placeholders = ",".join("?" for _ in _OPEN_LOCAL_STATES)
    rows = conn.execute(
        "SELECT id, title FROM work_items WHERE kind='local' AND deleted_at IS NULL "
        f"AND client_id IS ? AND project_id IS ? AND local_state IN ({placeholders})",
        (client_id, project_id, *_OPEN_LOCAL_STATES),
    ).fetchall()

    best_id = None
    best_score = 0.0
    for row in rows:
        existing_tokens = token_set(row["title"])
        union = candidate_tokens | existing_tokens
        if not union:
            continue
        score = len(candidate_tokens & existing_tokens) / len(union)
        if score >= min_similarity and score > best_score:
            best_score = score
            best_id = row["id"]
    return best_id


def get_or_create_local(conn, title: str, *, client_id, project_id,
                        hook_mode: bool = False) -> "tuple[str, bool]":
    """Return (work_item_id, created); reuses a matching item via `find_reusable_local`."""
    existing = find_reusable_local(conn, title, client_id=client_id, project_id=project_id)
    if existing is not None:
        return existing, False
    return create_local(conn, title, client_id=client_id, project_id=project_id,
                        hook_mode=hook_mode), True


def link(conn, from_id: str, to_id: str, link_type: str, *, note=None) -> str:
    """Idempotent on (from_work_item_id, to_work_item_id, link_type)."""
    device_id = entities.current_device(conn)
    now = timeutil.utc_now_iso()
    with connection.transaction(conn):
        row = conn.execute(
            "SELECT id FROM work_item_links WHERE from_work_item_id=? AND to_work_item_id=? "
            "AND link_type=? AND deleted_at IS NULL",
            (from_id, to_id, link_type),
        ).fetchone()
        if row is not None:
            if note is not None:
                conn.execute(
                    "UPDATE work_item_links SET note=?, updated_at=? WHERE id=?",
                    (note, now, row["id"]),
                )
            return row["id"]

        link_id = ids.new_id()
        conn.execute(
            "INSERT INTO work_item_links(id, from_work_item_id, to_work_item_id, link_type, note, "
            "origin_device_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (link_id, from_id, to_id, link_type, note, device_id, now, now),
        )
        return link_id


def merge_into(conn, source_id: str, target_id: str, *, reason: str) -> None:
    """Merge `source_id` into `target_id`, losslessly.

    Repoints `activities`, `remaining_items`, `decisions`, and
    `contradictions` rows from `source_id` to `target_id` (all evidence
    stays attached to those repointed records — nothing is deleted or
    summarized away), sets `source.merged_into_id = target_id` and
    `source.deleted_at = now`, and records a `merged_into` work_item_links
    row.

    `contradictions.resolve_for_merge` runs first, inside this same
    transaction: `uq_contradictions_open` would otherwise raise
    `IntegrityError` when the source and target both carry an open
    contradiction with the same `(rule, subject_id)` -- the repoint below
    would try to land two open rows on the same key.
    """
    device_id = entities.current_device(conn)
    now = timeutil.utc_now_iso()
    with connection.transaction(conn):
        contradictions.resolve_for_merge(conn, source_id, target_id)
        for table in _MERGE_REPOINT_TABLES:
            conn.execute(
                f"UPDATE {table} SET work_item_id=?, updated_at=? WHERE work_item_id=?",
                (target_id, now, source_id),
            )

        conn.execute(
            "UPDATE work_items SET merged_into_id=?, deleted_at=?, updated_at=? WHERE id=?",
            (target_id, now, now, source_id),
        )

        link_id = ids.new_id()
        conn.execute(
            "INSERT INTO work_item_links(id, from_work_item_id, to_work_item_id, link_type, note, "
            "origin_device_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (link_id, source_id, target_id, "merged_into", reason, device_id, now, now),
        )


def link_local_to_jira(conn, local_id: str, connection_id: str, external_key: str, *, title: str) -> str:
    """Create/update the Jira-backed item for (connection_id, external_key),
    merge the local item into it, and preserve the local item's display id
    as a "(was LOCAL-0192)" prefix line on the Jira row's description.
    """
    local_row = conn.execute(
        "SELECT display_id FROM work_items WHERE id=?", (local_id,)
    ).fetchone()
    local_display_id = local_row["display_id"] if local_row is not None else None

    jira_id = upsert_external(conn, connection_id, external_key, title=title)

    if local_display_id:
        prefix = f"(was {local_display_id})"
        now = timeutil.utc_now_iso()
        with connection.transaction(conn):
            row = conn.execute(
                "SELECT description FROM work_items WHERE id=?", (jira_id,)
            ).fetchone()
            existing_description = row["description"] if row is not None else None
            new_description = f"{prefix}\n{existing_description}" if existing_description else prefix
            conn.execute(
                "UPDATE work_items SET description=?, updated_at=? WHERE id=?",
                (new_description, now, jira_id),
            )

    merge_into(conn, local_id, jira_id, reason=f"linked to {external_key}")
    return jira_id
