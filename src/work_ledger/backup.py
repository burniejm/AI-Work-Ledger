"""Backup, export, and import (Spec §14.1, §18, §20.20; Ruling 9, 16, 17).

`create_backup`/`restore` always move data through the sqlite3 backup API
(`sqlite3.Connection.backup()`), never a raw file copy, so a backup taken
mid-write is never a torn/corrupt file.

`EXPORT_TABLES` and `IMPORT_ORDER` are module-level tuples of string
literals only -- every table/column identifier interpolated anywhere in
this module comes from one of these two constants (or from
`PRAGMA table_info(<validated table>)`, which reflects this project's own
schema, never caller input). That is why this file is on Task 38's SQL
guard allowlist (Ruling 9): there is no code path here that builds a SQL
identifier out of user, model, or Jira text, or out of an imported file's
column names.

Credentials are never part of a backup or export (Global Constraints):
`export_json`/`export_csv`/`export_markdown` are the third of the three
redaction choke points (the other two are `capture.events.append_source_event`
and `infer.stage_b.build_bundle`). Every string value that leaves this module
is passed through `redact.redact` as a second line of defense on top of the
structural stripping of `connections.credential_reference` (both on export,
and preserved against clobbering on import -- see `EXPORT_EXCLUDED_COLUMNS`)
and the omission of `settings` rows whose key is on `_CREDENTIAL_SETTING_KEYS`.
"""
import csv
import json
import os
import pathlib
import re
import sqlite3

from work_ledger import paths, redact, timeutil
from work_ledger.db import connection as dbc

# Ruling 16: the explicit export table list, each paired with its real
# primary key. `settings`, `schema_migrations`, and `display_id_counters`
# have no `id` column -- an `id`-keyed upsert would fail on them, which is
# exactly why the key is named here per table rather than assumed.
EXPORT_TABLES = (
    ("schema_migrations",          "version"),      # not id-keyed
    ("settings",                   "key"),          # not id-keyed
    ("display_id_counters",        "scope"),        # not id-keyed
    ("devices",                    "id"),
    ("clients",                    "id"),
    ("projects",                   "id"),
    ("work_items",                 "id"),
    ("work_item_links",            "id"),
    ("aliases",                    "id"),
    ("repositories",               "id"),
    ("repository_mappings",        "id"),
    ("connections",                "id"),
    ("connection_capabilities",    "id"),
    ("user_identities",            "id"),
    ("sessions",                   "id"),
    ("source_events",              "id"),
    ("capture_checkpoints",        "id"),
    ("human_commits",              "id"),
    ("session_tokens",             "id"),
    ("activities",                 "id"),
    ("activity_evidence",          "id"),
    ("remaining_items",            "id"),
    ("remaining_item_transitions", "id"),
    ("decisions",                  "id"),
    ("contradictions",             "id"),
    ("corrections",                "id"),
    ("summary_cache",              "id"),
)

# Ruling 17: the fixed topological insert order. Parents strictly precede
# children so an import into a freshly migrated database with
# `PRAGMA foreign_keys=ON` never violates a foreign key -- ordering is what
# makes that safe, not a pragma toggle (the pragma stays ON throughout).
IMPORT_ORDER = (
    "schema_migrations", "settings", "display_id_counters", "devices",
    "clients", "projects",
    "repositories", "connections",
    "repository_mappings", "connection_capabilities", "user_identities",
    "work_items",                      # -> clients, projects, connections, work_items(self)
    "work_item_links", "aliases",
    "sessions",                        # -> repositories
    "source_events",                   # -> sessions, repositories
    "capture_checkpoints",
    "human_commits",                   # no FKs; hook-captured commits
    "session_tokens",                  # keyed by session_id string; no FK
    "activities",                      # -> sessions, clients, projects, work_items
    "activity_evidence",               # -> activities, source_events
    "remaining_items",                 # -> work_items, clients, projects
    "remaining_item_transitions",      # -> remaining_items, source_events, activities
    "decisions",                       # -> work_items, activities, clients, projects
    "contradictions",                  # -> work_items
    "corrections",                     # -> corrections(self)
    "summary_cache",
)

