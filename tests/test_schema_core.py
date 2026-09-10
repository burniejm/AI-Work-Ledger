import os, sqlite3, tempfile, unittest
from work_ledger.db import connection as dbc, migrations as m
from work_ledger import ids, timeutil

def _client(conn, slug, deleted=None):
    now = timeutil.utc_now_iso()
    conn.execute("INSERT INTO clients(id,name,slug,origin_device_id,created_at,updated_at,deleted_at)"
                 " VALUES (?,?,?,?,?,?,?)", (ids.new_id(), slug, slug, "dev1", now, now, deleted))

class TestCoreSchema(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite"))
        m.apply_all(self.conn)
    def tearDown(self):
        self.tmp.cleanup()

    def test_tables_exist(self):
        names = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"settings","devices","display_id_counters","clients","projects",
                         "work_items","work_item_links","aliases"} <= names)

    def test_client_slug_unique_among_live_rows(self):
        _client(self.conn, "acme")
        with self.assertRaises(sqlite3.IntegrityError):
            _client(self.conn, "acme")

    def test_soft_deleted_client_frees_the_slug(self):
        _client(self.conn, "acme", deleted="2026-08-30T00:00:00.000Z")
        _client(self.conn, "acme")   # must not raise

    def test_external_identity_unique_per_connection(self):
        now = timeutil.utc_now_iso()
        args = lambda: (ids.new_id(), "jira", "conn-1", "AB-76", "t", now, now, "dev1")
        sql = ("INSERT INTO work_items(id,kind,connection_id,external_key,title,created_at,updated_at,origin_device_id)"
               " VALUES (?,?,?,?,?,?,?,?)")
        self.conn.execute(sql, args())
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(sql, args())
        # same key on a DIFFERENT connection is allowed
        self.conn.execute(sql, (ids.new_id(), "jira", "conn-2", "AB-76", "t", now, now, "dev1"))

    def test_orphan_ticket_key_is_unique_when_no_connection_owns_it(self):
        # Ruling 18: uq_work_items_orphan_key guards free-text ticket keys captured
        # by `worklog add` before any connection claims them (Task 16).
        now = timeutil.utc_now_iso()
        sql = ("INSERT INTO work_items(id,kind,connection_id,external_key,title,created_at,updated_at,origin_device_id)"
               " VALUES (?,?,?,?,?,?,?,?)")
        self.conn.execute(sql, (ids.new_id(), "jira", None, "AB-999", "t", now, now, "dev1"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(sql, (ids.new_id(), "jira", None, "AB-999", "t", now, now, "dev1"))
        # a local item with no external_key is unaffected (NULLs are distinct)
        for _ in range(2):
            self.conn.execute("INSERT INTO work_items(id,kind,title,created_at,updated_at,origin_device_id)"
                              " VALUES (?,?,?,?,?,?)", (ids.new_id(), "local", "t", now, now, "dev1"))

    def test_work_item_kind_check(self):
        now = timeutil.utc_now_iso()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO work_items(id,kind,title,created_at,updated_at,origin_device_id)"
                              " VALUES (?,?,?,?,?,?)", (ids.new_id(), "epic", "t", now, now, "dev1"))

    def test_foreign_keys_enforced(self):
        now = timeutil.utc_now_iso()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO projects(id,client_id,name,slug,origin_device_id,created_at,updated_at)"
                              " VALUES (?,?,?,?,?,?,?)", (ids.new_id(), "no-such-client", "p", "p", "dev1", now, now))
