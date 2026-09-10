"""`worklog doctor` — a self-contained health report (Ruling 19).

Every check is a plain function `(conn) -> (state, detail)` where `state` is
one of `"ok"`, `"warn"`, `"fail"`. `run()` wraps every check so a bug in a
check function itself can never crash `doctor` -- an unexpected exception
becomes `("fail", str(exc))`.

Ruling 19 refines that wrapper for the checks whose producers land in later
tasks (`transcripts_readable` -> Task 21, `hooks_installed` -> Task 23,
`git_identity` -> Task 24, `stage_b` -> Task 29, `connections` -> Task 32,
`subagent_capture` -> Tasks 21/25): each of those checks is written
defensively so that an absent producer (an import that fails, a
`hooks/hooks.json` that does not exist yet, an empty `user_identities`
table, `claude` missing from `PATH`) reports `"warn"` with detail
`f"{name}: {NOT_YET_CONFIGURED}"` rather than `"fail"`. `doctor.run()`
therefore degrades but never reports `"unhealthy"` on a fresh install. Later
tasks tighten each of these checks from a blanket `warn` to a real
`ok`/`fail` verdict as their producers land -- `CHECKS` keeps this exact
order and these exact names throughout.

Never prints a secret: every detail string here is built from schema
metadata, counts, and non-secret reference strings (`connections.
credential_reference` values are `keychain://...`/`env://...` references,
never the underlying secret -- Global Constraints).
"""
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from work_ledger import SCHEMA_VERSION, config as config_mod, paths, timeutil
from work_ledger.db import connection as dbc
from work_ledger.db import migrations

NOT_YET_CONFIGURED = "not yet configured/installed"

CHECKS = [
    "data_dir",
    "permissions",
    "synced_filesystem",
    "config",
    "database_writable",
    "schema_version",
    "integrity",
    "wal_mode",
    "hooks_installed",
    "recent_capture",
    "checkpoint_failures",
    "transcripts_readable",
    "git_identity",
    "connections",
    "stage_b",
    "subagent_capture",
    "backups",
]

_SYNCED_MARKERS = ("Dropbox", "iCloud", "Mobile Documents", "Google Drive", "OneDrive")
_NETWORK_FS_TYPES = {
    "nfs", "nfs4", "smbfs", "smb3", "afpfs", "webdav", "cifs", "fuse", "autofs",
    "fuse.sshfs", "9p", "afs", "glusterfs", "ceph",
}


def plugin_root() -> pathlib.Path:
    """The plugin's on-disk root, used to locate `hooks/hooks.json`.

    Honors `CLAUDE_PLUGIN_ROOT` (set by Claude Code when it invokes hooks);
    falls back to the repo root inferred from this file's own location
    (`<root>/src/work_ledger/doctor.py`), which is correct for a checkout
    that has not been installed as a plugin yet.

    Public because `setupwizard.step_hooks` needs the same answer: two
    independent derivations of "where does the plugin live" would eventually
    disagree, and doctor is the natural owner.
    """
    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env_root:
        return pathlib.Path(env_root)
    return pathlib.Path(__file__).resolve().parents[2]


# Retained for this module's own callers; `plugin_root` is the name to use.
_plugin_root = plugin_root