# Columns nulled out on export, regardless of content -- structural
# stripping, ahead of (not instead of) the per-string redact.redact() pass.
# Import's UPDATE path (see import_json) never writes these columns, so a
# live value already present on the target database survives an import
# rather than being clobbered with the export's null.
EXPORT_EXCLUDED_COLUMNS = {"connections": {"credential_reference"}}

# Explicit denylist of `settings` keys that must never leave a backup or
# export. Currently empty: no credential-bearing setting exists today (the
# one live `settings` row, `device_key`, is an internal identity linking
# key, not a secret -- excluding it would fork device identity on restore,
# since `entities.current_device` mints a fresh one and orphans the
# imported devices row). Kept as a literal module constant -- not a
# key-name/content heuristic -- so a future credential-bearing setting has
# one obvious, auditable place to be added, and so `redact.redact_obj`'s
# generic "key contains 'key'/'token'/..." heuristic (built for free-form
# nested payloads) never silently swallows an ordinary settings row again.
_CREDENTIAL_SETTING_KEYS = frozenset()

# `work_items.merged_into_id` and `corrections.undo_of_correction_id` are
# self-references. On import each table's rows are inserted with this
# column forced to NULL, then a second UPDATE pass (after every row of that
# table exists) restores the real value -- so a row that references a
# sibling inserted later in the same table never violates its own
# not-yet-populated foreign key.
_SELF_REFERENCE_COLUMNS = {
    "work_items": "merged_into_id",
    "corrections": "undo_of_correction_id",
}

# create_backup()'s filename: <compact UTC timestamp>[-label]-work-ledger.sqlite.
# The timestamp has ':'/'-'/'.' stripped so the format is filesystem-safe on
# every platform and unambiguous to parse back out of the filename in
# list_backups() (a label can itself contain '-').
_BACKUP_FILENAME_RE = re.compile(
    r"^(?P<ts>\d{8}T\d{9}Z)(?:-(?P<label>.+))?-work-ledger\.sqlite$"
)

# Minor #6 (final review): `create_backup`'s `label` becomes one path
# segment of a filename it builds itself -- a label containing '/' (easy
# when a caller passes the user's own words verbatim, e.g. `--label "pre/
# post migration"`) would let the backup escape `paths.backup_dir()`
# entirely, or fail with an opaque sqlite "unable to open database file".
# Restricted to the same safe, portable charset a filesystem never
# misinterprets.
_LABEL_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# export_csv() iterates this slice of EXPORT_TABLES -- still only
# module-level string literals, never a second independent list.
_CSV_TABLE_NAMES = ("activities", "remaining_items", "work_items", "decisions", "contradictions")
_CSV_TABLES = tuple((table, pk) for table, pk in EXPORT_TABLES if table in _CSV_TABLE_NAMES)


class _DryRunAbort(Exception):
    """Internal sentinel: unwinds the import transaction without committing."""


def _table_columns(conn: sqlite3.Connection, table: str) -> "list[str]":
    """Live column names for `table`, straight from the schema -- never from
    caller/import-file input."""
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def _redact_value(value):
    if isinstance(value, str):
        value, _ = redact.redact(value)
    return value


def _redact_row(row: dict) -> dict:
    return {key: _redact_value(value) for key, value in row.items()}


def create_backup(conn: sqlite3.Connection, *, label: "str | None" = None) -> pathlib.Path:
    """Snapshot the live database via `sqlite3.Connection.backup()` -- never a
    file copy -- into `backups/<UTC timestamp>[-label]-work-ledger.sqlite`,
    mode 0600.

    `label`, when given, must match `[A-Za-z0-9._-]+` -- it becomes a path
    segment of the destination filename, so anything else (a `/` above all)
    is refused with `ValueError` before any file is touched, rather than
    silently escaping `paths.backup_dir()` or failing with an opaque sqlite
    "unable to open database file" (Minor #6, final review).
    """
    if label is not None and not _LABEL_RE.fullmatch(label):
        raise ValueError(
            f"backup label {label!r} is invalid: it must match [A-Za-z0-9._-]+ "
            "(no '/' or other path/filename-unsafe characters)"
        )
    paths.ensure_dirs()
    compact_ts = re.sub(r"[-:.]", "", timeutil.utc_now_iso())
    name_parts = [compact_ts]
    if label:
        name_parts.append(label)
    name_parts.append("work-ledger.sqlite")
    dest = paths.backup_dir() / "-".join(name_parts)

    dest_conn = sqlite3.connect(str(dest))
    try:
        conn.backup(dest_conn)
    finally:
        dest_conn.close()
    os.chmod(dest, 0o600)
    return dest


