"""SQLite connection helpers.

Applies the standard pragma set (WAL, foreign keys, busy_timeout, NORMAL
synchronous), a 0600 file mode, and a bounded busy-retry `transaction()`
context manager. Hook callers pass ``hook_mode=True`` to get a much
shorter give-up budget (Ruling 14) instead of waiting out the full
busy_timeout, since hooks must return quickly.
"""
import contextlib
import os
import random
import sqlite3
import time
from typing import Iterator

from work_ledger import paths

# Non-hook connections use the full busy_timeout and retry budget.
DEFAULT_BUSY_TIMEOUT_MS = 5000
RETRY_DELAYS_MS = (50, 100, 200, 400, 800)

# Hook connections must return quickly: a short busy_timeout and a tiny
# retry budget. Worst case: 2 attempts x 300 ms lock wait + 50 ms + 100 ms
# sleep between attempts ~= 750 ms, comfortably under the 2.0 s ceiling.
HOOK_BUSY_TIMEOUT_MS = 300
HOOK_RETRY_DELAYS_MS = (50, 100)


class DatabaseBusy(Exception):
    """Raised when a transaction could not acquire a write lock within its retry budget."""


def _set_wal_mode(conn: sqlite3.Connection, *, hook_mode: bool) -> None:
    """`PRAGMA journal_mode=WAL`, with the same bounded busy-retry schedule
    `transaction()` uses.

    First-time creation of a brand-new database file requires briefly
    switching its journal mode, which needs the same write lock an ordinary
    write does. When several processes race to open the *same* not-yet-
    -existing file at once (Task 38's concurrency hardening test), this can
    raise ``OperationalError: database is locked`` even though the
    connection's own ``timeout=`` (passed to ``sqlite3.connect`` above) is
    already set -- switching journal modes is not always covered by
    SQLite's automatic busy handler the way an ordinary statement is. Once
    a database is already in WAL mode (the overwhelmingly common case: the
    file already exists), this PRAGMA is a fast, lock-free no-op, so the
    retry loop below almost never actually sleeps.
    """
    delays = HOOK_RETRY_DELAYS_MS if hook_mode else RETRY_DELAYS_MS
    last_err = None
    for delay_ms in delays:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            last_err = e
            time.sleep((delay_ms / 1000) * (1 + random.random() * 0.25))
    raise DatabaseBusy(str(last_err)) from last_err


def connect(path: str | None = None, *, hook_mode: bool = False) -> sqlite3.Connection:
    """Open a connection with the standard pragma set and 0600 file mode.

    Autocommit mode (isolation_level=None) is used so transactions are
    always explicit via `transaction()`.
    """
    if path is None:
        paths.ensure_dirs()
        path = str(paths.db_path())

    existed = os.path.exists(path)
    busy_timeout_ms = HOOK_BUSY_TIMEOUT_MS if hook_mode else DEFAULT_BUSY_TIMEOUT_MS

    conn = sqlite3.connect(path, timeout=busy_timeout_ms / 1000, isolation_level=None)
    conn.row_factory = sqlite3.Row
    _set_wal_mode(conn, hook_mode=hook_mode)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    conn.execute("PRAGMA synchronous=NORMAL")

    if not existed and os.path.exists(path):
        os.chmod(path, 0o600)

    return conn


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection, *, hook_mode: bool = False) -> Iterator[sqlite3.Connection]:
    """Context manager: BEGIN IMMEDIATE, commit on success, rollback on exception.

    Retries on "database is locked"/"busy" OperationalErrors using the
    hook or non-hook retry-delay schedule, sleeping (with jitter) between
    attempts. Once the schedule is exhausted, raises DatabaseBusy.

    Non-hook worst case: up to ~28 s if a writer holds the lock through
    every retry (5 attempts x 5000 ms busy_timeout + the retry sleeps);
    accepted for v1 (single-user CLI, contention is rare). hook_mode=True
    bounds this to well under 1 s (see HOOK_BUSY_TIMEOUT_MS).
    """
    delays = HOOK_RETRY_DELAYS_MS if hook_mode else RETRY_DELAYS_MS
    last_err = None
    for delay_ms in delays:
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            last_err = e
            time.sleep((delay_ms / 1000) * (1 + random.random() * 0.25))
    else:
        raise DatabaseBusy(str(last_err)) from last_err

    try:
        yield conn
    except BaseException:
        # Never let a rollback failure mask the original exception from
        # the transaction body.
        try:
            conn.rollback()
        except BaseException:
            pass
        raise
    else:
        try:
            conn.commit()
        except BaseException as commit_err:
            # A failure at COMMIT (e.g. a deferred foreign-key violation,
            # SQLITE_FULL, or a disk I/O error) leaves the transaction
            # active holding the write lock unless we explicitly roll it
            # back. Without this, every later transaction() on this
            # connection would raise "cannot start a transaction within a
            # transaction" and other processes would burn their full
            # retry budgets against a lock nobody is using productively.
            #
            # Minor #4 (final review): the rollback can itself fail (e.g.
            # the same disk I/O error that broke the commit) -- that must
            # never replace the original commit exception. On a clean
            # rollback, the bare `raise` below re-raises `commit_err`
            # unchanged; on a failing one, `commit_err` is still what gets
            # raised, with the rollback failure attached as its `__cause__`
            # so it still shows up in the traceback rather than vanishing.
            try:
                conn.rollback()
            except BaseException as rollback_err:
                raise commit_err from rollback_err
            raise