def _macos_fs_type(path: str):
    """`stat -f %T` on macOS. Returns None when the path can't be stat'd
    (doesn't exist yet, etc.) -- that is inconclusive, not a signal."""
    try:
        proc = subprocess.run(
            ["stat", "-f", "%T", path], capture_output=True, text=True, timeout=2,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip().lower()
    except Exception:
        pass
    return None


def _linux_fs_type(path: str):
    """Longest-prefix match of `path` against `/proc/mounts` entries,
    returning that mount's fstype. None when `/proc/mounts` is unreadable
    or nothing matches -- inconclusive, not a signal."""
    try:
        resolved = os.path.realpath(path)
        best_len = -1
        best_fstype = None
        with open("/proc/mounts", "r") as f:
            for line in f:
                fields = line.split()
                if len(fields) < 3:
                    continue
                mount_point, fstype = fields[1], fields[2]
                mp = mount_point.rstrip("/") or "/"
                if resolved == mp or resolved.startswith(mp + "/") or mp == "/":
                    if len(mp) > best_len:
                        best_len = len(mp)
                        best_fstype = fstype
        return best_fstype.lower() if best_fstype else None
    except Exception:
        return None


def _detect_fs_type(path: str) -> "tuple[str | None, bool]":
    """Best-effort filesystem-type detection. Returns `(fs_type, supported)`.

    `fs_type` is None when this *specific path* couldn't be resolved to a
    type (e.g. it doesn't exist yet) -- that's inconclusive and callers
    should not treat it as a finding either way. `supported` is False only
    when this *platform* offers no detection strategy at all (anything
    other than macOS/Linux), which is the "genuinely undetectable" case
    callers should surface as a warning rather than silently reporting ok.
    """
    if sys.platform == "darwin":
        return _macos_fs_type(path), True
    if sys.platform.startswith("linux"):
        return _linux_fs_type(path), True
    return None, False


def check_synced_filesystem(path) -> "tuple[str, str]":
    """`fail` when `path` is inside a known cloud-synced folder (Dropbox,
    iCloud/Mobile Documents, Google Drive, OneDrive) or appears to be on a
    network filesystem -- SQLite over either risks silent corruption.
    `warn` when this platform offers no filesystem-type detection strategy
    at all, rather than silently reporting `ok` for an unverified path."""
    text = str(path)
    parts = pathlib.Path(text).parts
    for marker in _SYNCED_MARKERS:
        if any(marker in part for part in parts):
            return (
                "fail",
                f"{text} is inside a synced folder ({marker}); SQLite over a "
                "synced filesystem risks database corruption -- move work-ledger's "
                "home out of it",
            )
    fs_type, supported = _detect_fs_type(text)
    if fs_type in _NETWORK_FS_TYPES:
        return ("fail", f"{text} appears to be on a network filesystem ({fs_type})")
    if not supported:
        return ("warn", f"{text}: network-fs detection unavailable on this platform")
    return ("ok", text)


def _check_data_dir(conn) -> "tuple[str, str]":
    home = paths.home()
    if not home.exists():
        return ("warn", f"data_dir: {home} does not exist yet (run any worklog command to create it)")
    if not home.is_dir():
        return ("fail", f"{home} exists but is not a directory")
    return ("ok", str(home))


def _check_permissions(conn) -> "tuple[str, str]":
    import stat as stat_mod

    problems = []
    dirs = (
        paths.home(), paths.backup_dir(), paths.log_dir(),
        paths.state_dir(), paths.export_dir(),
    )
    for d in dirs:
        if d.exists():
            mode = stat_mod.S_IMODE(os.stat(d).st_mode)
            if mode & 0o077:
                problems.append(f"{d} is {oct(mode)}, expected 0700")
    for p in (paths.config_path(), paths.db_path()):
        if p.exists():
            mode = stat_mod.S_IMODE(os.stat(p).st_mode)
            if mode & 0o077:
                problems.append(f"{p} is {oct(mode)}, expected 0600")
    if problems:
        return ("warn", "; ".join(problems))
    return ("ok", "data directory and files are private to the owner")


def _check_synced_filesystem(conn) -> "tuple[str, str]":
    return check_synced_filesystem(str(paths.home()))


def _check_config(conn) -> "tuple[str, str]":
    cfg = config_mod.load()
    problems = config_mod.validate(cfg)
    if problems:
        return ("fail", "; ".join(problems))
    return ("ok", "config is valid")


def _check_database_writable(conn) -> "tuple[str, str]":
    if conn is None:
        return ("fail", "no database connection available")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
    except Exception as exc:
        return ("fail", f"database is not writable: {exc}")
    return ("ok", "database accepts write locks")


def _check_schema_version(conn) -> "tuple[str, str]":
    if conn is None:
        return ("fail", "no database connection available")
    try:
        done = migrations.applied(conn)
        pending = migrations.pending(conn)
    except (migrations.NewerDatabase, migrations.ChecksumMismatch) as exc:
        return ("fail", str(exc))
    if pending:
        names = ", ".join(f"{version:03d}_{name}" for version, name, _ in pending)
        return ("warn", f"{len(pending)} pending migration(s): {names}")
    max_applied = max(done) if done else 0
    return ("ok", f"schema at version {max_applied} (build supports up to {SCHEMA_VERSION})")


def _check_integrity(conn) -> "tuple[str, str]":
    if conn is None:
        return ("fail", "no database connection available")
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    except Exception as exc:
        return ("fail", f"integrity_check failed to run: {exc}")
    result = row[0] if row else "unknown"
    if result == "ok":
        return ("ok", "integrity_check: ok")
    return ("fail", f"integrity_check: {result}")


def _check_wal_mode(conn) -> "tuple[str, str]":
    if conn is None:
        return ("fail", "no database connection available")
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
    except Exception as exc:
        return ("fail", f"could not read journal_mode: {exc}")
    mode = (row[0] if row else "").lower()
    if mode == "wal":
        return ("ok", "journal_mode=WAL")
    return ("warn", f"journal_mode={mode}, expected wal")


def check_hooks_installed(conn=None) -> "tuple[str, str]":
    """Ruling 19: `hooks/hooks.json` is created by Task 23. Until then (or
    if it is empty) this reports warn, never fail."""
    hooks_file = _plugin_root() / "hooks" / "hooks.json"
    if not hooks_file.exists():
        return ("warn", f"hooks_installed: {NOT_YET_CONFIGURED}")
    try:
        data = json.loads(hooks_file.read_text())
    except Exception as exc:
        return ("fail", f"{hooks_file} is not valid JSON: {exc}")
    if not data:
        return ("warn", f"hooks_installed: {NOT_YET_CONFIGURED}")
    return ("ok", f"hooks.json present at {hooks_file}")


def _check_hooks_installed(conn) -> "tuple[str, str]":
    return check_hooks_installed(conn)


def _check_recent_capture(conn) -> "tuple[str, str]":
    if conn is None:
        return ("warn", f"recent_capture: {NOT_YET_CONFIGURED}")
    hooks_state, _ = check_hooks_installed(conn)
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=7)
    cutoff = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{cutoff_dt.microsecond // 1000:03d}Z"
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM source_events WHERE occurred_at >= ?", (cutoff,)
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return ("warn", f"recent_capture: {NOT_YET_CONFIGURED}")
    if count > 0:
        return ("ok", f"{count} source_events captured in the last 7 days")
    if hooks_state != "ok":
        return ("ok", "no source_events yet; hooks are not installed")
    return ("warn", "no source_events in the last 7 days despite hooks being installed")


def _check_checkpoint_failures(conn) -> "tuple[str, str]":
    if conn is None:
        return ("warn", f"checkpoint_failures: {NOT_YET_CONFIGURED}")
    try:
        rows = conn.execute(
            "SELECT checkpoint_key, hook_event, detail, created_at FROM capture_checkpoints "
            "WHERE status='failed' ORDER BY created_at DESC LIMIT 5"
        ).fetchall()
    except sqlite3.OperationalError:
        return ("warn", f"checkpoint_failures: {NOT_YET_CONFIGURED}")
    if not rows:
        return ("ok", "no failed checkpoints in recent history")
    detail = "; ".join(
        f"{row['hook_event']}/{row['checkpoint_key']}: {row['detail']}" for row in rows
    )
    return ("warn", detail)


def _check_transcripts_readable(conn) -> "tuple[str, str]":
    try:
        from work_ledger import transcript
    except ImportError:
        return ("warn", f"transcripts_readable: {NOT_YET_CONFIGURED}")
    try:
        root = transcript.projects_root()
    except Exception as exc:
        return ("fail", f"transcript.projects_root() failed: {exc}")
    if not root.exists():
        return ("warn", f"transcripts_readable: {NOT_YET_CONFIGURED}")
    if not os.access(root, os.R_OK):
        return ("fail", f"{root} is not readable")
    return ("ok", str(root))


def _check_git_identity(conn) -> "tuple[str, str]":
    if conn is None:
        return ("warn", f"git_identity: {NOT_YET_CONFIGURED}")
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM user_identities WHERE kind='git' AND deleted_at IS NULL"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return ("warn", f"git_identity: {NOT_YET_CONFIGURED}")
    if count == 0:
        return ("warn", f"git_identity: {NOT_YET_CONFIGURED}")
    return ("ok", f"{count} git identity(ies) configured")


def _check_connections(conn) -> "tuple[str, str]":
    """Ruling 19: tightened now that Task 35's `connections.health_*` rollup
    (kept fresh by `jira.refresh.refresh_all_rest` at session start, and by
    `worklog connections test/repair`) is real data, not a placeholder.

    No configured connections yet still reads as the standard
    not-yet-configured `warn`. Once at least one live connection exists,
    this reports `ok` when every enabled one is `healthy`, else `warn`
    naming the degraded/expired/missing ones -- never `fail`: the Global
    Constraints say a failed Jira connection must never block local
    capture, and `doctor` must not contradict that by treating one as fatal.
    Never prints a credential -- only `name=health_state` pairs.
    """
    if conn is None:
        return ("warn", f"connections: {NOT_YET_CONFIGURED}")
    try:
        from work_ledger import connections as connections_mod
    except ImportError:
        return ("warn", f"connections: {NOT_YET_CONFIGURED}")
    try:
        rows = connections_mod.list_connections(conn, include_disabled=True)
    except sqlite3.OperationalError:
        return ("warn", f"connections: {NOT_YET_CONFIGURED}")
    if not rows:
        return ("warn", f"connections: {NOT_YET_CONFIGURED}")
    unhealthy = [f"{r['name']}={r['health_state']}" for r in rows
                 if r["enabled"] and r["health_state"] != "healthy"]
    if unhealthy:
        return ("warn", "degraded connection(s): " + ", ".join(unhealthy))
    return ("ok", f"{len(rows)} connection(s) configured and healthy")


def _check_stage_b(conn) -> "tuple[str, str]":
    """Ruling 19: tightened from a blanket `warn` now that Task 29 landed the
    producer. Stage B being off is never a `fail` -- Stage A output stands on
    its own (DESIGN §6 override 3), so an absent `claude` or a deliberate
    `stage_b.enabled: false` is reported as a `warn` with the reason."""
    try:
        from work_ledger.infer import stage_b as stage_b_mod
    except ImportError:
        return ("warn", f"stage_b: {NOT_YET_CONFIGURED}")

    if not stage_b_mod.enabled():
        return ("warn", "stage_b.enabled is false; Stage A output stands alone")
    if shutil.which("claude") is None:
        return ("warn", "claude is not on PATH; Stage B cannot run")

    if conn is None:
        return ("ok", "stage_b is enabled and claude is on PATH")
    try:
        row = conn.execute(
            "SELECT status, detail, started_at FROM capture_checkpoints "
            "WHERE hook_event='stage_b' ORDER BY started_at DESC, created_at DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return ("warn", f"stage_b: {NOT_YET_CONFIGURED}")

    if row is None:
        return ("ok", "stage_b is enabled and claude is on PATH; no run recorded yet")
    if row["status"] == "ok":
        return ("ok", f"last stage_b run at {row['started_at']}: {row['detail']}")
    if row["status"] == "skipped":
        # A guard declined -- rate limited, nothing captured, deliberately off.
        # That is Stage B working as designed, not a finding: the rate limiter
        # alone produces one of these on most Stop hooks.
        return ("ok", f"last stage_b pass at {row['started_at']} stood down: {row['detail']}")
    return ("warn", f"last stage_b run at {row['started_at']} was "
                    f"{row['status']}: {row['detail']}")


def _check_subagent_capture(conn) -> "tuple[str, str]":
    detail = "unverified: sidechain transcript capture is best-effort (DESIGN gap G4)"
    if conn is None:
        return ("warn", detail)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM source_events WHERE is_sidechain=1"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return ("warn", detail)
    if count > 0:
        return ("ok", f"{count} sidechain source_events captured")
    return ("warn", detail)


def _check_backups(conn) -> "tuple[str, str]":
    try:
        from work_ledger import backup as backup_mod
        rows = backup_mod.list_backups()
    except Exception as exc:
        return ("warn", f"could not list backups: {exc}")
    if not rows:
        return ("warn", "no backups found yet; run `worklog backup` periodically")
    newest = rows[-1]  # list_backups() returns oldest first
    return ("ok", f"{len(rows)} backup(s); most recent {newest['path']}")


_CHECK_FUNCS = {
    "data_dir": _check_data_dir,
    "permissions": _check_permissions,
    "synced_filesystem": _check_synced_filesystem,
    "config": _check_config,
    "database_writable": _check_database_writable,
    "schema_version": _check_schema_version,
    "integrity": _check_integrity,
    "wal_mode": _check_wal_mode,
    "hooks_installed": _check_hooks_installed,
    "recent_capture": _check_recent_capture,
    "checkpoint_failures": _check_checkpoint_failures,
    "transcripts_readable": _check_transcripts_readable,
    "git_identity": _check_git_identity,
    "connections": _check_connections,
    "stage_b": _check_stage_b,
    "subagent_capture": _check_subagent_capture,
    "backups": _check_backups,
}


def _run_check(name: str, conn) -> "tuple[str, str]":
    fn = _CHECK_FUNCS[name]
    try:
        state, detail = fn(conn)
        return state, detail
    except Exception as exc:  # doctor itself must never crash
        return "fail", str(exc)


def run(conn=None) -> dict:
    """Run every check in `CHECKS` order and roll up an overall verdict.

    When `conn` is omitted, opens (and closes) its own connection against
    the configured database, applying any pending migrations first, so a
    caller with no open connection still gets the full report. If even
    that fails, the DB-dependent checks report `fail` individually rather
    than crashing `run()`.
    """
    owns_conn = False
    active_conn = conn
    if active_conn is None:
        opened_conn = None
        try:
            paths.ensure_dirs()
            opened_conn = dbc.connect()
            migrations.ensure_current(opened_conn)
            active_conn = opened_conn
            owns_conn = True
        except Exception:
            if opened_conn is not None:
                try:
                    opened_conn.close()
                except Exception:
                    pass
            active_conn = None

    try:
        checks = [
            {"name": name, **dict(zip(("state", "detail"), _run_check(name, active_conn)))}
            for name in CHECKS
        ]
    finally:
        if owns_conn and active_conn is not None:
            try:
                active_conn.close()
            except Exception:
                pass

    if any(c["state"] == "fail" for c in checks):
        overall = "unhealthy"
    elif any(c["state"] == "warn" for c in checks):
        overall = "degraded"
    else:
        overall = "healthy"

    return {"overall": overall, "checks": checks, "generated_at": timeutil.utc_now_iso()}
