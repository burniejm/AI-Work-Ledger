"""Entity repository: devices, clients, projects, aliases, and repositories.

Every write goes through `db.connection.transaction` (BEGIN IMMEDIATE with
the standard bounded busy retry). `soft_delete`'s `table` argument is only
ever taken from the module-level `SOFT_DELETABLE` allowlist below — never
from user, model, or Jira text — so the f-string interpolation of the
validated name is safe (Task 38's SQL guard allowlists this exact pattern).

Ruling 29: `config.get("device_name")` is consumed by `current_device` and
nowhere else in the codebase.
"""
import json
import re
import socket
import sys

from work_ledger import config, ids, timeutil
from work_ledger.db import connection

# Table names a caller may soft-delete by. Validated against this frozen set
# before any SQL is built; anything else raises ValueError.
SOFT_DELETABLE = frozenset({
    "clients", "projects", "work_items", "work_item_links", "aliases",
    "repositories", "repository_mappings", "connections", "user_identities",
    "activities", "remaining_items", "decisions", "contradictions",
})

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Lowercase, run of non-alphanumeric characters -> '-', strip leading/trailing '-'."""
    slug = _SLUG_RE.sub("-", name.lower())
    return slug.strip("-")


def current_device(conn, *, hook_mode: bool = False) -> str:
    """Return this machine's `devices.id`, creating the row once.

    The row is keyed by a stable `device_key` persisted in `settings`
    (generated once, on first use). `platform` comes from `sys.platform`;
    `hostname` is `config.get("device_name") or socket.gethostname()`
    (Ruling 29). A later change to `device_name` updates the existing row's
    hostname in place rather than creating a second device.

    `hook_mode=True` threads through to `connection.transaction` so a hook
    caller (which opened its connection with `connection.connect(hook_mode=
    True)`) gives up on lock contention within the hook budget instead of
    the full non-hook retry schedule -- this first-registration path is rare
    (it only writes once per device) but there is no reason to let it be the
    one place a hook blocks for seconds.
    """
    hostname = config.get("device_name") or socket.gethostname()
    platform_name = sys.platform
    now = timeutil.utc_now_iso()
    with connection.transaction(conn, hook_mode=hook_mode):
        key_row = conn.execute(
            "SELECT value FROM settings WHERE key='device_key'"
        ).fetchone()
        if key_row is None:
            device_key = ids.new_id()
            conn.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES (?,?,?)",
                ("device_key", device_key, now),
            )
        else:
            device_key = key_row["value"]

        row = conn.execute(
            "SELECT id, hostname FROM devices WHERE device_key=?", (device_key,)
        ).fetchone()
        if row is None:
            device_id = ids.new_id()
            conn.execute(
                "INSERT INTO devices(id, device_key, hostname, platform, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (device_id, device_key, hostname, platform_name, now, now),
            )
            return device_id

        if row["hostname"] != hostname:
            conn.execute(
                "UPDATE devices SET hostname=?, updated_at=? WHERE id=?",
                (hostname, now, row["id"]),
            )
        return row["id"]


def upsert_client(conn, name: str, *, notes=None) -> str:
    """Return a live client's id, matching on slug; creates one if none exists."""
    slug = slugify(name)
    # Separate transaction: connection.transaction() is not reentrant (BEGIN
    # IMMEDIATE would raise if called while one is already open on this
    # connection), so current_device() must complete and commit before the
    # entity-write transaction below opens its own.
    device_id = current_device(conn)
    now = timeutil.utc_now_iso()
    with connection.transaction(conn):
        row = conn.execute(
            "SELECT id FROM clients WHERE slug=? AND deleted_at IS NULL", (slug,)
        ).fetchone()
        if row is not None:
            if notes is not None:
                conn.execute(
                    "UPDATE clients SET notes=?, updated_at=? WHERE id=?",
                    (notes, now, row["id"]),
                )
            return row["id"]

        client_id = ids.new_id()
        conn.execute(
            "INSERT INTO clients(id, name, slug, notes, origin_device_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (client_id, name, slug, notes, device_id, now, now),
        )
        return client_id


def upsert_project(conn, client_id: str, name: str) -> str:
    """Return a live project's id under `client_id`, matching on slug."""
    slug = slugify(name)
    device_id = current_device(conn)
    now = timeutil.utc_now_iso()
    with connection.transaction(conn):
        row = conn.execute(
            "SELECT id FROM projects WHERE client_id=? AND slug=? AND deleted_at IS NULL",
            (client_id, slug),
        ).fetchone()
        if row is not None:
            return row["id"]

        project_id = ids.new_id()
        conn.execute(
            "INSERT INTO projects(id, client_id, name, slug, origin_device_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (project_id, client_id, name, slug, device_id, now, now),
        )
        return project_id


