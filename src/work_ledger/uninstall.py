"""`worklog uninstall` -- inventory-first removal with typed confirmation (Ruling 12).

`inventory()` only ever reads: it opens the database read-only-in-spirit
(no writes are issued), counts rows, and lists `connections.
credential_reference` *references* -- never secrets, which never leave the
OS keychain (Global Constraints).

`run()` never deletes anything unless `data=True` **and** the caller passed
the exact `CONFIRMATION_PHRASE` (DESIGN §15's typed-confirmation gate).
`delete_credentials=True` is honored only alongside that same gate (Spec
§14.1) -- it is never implicit, and passing it alone (or with a wrong
phrase) is a silent no-op, verified by `test_credential_deletion_is_never_implicit`.
"""
import shutil
import sqlite3
import subprocess

from work_ledger import credentials, paths
from work_ledger.db import connection as dbc

CONFIRMATION_PHRASE = "delete my work ledger"

# Fixed, module-level allowlist of tables `inventory()` counts rows in --
# never built from user, model, or Jira input (Global Constraints).
_COUNT_TABLES = ("activities", "work_items", "remaining_items", "source_events", "sessions")


def _count_rows(conn, table: str) -> int:
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # nosec: table is from _COUNT_TABLES
    except sqlite3.OperationalError:
        return 0


def _credential_references(conn) -> "list[str]":
    try:
        rows = conn.execute(
            "SELECT credential_reference FROM connections "
            "WHERE credential_reference IS NOT NULL AND deleted_at IS NULL "
            "ORDER BY created_at"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [row["credential_reference"] for row in rows]


def _dir_entry_count(directory) -> int:
    if not directory.is_dir():
        return 0
    return sum(1 for _ in directory.iterdir())


def inventory() -> dict:
    """Read-only summary of everything a real uninstall would remove.
    Deletes nothing."""
    home = paths.home()
    db_path = paths.db_path()
    db_exists = db_path.exists()
    db_bytes = db_path.stat().st_size if db_exists else 0

    counts = {table: 0 for table in _COUNT_TABLES}
    credential_references: "list[str]" = []

    if db_exists:
        conn = dbc.connect(str(db_path))
        try:
            for table in _COUNT_TABLES:
                counts[table] = _count_rows(conn, table)
            credential_references = _credential_references(conn)
        finally:
            conn.close()

    return {
        "home": str(home),
        "database": {"path": str(db_path), "bytes": db_bytes, "exists": db_exists},
        "counts": counts,
        "backups": _dir_entry_count(paths.backup_dir()),
        "exports": _dir_entry_count(paths.export_dir()),
        "logs": _dir_entry_count(paths.log_dir()),
        "credential_references": credential_references,
    }


def _delete_keychain_entry(reference: str) -> "tuple[bool, str]":
    """Removal of a `keychain://` credential reference via the `security`
    CLI (argv list, subprocess shell never enabled; the binary is pinned to
    `credentials._SECURITY_BIN` -- Minor #7, final review -- rather than a
    bare `"security"` resolved off PATH, so this call site and
    `credentials.py`'s own keychain calls can never disagree on subprocess
    hardening). `env://` references have no keychain entry to delete.

    Returns `(removed, detail)`. `removed` is True **only** when `security`
    exits 0 -- a non-zero exit, a missing binary, or a timeout all report
    `removed=False` with a detail explaining why, so a caller never claims a
    deletion that didn't actually happen."""
    if not isinstance(reference, str) or not reference.startswith("keychain://"):
        return False, "not a keychain:// reference"
    rest = reference[len("keychain://"):]
    if "/" not in rest:
        return False, "malformed keychain:// reference"
    service, account = rest.split("/", 1)
    try:
        proc = subprocess.run(
            [credentials._SECURITY_BIN, "delete-generic-password", "-s", service, "-a", account],
            capture_output=True, text=True, timeout=5,
        )
    except FileNotFoundError:
        return False, "the `security` command is not available on this system"
    except subprocess.TimeoutExpired:
        return False, "the `security` command timed out"
    except Exception as exc:
        return False, str(exc)
    if proc.returncode == 0:
        return True, "removed"
    return False, (proc.stderr or proc.stdout or f"security exited {proc.returncode}").strip()


def run(*, data: bool = False, delete_credentials: bool = False, confirmation: "str | None" = None) -> dict:
    """See module docstring for the confirmation/credential-deletion rules."""
    inv = inventory()
    deleted = {"home": False, "credentials": []}

    if not data:
        return {"inventory": inv, "deleted": deleted, "refused": None}

    if confirmation != CONFIRMATION_PHRASE:
        return {"inventory": inv, "deleted": deleted, "refused": "confirmation phrase not given"}

    if delete_credentials:
        for reference in inv["credential_references"]:
            removed, detail = _delete_keychain_entry(reference)
            deleted["credentials"].append({"reference": reference, "removed": removed, "detail": detail})

    home = paths.home()
    if home.exists():
        shutil.rmtree(home)
    deleted["home"] = True

    return {"inventory": inv, "deleted": deleted, "refused": None}
