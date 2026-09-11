import os, sqlite3, tempfile, unittest
from work_ledger import SCHEMA_VERSION, ids, timeutil
from work_ledger.db import connection as dbc, migrations as m

class TestMigrationUpgrade(unittest.TestCase):
    """Apply 001 only, insert representative rows, then apply the rest, and assert
    every row survives with the same ids and no NOT NULL / FK violation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite"))
        self.now = timeutil.utc_now_iso()
    def tearDown(self):
        self.tmp.cleanup()

    def _apply_up_to(self, version):
        # m._apply_one() alone doesn't create schema_migrations -- in
        # production it is only ever called from apply_all(), which calls
        # m._ensure_table() first. Reaching into the private helper here
        # to simulate a partially-applied database must reproduce that same
        # precondition rather than a state production code never leaves it in.
        m._ensure_table(self.conn)
        applied = []
        for v, name, sql in m.discover():
            if v > version:
                break
            m._apply_one(self.conn, v, name, sql)
            applied.append(v)
        return applied

    def test_incremental_upgrade_preserves_every_row(self):
        self.assertEqual(self._apply_up_to(1), [1])
        self.assertEqual(sorted(m.applied(self.conn)), [1])

        client_id, project_id, item_id = ids.new_id(), ids.new_id(), ids.new_id()
        self.conn.execute(
            "INSERT INTO clients(id,name,slug,origin_device_id,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?)", (client_id, "Acme", "acme", "dev1", self.now, self.now))
        self.conn.execute(
            "INSERT INTO projects(id,client_id,name,slug,origin_device_id,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?)", (project_id, client_id, "Replay", "replay", "dev1",
                                        self.now, self.now))
        self.conn.execute(
            "INSERT INTO work_items(id,kind,client_id,project_id,external_key,title,"
            "created_at,updated_at,origin_device_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (item_id, "jira", client_id, project_id, "AB-76", "Upload recovery",
             self.now, self.now, "dev1"))
        self.conn.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES ('device_key','abc',?)", (self.now,))
        before = [dict(r) for r in self.conn.execute("SELECT * FROM work_items")]

        all_versions = [version for (version, _, _) in m.discover()]
        applied = m.apply_all(self.conn)                       # everything after 001
        self.assertEqual(applied, all_versions[1:])
        self.assertEqual(sorted(m.applied(self.conn)), all_versions)
        self.assertEqual(m.pending(self.conn), [])

        after = [dict(r) for r in self.conn.execute("SELECT * FROM work_items")]
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]["id"], item_id)
        for column, value in before[0].items():
            self.assertEqual(after[0][column], value, f"column {column} changed during upgrade")
        self.assertEqual(self.conn.execute(
            "SELECT id FROM clients").fetchone()[0], client_id)
        self.assertEqual(self.conn.execute(
            "SELECT value FROM settings WHERE key='device_key'").fetchone()[0], "abc")

        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

        # rows from the later migrations can now reference the pre-existing work item
        self.conn.execute(
            "INSERT INTO remaining_items(id,work_item_id,title,normalized_key,state,first_seen_at,"
            "last_seen_at,confidence,source,origin_device_id,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ids.new_id(), item_id, "Retest", "retest", "open", self.now, self.now,
             "high", "stage_a", "dev1", self.now, self.now))
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_schema_version_equals_max_discovered_version(self):
        self.assertEqual(SCHEMA_VERSION, max(v for v, _, _ in m.discover()))
        m.apply_all(self.conn)
        self.assertEqual(SCHEMA_VERSION, max(m.applied(self.conn)))

    def test_upgrade_is_idempotent_and_checksums_are_stable(self):
        self._apply_up_to(1)
        first = dict(self.conn.execute(
            "SELECT version, checksum FROM schema_migrations WHERE version=1").fetchone())
        m.apply_all(self.conn)
        self.assertEqual(m.apply_all(self.conn), [])
        second = dict(self.conn.execute(
            "SELECT version, checksum FROM schema_migrations WHERE version=1").fetchone())
        self.assertEqual(first, second, "re-running migrations rewrote an applied row")

    def test_a_partially_applied_database_is_not_treated_as_newer(self):
        self._apply_up_to(2)
        self.assertNotEqual(m.pending(self.conn), [])
        m.apply_all(self.conn)                                  # must not raise NewerDatabase
        self.assertEqual(sorted(m.applied(self.conn)),
                         [version for (version, _, _) in m.discover()])