def list_backups() -> "list[dict]":
    """List backups found in `paths.backup_dir()`, oldest first."""
    backup_dir = paths.backup_dir()
    if not backup_dir.is_dir():
        return []
    out = []
    for entry in backup_dir.iterdir():
        match = _BACKUP_FILENAME_RE.match(entry.name)
        if not match:
            continue
        out.append({
            "path": entry,
            "filename": entry.name,
            "timestamp": match.group("ts"),
            "label": match.group("label"),
            "size_bytes": entry.stat().st_size,
        })
    out.sort(key=lambda row: row["timestamp"])
    return out


def restore(backup_path, *, confirm: bool) -> None:
    """Replace the live database with `backup_path`'s contents.

    A safety backup of the current database is taken first (also via the
    backup API). Refuses without `confirm=True`.
    """
    if not confirm:
        raise ValueError("restore requires confirm=True to replace the live database")

    backup_path = pathlib.Path(backup_path)
    if not backup_path.exists():
        raise FileNotFoundError(f"backup not found: {backup_path}")

    current_path = paths.db_path()
    if current_path.exists():
        safety_conn = dbc.connect(str(current_path))
        try:
            create_backup(safety_conn, label="pre-restore")
        finally:
            safety_conn.close()

    src_conn = sqlite3.connect(str(backup_path))
    try:
        dest_conn = dbc.connect(str(current_path))
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()
    os.chmod(current_path, 0o600)


def export_json(conn: sqlite3.Connection, out_path) -> dict:
    """Write `{"format":"work-ledger-export","version":1,"exported_at":...,
    "tables":{...}}` to `out_path` and return the same dict.

    Iterates `EXPORT_TABLES` -- never `sqlite_master` -- omits `settings`
    rows whose key is on `_CREDENTIAL_SETTING_KEYS`, nulls
    `EXPORT_EXCLUDED_COLUMNS`, and passes every remaining string value
    through `redact.redact` as a second line of defense.
    """
    tables: dict = {}
    for table, pk in EXPORT_TABLES:
        excluded = EXPORT_EXCLUDED_COLUMNS.get(table, frozenset())
        rows_out = []
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY {pk}"):
            record = dict(row)
            if table == "settings" and record["key"] in _CREDENTIAL_SETTING_KEYS:
                continue
            for col in excluded:
                record[col] = None
            rows_out.append(_redact_row(record))
        tables[table] = rows_out

    data = {
        "format": "work-ledger-export",
        "version": 1,
        "exported_at": timeutil.utc_now_iso(),
        "tables": tables,
    }
    out_path = pathlib.Path(out_path)
    out_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(out_path, 0o600)
    return data


def _work_item_label(external_key, display_id, title) -> str:
    label = external_key or display_id
    if label and title:
        return f"[{label}] {title}"
    if label:
        return f"[{label}]"
    return title or "Unassigned"


