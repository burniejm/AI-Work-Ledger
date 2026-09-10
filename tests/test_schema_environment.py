import os, sqlite3, tempfile, unittest
from work_ledger.db import connection as dbc, migrations as m
from work_ledger import ids, timeutil

class TestEnvironmentSchema(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite"))
        m.apply_all(self.conn)
        self.now = timeutil.utc_now_iso()
    def tearDown(self):
        self.tmp.cleanup()

    def _conn_row(self, name, adapter="jira-rest", health="healthy"):
        self.conn.execute(
            "INSERT INTO connections(id,name,adapter,health_state,origin_device_id,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?)", (ids.new_id(), name, adapter, health, "dev1", self.now, self.now))

    def test_connection_name_unique(self):
        self._conn_row("acme-jira")
        with self.assertRaises(sqlite3.IntegrityError):
            self._conn_row("acme-jira")

    def test_adapter_and_health_checks(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self._conn_row("bad-adapter", adapter="graphql")
        with self.assertRaises(sqlite3.IntegrityError):
            self._conn_row("bad-health", health="probably-fine")

    def test_identity_dedup(self):
        sql = ("INSERT INTO user_identities(id,kind,value,origin_device_id,created_at,updated_at)"
               " VALUES (?,?,?,?,?,?)")
        self.conn.execute(sql, (ids.new_id(), "git", "dev@example.com", "dev1", self.now, self.now))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(sql, (ids.new_id(), "git", "dev@example.com", "dev1", self.now, self.now))

    def test_repository_path_unique(self):
        sql = ("INSERT INTO repositories(id,root_path,name,origin_device_id,created_at,updated_at)"
               " VALUES (?,?,?,?,?,?)")
        self.conn.execute(sql, (ids.new_id(), "/Users/x/repo", "repo", "dev1", self.now, self.now))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(sql, (ids.new_id(), "/Users/x/repo", "repo", "dev1", self.now, self.now))

    def test_capability_unique_per_connection(self):
        self._conn_row("c1")
        cid = self.conn.execute("SELECT id FROM connections WHERE name='c1'").fetchone()["id"]
        sql = ("INSERT INTO connection_capabilities(id,connection_id,capability,supported,checked_at,created_at,updated_at)"
               " VALUES (?,?,?,?,?,?,?)")
        self.conn.execute(sql, (ids.new_id(), cid, "get_issue", 1, self.now, self.now, self.now))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(sql, (ids.new_id(), cid, "get_issue", 1, self.now, self.now, self.now))

    def _conn_row_with_auth(self, name, auth_method):
        self.conn.execute(
            "INSERT INTO connections(id,name,adapter,health_state,auth_method,origin_device_id,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (ids.new_id(), name, "jira-rest", "healthy", auth_method, "dev1", self.now, self.now))

    def test_auth_method_check_rejects_invalid_value(self):
        # Regression: CHECK (auth_method IN ('mcp-oauth','cloud-api-token','pat',NULL)) is a
        # no-op -- IN with a NULL in the list evaluates to NULL (= satisfied) for any string not
        # in the list, so 'oauth2' would silently succeed. The fixed CHECK is
        # (auth_method IS NULL OR auth_method IN (...)).
        with self.assertRaises(sqlite3.IntegrityError):
            self._conn_row_with_auth("bad-auth", "oauth2")

    def test_auth_method_check_accepts_null_and_each_valid_value(self):
        self._conn_row_with_auth("auth-null", None)
        for i, value in enumerate(("mcp-oauth", "cloud-api-token", "pat")):
            self._conn_row_with_auth(f"auth-{i}", value)
