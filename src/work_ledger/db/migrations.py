"""Forward-only SQLite migration runner.

Reads ``NNN_name.sql`` files from ``db/sql/``, applies each one inside a
single ``BEGIN IMMEDIATE`` transaction together with its
``schema_migrations`` row, and refuses to touch the database if it looks
newer than this build (a migration version beyond ``SCHEMA_VERSION`` is
already applied) or if an already-applied migration's checksum no longer
matches its file on disk.

``schema_migrations`` itself is created by this module (not by a
migration file) to avoid a bootstrap chicken-and-egg problem; migration
001 may assume the table already exists.
"""
import hashlib
import pathlib
import re
import sqlite3

from work_ledger import SCHEMA_VERSION, timeutil
from work_ledger.db import connection

_SQL_DIR = pathlib.Path(__file__).parent / "sql"
_FILENAME_RE = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")

_CREATE_SCHEMA_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


class NewerDatabase(Exception):
    """Raised when the database has a migration applied beyond this build's SCHEMA_VERSION."""


class ChecksumMismatch(Exception):
    """Raised when an already-applied migration's stored checksum no longer matches its file."""


def discover() -> "list[tuple[int, str, str]]":
    """Read db/sql/NNN_name.sql files, returning (version, name, sql_text) sorted by version.

    Raises ValueError if two files claim the same version number.
    """
    found = []
    seen_by_version = {}
    if _SQL_DIR.is_dir():
        for path in _SQL_DIR.iterdir():
            match = _FILENAME_RE.match(path.name)
            if not match:
                continue
            version = int(match.group(1))
            name = match.group(2)
            if version in seen_by_version:
                raise ValueError(
                    f"duplicate migration version {version}: "
                    f"{seen_by_version[version]!r} and {path.name!r}"
                )
            seen_by_version[version] = path.name
            sql_text = path.read_text(encoding="utf-8")
            found.append((version, name, sql_text))
    found.sort(key=lambda item: item[0])
    return found


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE_SCHEMA_MIGRATIONS)


def applied(conn: sqlite3.Connection) -> "dict[int, sqlite3.Row]":
    """Return {version: row} for every migration already recorded as applied."""
    _ensure_table(conn)
    rows = conn.execute("SELECT * FROM schema_migrations").fetchall()
    return {row["version"]: row for row in rows}


def pending(conn: sqlite3.Connection) -> "list[tuple[int, str, str]]":
    """Discovered migrations not yet recorded as applied, sorted by version."""
    done = applied(conn)
    return [(version, name, sql) for (version, name, sql) in discover() if version not in done]


def _split_statements(sql: str) -> "list[str]":
    """Split a SQL script on ';' outside single-quoted strings and comments.

    Tracks single-quoted strings (with '' escaping), '--' line comments, and
    '/* */' block comments so a ';' inside any of those is not treated as a
    statement terminator. This project's migrations never need it, but a
    naive str.split(';') would be silently wrong the day one does.
    """
    statements = []
    buf = []
    i = 0
    n = len(sql)
    in_single = False
    in_line_comment = False
    in_block_comment = False
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append(nxt)
                i += 2
                in_block_comment = False
                continue
            i += 1
            continue

        if in_single:
            buf.append(ch)
            if ch == "'":
                if nxt == "'":
                    buf.append(nxt)
                    i += 2
                    continue
                in_single = False
            i += 1
            continue

        if ch == "'":
            in_single = True
            buf.append(ch)
            i += 1
            continue
        if ch == "-" and nxt == "-":
            in_line_comment = True
            buf.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def _apply_one(conn: sqlite3.Connection, version: int, name: str, sql: str) -> bool:
    """Apply one migration file and its schema_migrations row in a single transaction.
    Returns True when this call actually applied the migration, False when a
    racing process's commit already made it a no-op.

    Uses connection.transaction() (BEGIN IMMEDIATE with the standard bounded
    busy retry, guarded commit/rollback) rather than hand-rolled
    BEGIN/COMMIT/ROLLBACK, per the Global Constraints' bounded-retry-for-writes
    rule for every write transaction in this project.

    Re-checks that `version` is still unapplied *after* acquiring the write
    lock. Two processes can both call `apply_all()` against the same
    brand-new database and both read `applied(conn)` before either has
    written anything (Task 38's concurrency hardening test does exactly
    this, 8-way, against `bin/worklog hook`'s own `migrations.ensure_current`
    call path); without this re-check, whichever process's `BEGIN IMMEDIATE`
    loses the race would blindly re-run this migration's `CREATE TABLE ...`
    after the winner already committed it, and fail with "table already
    exists" instead of simply observing the migration is now done.

    When the row already exists, its recorded checksum is verified against
    this call's `sql` before treating it as a safe no-op -- a racing
    process's commit is only a safe substitute for this one's write when it
    applied the *same* migration file; a mismatch here is exactly as
    significant as the one `apply_all()` checks for pre-existing rows before
    this loop even starts, and must not be silently waved through just
    because the row happened to be written moments ago instead of earlier.
    """
    checksum = hashlib.sha256(sql.encode()).hexdigest()
    with connection.transaction(conn):
        row = conn.execute(
            "SELECT checksum FROM schema_migrations WHERE version=?", (version,)
        ).fetchone()
        if row is not None:
            if row["checksum"] != checksum:
                raise ChecksumMismatch(
                    f"migration {version} checksum mismatch: "
                    f"recorded {row['checksum']}, expected {checksum}"
                )
            return False
        for stmt in _split_statements(sql):
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_migrations(version, name, checksum, applied_at) VALUES (?,?,?,?)",
            (version, name, checksum, timeutil.utc_now_iso()),
        )
        return True


def apply_all(conn: sqlite3.Connection) -> "list[int]":
    """Apply every pending migration in ascending version order.

    Ensures schema_migrations exists, then checks for NewerDatabase and
    ChecksumMismatch before applying anything, then applies pending
    versions. Returns the list of versions applied (empty if nothing was
    pending).
    """
    _ensure_table(conn)
    done = applied(conn)
    discovered = discover()

    if done:
        max_applied = max(done)
        if max_applied > SCHEMA_VERSION:
            raise NewerDatabase(
                f"database has migration {max_applied} applied; "
                f"this build only knows migrations up to {SCHEMA_VERSION}"
            )

    discovered_by_version = {version: (name, sql) for (version, name, sql) in discovered}
    for version, row in done.items():
        match = discovered_by_version.get(version)
        if match is None:
            continue  # applied migration no longer ships a file; nothing to verify against
        _, sql = match
        expected = hashlib.sha256(sql.encode()).hexdigest()
        if row["checksum"] != expected:
            raise ChecksumMismatch(
                f"migration {version} checksum mismatch: "
                f"recorded {row['checksum']}, expected {expected}"
            )

    to_apply = [(version, name, sql) for (version, name, sql) in discovered if version not in done]
    applied_versions = []
    for version, name, sql in to_apply:
        if _apply_one(conn, version, name, sql):
            applied_versions.append(version)
    return applied_versions


def ensure_current(conn: sqlite3.Connection) -> None:
    """Apply any pending migrations. No-op when nothing is pending.

    Every entry point calls this before using the database.
    """
    apply_all(conn)