def export_markdown(conn: sqlite3.Connection, out_path, *, tz: str, rollover_hour: int) -> None:
    """Human-readable history grouped by workday -> client -> project -> work item.

    Includes every work item's opening (dated by `created_at`) as well as
    logged activities, so a ticket that has been captured but has no
    activity against it yet still appears in the export rather than being
    silently dropped.
    """
    entries = []  # (workday, client_name, project_name, work_item_key, line)

    for row in conn.execute(
        "SELECT wi.*, c.name AS client_name, p.name AS project_name "
        "FROM work_items wi "
        "LEFT JOIN clients c ON c.id = wi.client_id "
        "LEFT JOIN projects p ON p.id = wi.project_id "
        "WHERE wi.deleted_at IS NULL "
        "ORDER BY wi.created_at"
    ):
        workday = timeutil.workday_of(row["created_at"], tz, rollover_hour)
        title, _ = redact.redact(row["title"] or "")
        entries.append((
            workday, row["client_name"] or "Unassigned", row["project_name"] or "Unassigned",
            _work_item_label(row["external_key"], row["display_id"], row["title"]),
            f"- opened: {title}",
        ))

    for row in conn.execute(
        "SELECT a.*, c.name AS client_name, p.name AS project_name, "
        "wi.external_key AS wi_external_key, wi.display_id AS wi_display_id, wi.title AS wi_title "
        "FROM activities a "
        "LEFT JOIN clients c ON c.id = a.client_id "
        "LEFT JOIN projects p ON p.id = a.project_id "
        "LEFT JOIN work_items wi ON wi.id = a.work_item_id "
        "WHERE a.deleted_at IS NULL "
        "ORDER BY a.started_at"
    ):
        workday = timeutil.workday_of(row["started_at"], tz, rollover_hour)
        summary, _ = redact.redact(row["summary"] or "")
        line = f"- {summary}"
        if row["outcome"]:
            outcome, _ = redact.redact(row["outcome"])
            line += f" ({outcome})"
        entries.append((
            workday, row["client_name"] or "Unassigned", row["project_name"] or "Unassigned",
            _work_item_label(row["wi_external_key"], row["wi_display_id"], row["wi_title"]),
            line,
        ))

    tree: dict = {}
    for workday, client_name, project_name, work_item_key, line in entries:
        (tree.setdefault(workday, {})
             .setdefault(client_name, {})
             .setdefault(project_name, {})
             .setdefault(work_item_key, [])
             .append(line))

    lines = ["# Work Ledger History", ""]
    for workday in sorted(tree):
        lines.append(f"## {workday}")
        lines.append("")
        for client_name in sorted(tree[workday]):
            for project_name in sorted(tree[workday][client_name]):
                for work_item_key in sorted(tree[workday][client_name][project_name]):
                    lines.append(f"### {client_name} / {project_name} / {work_item_key}")
                    lines.extend(tree[workday][client_name][project_name][work_item_key])
                    lines.append("")
        lines.append("")

    out_path = pathlib.Path(out_path)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    os.chmod(out_path, 0o600)


def export_csv(conn: sqlite3.Connection, out_dir) -> "list[pathlib.Path]":
    """One CSV per table for activities, remaining_items, work_items,
    decisions, contradictions -- a slice of `EXPORT_TABLES`."""
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(out_dir, 0o700)

    written = []
    for table, pk in _CSV_TABLES:
        cols = _table_columns(conn, table)
        out_path = out_dir / f"{table}.csv"
        with out_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(cols)
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY {pk}"):
                writer.writerow([_redact_value(row[col]) for col in cols])
        os.chmod(out_path, 0o600)
        written.append(out_path)
    return written


def _row_differs(existing_row: sqlite3.Row, incoming: dict, cols: "list[str]", *, exclude: set) -> bool:
    for col in cols:
        if col in exclude:
            continue
        if existing_row[col] != incoming[col]:
            return True
    return False


