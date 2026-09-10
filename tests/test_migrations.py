import os, pathlib, sqlite3, tempfile, unittest
from unittest import mock

from work_ledger.db import connection as dbc, migrations as m


class TestMigrations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite"))

        # These tests must be green on their own: db/sql/ ships empty until
        # Task 7 lands the real migration 001, so point discover() at a
        # throwaway fixture directory instead of the real (empty) one. This
        # keeps db/sql/ itself empty, as this task's brief requires.
        self.sql_dir = tempfile.TemporaryDirectory()
        (pathlib.Path(self.sql_dir.name) / "001_fixture.sql").write_text(
            "CREATE TABLE fixture_t (a INTEGER);\n", encoding="utf-8"
        )
        self._sql_dir_patch = mock.patch.object(m, "_SQL_DIR", pathlib.Path(self.sql_dir.name))
        self._sql_dir_patch.start()

    def tearDown(self):
        self._sql_dir_patch.stop()
        self.sql_dir.cleanup()
        self.tmp.cleanup()

    def test_applies_all_and_is_idempotent(self):
        first = m.apply_all(self.conn)
        self.assertEqual(first, [1])
        self.assertEqual(first, sorted(first))
        self.assertEqual(m.apply_all(self.conn), [])
        self.assertEqual(m.pending(self.conn), [])

    def test_schema_migrations_records_checksum(self):
        m.apply_all(self.conn)
        row = self.conn.execute("SELECT * FROM schema_migrations ORDER BY version LIMIT 1").fetchone()
        self.assertEqual(len(row["checksum"]), 64)
        self.assertTrue(row["applied_at"].endswith("Z"))

    def test_refuses_newer_database(self):
        m.apply_all(self.conn)
        self.conn.execute("INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES (999,'future','x','2030-01-01T00:00:00.000Z')")
        with self.assertRaises(m.NewerDatabase):
            m.apply_all(self.conn)

    def test_detects_checksum_mismatch(self):
        m.apply_all(self.conn)
        self.conn.execute("UPDATE schema_migrations SET checksum='tampered' WHERE version=1")
        with self.assertRaises(m.ChecksumMismatch):
            m.apply_all(self.conn)

    def test_discover_rejects_duplicate_versions(self):
        (pathlib.Path(self.sql_dir.name) / "001_other.sql").write_text(
            "CREATE TABLE other_t (a INTEGER);\n", encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            m.discover()
