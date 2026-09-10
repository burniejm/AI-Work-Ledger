import os, sqlite3, tempfile, unittest
from work_ledger.db import connection as dbc, migrations as m
from work_ledger import ids, timeutil

class TestInterpretationSchema(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite"))
        m.apply_all(self.conn)
        self.now = timeutil.utc_now_iso()
    def tearDown(self):
        self.tmp.cleanup()

    def _remaining(self, key, state="open", work_item=None):
        self.conn.execute(
            "INSERT INTO remaining_items(id,work_item_id,title,normalized_key,state,first_seen_at,last_seen_at,"
            "confidence,source,origin_device_id,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ids.new_id(), work_item, "t", key, state, self.now, self.now, "high", "stage_a", "dev1", self.now, self.now))

    def test_all_eight_states_accepted(self):
        for i, s in enumerate(["open","in_progress","blocked","waiting","completed","superseded","cancelled","reopened"]):
            self._remaining(f"k{i}", state=s)
        with self.assertRaises(sqlite3.IntegrityError):
            self._remaining("kx", state="pending")

    def test_remaining_key_unique_within_scope(self):
        self._remaining("upload-recovery-fix")
        with self.assertRaises(sqlite3.IntegrityError):
            self._remaining("upload-recovery-fix")

    def test_activity_idempotency_and_confidence_check(self):
        sql = ("INSERT INTO activities(id,kind,summary,started_at,confidence,source,idempotency_key,source_hash,"
               "origin_device_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)")
        self.conn.execute(sql, (ids.new_id(),"code","did a thing",self.now,"high","stage_a","idem-1","h","dev1",self.now,self.now))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(sql, (ids.new_id(),"code","again",self.now,"high","stage_a","idem-1","h","dev1",self.now,self.now))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(sql, (ids.new_id(),"code","x",self.now,"quite-sure","stage_a","idem-2","h","dev1",self.now,self.now))

    def test_one_open_contradiction_per_rule_and_subject(self):
        sql = ("INSERT INTO contradictions(id,rule,work_item_id,ledger_claim,external_claim,status,detected_at,"
               "origin_device_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)")
        self.conn.execute(sql, (ids.new_id(),"jira_done_open_remaining",None,"a","b","open",self.now,"dev1",self.now,self.now))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(sql, (ids.new_id(),"jira_done_open_remaining",None,"a","b","open",self.now,"dev1",self.now,self.now))
        # resolving frees the slot
        self.conn.execute("UPDATE contradictions SET status='resolved', resolved_at=?", (self.now,))
        self.conn.execute(sql, (ids.new_id(),"jira_done_open_remaining",None,"a","b","open",self.now,"dev1",self.now,self.now))

    def test_append_only_tables_have_no_deleted_at(self):
        for t in ("remaining_item_transitions", "corrections"):
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({t})")}
            self.assertNotIn("deleted_at", cols)