def import_json(conn: sqlite3.Connection, in_path, *, dry_run: bool = False) -> dict:
    """Walk `IMPORT_ORDER` and upsert each table on its own primary key from
    `EXPORT_TABLES`.

    Rows whose primary key already exists are updated only when the
    incoming `updated_at` is newer, otherwise skipped and counted. Tables
    with no `updated_at` column (schema_migrations, source_events,
    capture_checkpoints, remaining_item_transitions, corrections,
    activity_evidence, summary_cache -- the append-only/cache tables) are
    insert-only: an existing key is always skipped, never overwritten.
    `source_events` is additionally deduped by `idempotency_key`. The whole
    import runs inside a single `BEGIN IMMEDIATE`; `PRAGMA foreign_keys`
    stays ON throughout (Ruling 17 -- IMPORT_ORDER is what makes that safe).
    """
    assert set(IMPORT_ORDER) == {table for table, _ in EXPORT_TABLES}, \
        "IMPORT_ORDER must exactly match EXPORT_TABLES (Ruling 17)"

    data = json.loads(pathlib.Path(in_path).read_text(encoding="utf-8"))
    tables_in = data.get("tables", {})
    pk_by_table = dict(EXPORT_TABLES)

    stats = {"inserted": 0, "updated": 0, "skipped": 0, "conflicts": []}
    deferred_self_ref: dict = {}

    def run() -> None:
        for table in IMPORT_ORDER:
            rows = tables_in.get(table) or []
            if not rows:
                continue

            pk = pk_by_table[table]
            cols = _table_columns(conn, table)
            has_updated_at = "updated_at" in cols
            self_ref_col = _SELF_REFERENCE_COLUMNS.get(table)
            dedupe_col = "idempotency_key" if table == "source_events" else None
            # Never written by the UPDATE path -- a live value (e.g. a
            # connections.credential_reference re-established since the
            # export was taken) survives an import instead of being
            # clobbered with the export's null (Important B).
            preserved_cols = EXPORT_EXCLUDED_COLUMNS.get(table, frozenset())

            for raw_row in rows:
                row = {col: raw_row.get(col) for col in cols}

                self_ref_value = None
                if self_ref_col is not None and row.get(self_ref_col) is not None:
                    self_ref_value = row[self_ref_col]
                    row[self_ref_col] = None

                if dedupe_col is not None:
                    already = conn.execute(
                        f"SELECT 1 FROM {table} WHERE {dedupe_col}=?", (row[dedupe_col],)
                    ).fetchone()
                    if already is not None:
                        stats["skipped"] += 1
                        continue

                pk_value = row.get(pk)
                existing = conn.execute(f"SELECT * FROM {table} WHERE {pk}=?", (pk_value,)).fetchone()
                exclude = set(preserved_cols)
                if self_ref_col:
                    exclude.add(self_ref_col)

                # Tracks whether this row's data was actually written this
                # pass -- the deferred self-reference update below must only
                # ever apply to a row that was just inserted or updated
                # (Critical fix): applying it to a row that was skipped as
                # stale (newer live `updated_at`) or skipped as
                # already-present-in-an-insert-only/append-only table would
                # silently overwrite live data (or mutate a row that must
                # never change post-insert) despite the import otherwise
                # leaving that row alone.
                wrote_row = False

                if existing is None:
                    placeholders = ",".join("?" for _ in cols)
                    conn.execute(
                        f"INSERT INTO {table}({','.join(cols)}) VALUES ({placeholders})",
                        [row[col] for col in cols],
                    )
                    stats["inserted"] += 1
                    wrote_row = True
                elif has_updated_at and (row.get("updated_at") or "") > (existing["updated_at"] or ""):
                    update_cols = [col for col in cols if col != pk and col not in preserved_cols]
                    sets = ",".join(f"{col}=?" for col in update_cols)
                    values = [row[col] for col in update_cols] + [pk_value]
                    conn.execute(f"UPDATE {table} SET {sets} WHERE {pk}=?", values)
                    stats["updated"] += 1
                    wrote_row = True
                else:
                    if _row_differs(existing, row, cols, exclude=exclude):
                        stats["conflicts"].append({"table": table, "pk": pk_value})
                    stats["skipped"] += 1

                if wrote_row and self_ref_col is not None and self_ref_value is not None:
                    deferred_self_ref.setdefault(table, []).append((pk_value, self_ref_value))

        for table, updates in deferred_self_ref.items():
            pk = pk_by_table[table]
            self_ref_col = _SELF_REFERENCE_COLUMNS[table]
            for pk_value, self_ref_value in updates:
                conn.execute(
                    f"UPDATE {table} SET {self_ref_col}=? WHERE {pk}=?",
                    (self_ref_value, pk_value),
                )

    try:
        with dbc.transaction(conn):
            run()
            if dry_run:
                raise _DryRunAbort()
    except _DryRunAbort:
        pass

    return stats
