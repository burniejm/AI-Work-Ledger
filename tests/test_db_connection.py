import os, sqlite3, tempfile, threading, unittest
from work_ledger.db import connection as dbc


class _FakeBeginImmediateFailsConnection(sqlite3.Connection):
    """Test double: BEGIN IMMEDIATE always raises a non-busy OperationalError."""

    def execute(self, sql, *args, **kwargs):
        if sql == "BEGIN IMMEDIATE":
            raise sqlite3.OperationalError("no such table: bogus")
        return super().execute(sql, *args, **kwargs)


class _FakeCommitAndRollbackFailConnection(sqlite3.Connection):
    """Test double: both `commit()` and the post-commit-failure `rollback()`
    raise (Minor #4: a disk I/O error can plausibly break both)."""

    def commit(self):
        raise sqlite3.OperationalError("disk I/O error (commit)")

    def rollback(self):
        raise sqlite3.OperationalError("disk I/O error (rollback)")


class TestConnection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.sqlite")
    def tearDown(self):
        self.tmp.cleanup()

    def test_pragmas(self):
        c = dbc.connect(self.db)
        self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        self.assertEqual(c.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(c.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_file_mode_0600(self):
        dbc.connect(self.db).execute("CREATE TABLE t(a)")
        self.assertEqual(oct(os.stat(self.db).st_mode & 0o777), "0o600")

    def test_transaction_rolls_back(self):
        c = dbc.connect(self.db)
        c.execute("CREATE TABLE t(a INTEGER)")
        with self.assertRaises(ValueError):
            with dbc.transaction(c):
                c.execute("INSERT INTO t VALUES (1)")
                raise ValueError("boom")
        self.assertEqual(c.execute("SELECT count(*) FROM t").fetchone()[0], 0)

    def test_hook_mode_gives_up_fast(self):
        holder = dbc.connect(self.db)
        holder.execute("CREATE TABLE t(a INTEGER)")
        holder.execute("BEGIN IMMEDIATE")           # hold the write lock
        other = dbc.connect(self.db, hook_mode=True)
        import time; t0 = time.monotonic()
        with self.assertRaises(dbc.DatabaseBusy):
            with dbc.transaction(other, hook_mode=True):
                other.execute("INSERT INTO t VALUES (1)")
        self.assertLess(time.monotonic() - t0, 2.0)   # bounded, not the 5s busy_timeout
        holder.rollback()

    def test_hook_mode_busy_timeout_is_300ms(self):
        c = dbc.connect(self.db, hook_mode=True)
        self.assertEqual(c.execute("PRAGMA busy_timeout").fetchone()[0], dbc.HOOK_BUSY_TIMEOUT_MS)
        self.assertEqual(dbc.HOOK_BUSY_TIMEOUT_MS, 300)

    def test_commit_failure_rolls_back_and_leaves_connection_usable(self):
        # A deferred foreign-key violation is only detected at COMMIT time,
        # which exercises the guard around conn.commit() in transaction():
        # the failed COMMIT must not leave a dangling write transaction.
        c = dbc.connect(self.db)
        c.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
        c.execute(
            "CREATE TABLE child("
            "id INTEGER PRIMARY KEY, "
            "parent_id INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED"
            ")"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            with dbc.transaction(c):
                c.execute("INSERT INTO child (parent_id) VALUES (999)")
        self.assertFalse(c.in_transaction)
        # The connection must still be usable for a subsequent transaction
        # rather than raising "cannot start a transaction within a transaction".
        with dbc.transaction(c):
            c.execute("INSERT INTO parent (id) VALUES (1)")
        self.assertEqual(c.execute("SELECT count(*) FROM parent").fetchone()[0], 1)

    def test_a_failing_rollback_never_masks_the_original_commit_error(self):
        # Minor #4 (final review): a rollback failure after a failed commit
        # must not replace the commit error -- it's attached as __cause__
        # instead, so it still shows up in a traceback rather than vanishing.
        conn = sqlite3.connect(
            self.db, factory=_FakeCommitAndRollbackFailConnection, isolation_level=None
        )
        conn.execute("CREATE TABLE t(a INTEGER)")
        with self.assertRaises(sqlite3.OperationalError) as ctx:
            with dbc.transaction(conn):
                conn.execute("INSERT INTO t VALUES (1)")
        self.assertIn("commit", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, sqlite3.OperationalError)
        self.assertIn("rollback", str(ctx.exception.__cause__))

    def test_commit_durability_via_second_connection(self):
        c = dbc.connect(self.db)
        c.execute("CREATE TABLE t(a INTEGER)")
        with dbc.transaction(c):
            c.execute("INSERT INTO t VALUES (42)")
        c2 = dbc.connect(self.db)
        self.assertEqual(c2.execute("SELECT a FROM t").fetchone()[0], 42)

    def test_non_hook_retry_gives_databasebusy_with_patched_delays(self):
        holder = dbc.connect(self.db)
        holder.execute("CREATE TABLE t(a INTEGER)")
        holder.execute("BEGIN IMMEDIATE")  # hold the write lock

        orig_timeout, orig_delays = dbc.DEFAULT_BUSY_TIMEOUT_MS, dbc.RETRY_DELAYS_MS
        dbc.DEFAULT_BUSY_TIMEOUT_MS = 50
        dbc.RETRY_DELAYS_MS = (10, 10, 10, 10, 10)
        try:
            other = dbc.connect(self.db)  # picks up the patched busy_timeout
            import time; t0 = time.monotonic()
            with self.assertRaises(dbc.DatabaseBusy):
                with dbc.transaction(other):
                    other.execute("INSERT INTO t VALUES (1)")
            self.assertLess(time.monotonic() - t0, 2.0)
        finally:
            dbc.DEFAULT_BUSY_TIMEOUT_MS, dbc.RETRY_DELAYS_MS = orig_timeout, orig_delays
            holder.rollback()

    def test_non_busy_operational_error_reraises_without_retry(self):
        # A non-busy OperationalError at BEGIN IMMEDIATE (e.g. a genuinely
        # malformed statement, or a read-only database) must propagate
        # immediately, without burning the retry/sleep budget meant for
        # lock contention.
        conn = sqlite3.connect(
            self.db, factory=_FakeBeginImmediateFailsConnection, isolation_level=None
        )
        try:
            import time; t0 = time.monotonic()
            with self.assertRaises(sqlite3.OperationalError) as ctx:
                with dbc.transaction(conn):
                    pass
            self.assertLess(time.monotonic() - t0, 0.5)
            self.assertNotIsInstance(ctx.exception, dbc.DatabaseBusy)
        finally:
            conn.close()
