import os, sqlite3, tempfile, unittest
from work_ledger.db import connection as dbc, migrations as m
from work_ledger import ids, timeutil

class TestCaptureSchema(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite"))
        m.apply_all(self.conn)
        self.now = timeutil.utc_now_iso()
    def tearDown(self):
        self.tmp.cleanup()

    def _event(self, key):
        self.conn.execute(
            "INSERT INTO source_events(id,session_id,idempotency_key,event_type,occurred_at,ingested_at,"
            "payload_json,payload_bytes,source_hash,origin_device_id,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ids.new_id(), "s1", key, "tool_use", "2026-08-30T12:00:00.000Z", self.now, "{}", 2, "h", "dev1", self.now))

    def test_idempotency_key_unique(self):
        self._event("k1")
        with self.assertRaises(sqlite3.IntegrityError):
            self._event("k1")

    def test_insert_or_ignore_is_the_safe_replay_path(self):
        self._event("k2")
        self.conn.execute(
            "INSERT OR IGNORE INTO source_events(id,session_id,idempotency_key,event_type,occurred_at,ingested_at,"
            "payload_json,payload_bytes,source_hash,origin_device_id,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ids.new_id(), "s1", "k2", "tool_use", "2026-08-30T12:00:00.000Z", self.now, "{}", 2, "h", "dev1", self.now))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM source_events").fetchone()[0], 1)

    def test_source_events_is_append_only_by_shape(self):
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(source_events)")}
        self.assertNotIn("deleted_at", cols)
        self.assertIn("occurred_at", cols)
        self.assertIn("ingested_at", cols)

    def test_session_id_unique(self):
        sql = ("INSERT INTO sessions(id,session_id,started_at,last_seen_at,origin_device_id,created_at,updated_at)"
               " VALUES (?,?,?,?,?,?,?)")
        self.conn.execute(sql, (ids.new_id(), "sess-1", self.now, self.now, "dev1", self.now, self.now))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(sql, (ids.new_id(), "sess-1", self.now, self.now, "dev1", self.now, self.now))

    def test_checkpoint_status_check(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO capture_checkpoints(id,hook_event,checkpoint_key,status,started_at,origin_device_id,created_at)"
                " VALUES (?,?,?,?,?,?,?)", (ids.new_id(), "Stop", "ck1", "meh", self.now, "dev1", self.now))

    def test_git_commit_signal_is_a_documented_event_type(self):
        # Ruling 30(g): DESIGN §4.2 mandates this kind; the DDL comment must list it
        # and the column must accept it (event_type has no CHECK by design).
        self._event("k-signal")
        self.conn.execute(
            "UPDATE source_events SET event_type='git_commit_signal' WHERE idempotency_key='k-signal'")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM source_events WHERE event_type='git_commit_signal'").fetchone()[0], 1)
        ddl = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='source_events'").fetchone()[0]
        self.assertIn("git_commit_signal", ddl)