def get_client_by_name(conn, name: str):
    """Resolve a client by name: through `aliases` first, then by slug."""
    alias = resolve_alias(conn, "client", name)
    if alias is not None and alias["target_kind"] == "client":
        row = conn.execute(
            "SELECT * FROM clients WHERE id=? AND deleted_at IS NULL", (alias["target_id"],)
        ).fetchone()
        if row is not None:
            return row

    slug = slugify(name)
    return conn.execute(
        "SELECT * FROM clients WHERE slug=? AND deleted_at IS NULL", (slug,)
    ).fetchone()


def add_alias(conn, alias_kind: str, alias_text: str, target_kind: str, target_id: str,
              *, source: str, confidence: str = "high") -> str:
    """Idempotent on (alias_kind, normalized alias_text). Normalization: strip + lower."""
    device_id = current_device(conn)
    with connection.transaction(conn):
        return _add_alias_in_txn(conn, alias_kind, alias_text, target_kind, target_id,
                                  source=source, confidence=confidence, device_id=device_id)


def _add_alias_in_txn(conn, alias_kind: str, alias_text: str, target_kind: str, target_id: str,
                       *, source: str, confidence: str = "high", device_id: str) -> str:
    """Body of `add_alias`, factored out for callers that already hold an open
    `connection.transaction(conn)` block (e.g. `corrections.move_activity`,
    which must record the alias's audit row in the same transaction as the
    alias write). The caller is responsible for having already resolved
    `device_id` via `current_device(conn)` *before* opening that transaction
    (non-reentrancy, per the module docstring) and for holding the
    transaction open around this call.
    """
    normalized = alias_text.strip().lower()
    now = timeutil.utc_now_iso()
    row = conn.execute(
        "SELECT id FROM aliases WHERE alias_kind=? AND alias_text=? AND deleted_at IS NULL",
        (alias_kind, normalized),
    ).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE aliases SET target_kind=?, target_id=?, confidence=?, source=?, updated_at=? "
            "WHERE id=?",
            (target_kind, target_id, confidence, source, now, row["id"]),
        )
        return row["id"]

    alias_id = ids.new_id()
    conn.execute(
        "INSERT INTO aliases(id, alias_kind, alias_text, target_kind, target_id, confidence, "
        "source, origin_device_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (alias_id, alias_kind, normalized, target_kind, target_id, confidence, source,
         device_id, now, now),
    )
    return alias_id


def resolve_alias(conn, alias_kind: str, alias_text: str):
    """Look up a live alias by (alias_kind, normalized alias_text)."""
    normalized = alias_text.strip().lower()
    return conn.execute(
        "SELECT * FROM aliases WHERE alias_kind=? AND alias_text=? AND deleted_at IS NULL",
        (alias_kind, normalized),
    ).fetchone()


def upsert_repository(conn, root_path: str, *, name=None, default_branch=None,
                       remotes=None, approved=None, hook_mode: bool = False) -> str:
    """Return a live repository's id, matching on `root_path`.

    `name`, `default_branch`, `remotes`, and `approved` are all
    "only-when-supplied" on an existing row: a bare re-call (all left at
    their `None` default) leaves previously-set values — including a
    user's `approved=False` — untouched rather than reverting them to
    freshly-computed defaults. On first insert, `None` falls back to a
    computed default (`name` from the last path segment, `approved=True`,
    `remotes=[]`).

    `hook_mode=True` threads through to `current_device` and
    `connection.transaction` so the SessionStart hook's repository upsert
    (hook allowlist item (b)) gives up on lock contention inside the hook
    budget instead of burning the full non-hook retry schedule. Default
    `False` leaves every other caller exactly as it was.
    """
    device_id = current_device(conn, hook_mode=hook_mode)
    now = timeutil.utc_now_iso()
    with connection.transaction(conn, hook_mode=hook_mode):
        row = conn.execute(
            "SELECT id FROM repositories WHERE root_path=? AND deleted_at IS NULL", (root_path,)
        ).fetchone()
        if row is not None:
            sets = []
            params = []
            if name is not None:
                sets.append("name=?")
                params.append(name)
            if default_branch is not None:
                sets.append("default_branch=?")
                params.append(default_branch)
            if remotes is not None:
                sets.append("remotes_json=?")
                params.append(json.dumps(remotes))
            if approved is not None:
                sets.append("approved=?")
                params.append(int(bool(approved)))
            if sets:
                sets.append("updated_at=?")
                params.append(now)
                params.append(row["id"])
                conn.execute(f"UPDATE repositories SET {', '.join(sets)} WHERE id=?", params)
            return row["id"]

        resolved_name = name if name is not None else (root_path.rstrip("/").rsplit("/", 1)[-1] or root_path)
        resolved_approved = True if approved is None else approved
        remotes_json = json.dumps(remotes if remotes is not None else [])
        repo_id = ids.new_id()
        conn.execute(
            "INSERT INTO repositories(id, root_path, name, default_branch, remotes_json, approved, "
            "origin_device_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (repo_id, root_path, resolved_name, default_branch, remotes_json, int(bool(resolved_approved)),
             device_id, now, now),
        )
        return repo_id


def map_repository(conn, repository_id: str, *, client_id=None, project_id=None,
                    path_prefix: str = "", confidence: str, source: str) -> str:
    """Idempotent on (repository_id, path_prefix).

    `client_id`/`project_id` are only-when-supplied on an existing row (same
    pattern as `upsert_repository`): a re-call that omits one leaves that
    mapping's existing value in place rather than clearing it to NULL.
    `confidence`/`source` have no such ambiguity — they're required on every
    call and always updated.
    """
    device_id = current_device(conn)
    now = timeutil.utc_now_iso()
    with connection.transaction(conn):
        row = conn.execute(
            "SELECT id FROM repository_mappings WHERE repository_id=? AND path_prefix=? "
            "AND deleted_at IS NULL",
            (repository_id, path_prefix),
        ).fetchone()
        if row is not None:
            sets = ["confidence=?", "source=?"]
            params = [confidence, source]
            if client_id is not None:
                sets.append("client_id=?")
                params.append(client_id)
            if project_id is not None:
                sets.append("project_id=?")
                params.append(project_id)
            sets.append("updated_at=?")
            params.append(now)
            params.append(row["id"])
            conn.execute(f"UPDATE repository_mappings SET {', '.join(sets)} WHERE id=?", params)
            return row["id"]

        mapping_id = ids.new_id()
        conn.execute(
            "INSERT INTO repository_mappings(id, repository_id, path_prefix, client_id, project_id, "
            "confidence, source, origin_device_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (mapping_id, repository_id, path_prefix, client_id, project_id, confidence, source,
             device_id, now, now),
        )
        return mapping_id


def _prefix_matches(relative: str, prefix: str) -> bool:
    if prefix == "":
        return True
    return relative == prefix or relative.startswith(prefix + "/")


def resolve_scope_for_path(conn, path: str):
    """Return (client_id, project_id, confidence, reason) for a filesystem path.

    Picks the live, approved repository whose `root_path` is the longest
    prefix of `path`, then among that repository's live mappings the one
    whose `path_prefix` is the longest match of the remaining relative path.
    """
    repos = conn.execute(
        "SELECT id, root_path FROM repositories WHERE deleted_at IS NULL AND approved=1"
    ).fetchall()

    best_repo = None
    best_len = -1
    for repo in repos:
        root = repo["root_path"].rstrip("/")
        if path == root or path.startswith(root + "/"):
            if len(root) > best_len:
                best_len = len(root)
                best_repo = repo

    if best_repo is None:
        return (None, None, "low", "no repository matches this path")

    root = best_repo["root_path"].rstrip("/")
    relative = path[len(root):].lstrip("/")

    mappings = conn.execute(
        "SELECT * FROM repository_mappings WHERE repository_id=? AND deleted_at IS NULL",
        (best_repo["id"],),
    ).fetchall()

    best_mapping = None
    best_prefix_len = -1
    for mapping in mappings:
        prefix = mapping["path_prefix"]
        if _prefix_matches(relative, prefix) and len(prefix) > best_prefix_len:
            best_prefix_len = len(prefix)
            best_mapping = mapping

    if best_mapping is None:
        return (None, None, "low", f"repository {best_repo['root_path']} matched but has no mapping")

    reason = f"repository {best_repo['root_path']} path_prefix {best_mapping['path_prefix']!r}"
    return (best_mapping["client_id"], best_mapping["project_id"], best_mapping["confidence"], reason)


def soft_delete(conn, table: str, row_id: str) -> None:
    """Set `deleted_at` on a row. `table` must be in SOFT_DELETABLE."""
    if table not in SOFT_DELETABLE:
        raise ValueError(f"table {table!r} is not soft-deletable")

    now = timeutil.utc_now_iso()
    with connection.transaction(conn):
        conn.execute(f"UPDATE {table} SET deleted_at=? WHERE id=?", (now, row_id))
