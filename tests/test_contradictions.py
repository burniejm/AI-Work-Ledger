import os, tempfile, time, unittest
from work_ledger.db import connection as dbc, migrations as m
from work_ledger import contradictions as x, entities as e, work_items as wi, remaining as r, ids, timeutil

class TestContradictions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite")); m.apply_all(self.conn)
        now = timeutil.utc_now_iso()
        self.conn.execute("INSERT INTO connections(id,name,adapter,origin_device_id,created_at,updated_at)"
                          " VALUES ('c1','j','jira-rest','d',?,?)", (now, now))
        self.cl = e.upsert_client(self.conn, "Acme")
        self.item = wi.upsert_external(self.conn, "c1", "AB-76", title="Upload recovery", client_id=self.cl)
    def tearDown(self):
        self.tmp.cleanup()

    def _done(self):
        self.conn.execute("UPDATE work_items SET jira_status='Done', jira_status_category='done',"
                          " jira_resolution='Fixed', jira_stale=0 WHERE id=?", (self.item,))

    def test_done_ticket_with_open_remaining_opens_a_contradiction(self):
        r.create(self.conn, title="Retest on hardware", normalized_key="hardware-retest", work_item_id=self.item,
                 confidence="high", source="stage_a", occurred_at="2026-08-29T12:00:00.000Z")
        self._done()
        out = x.evaluate(self.conn)
        self.assertEqual(len(out["opened"]), 1)
        row = x.list_open(self.conn)[0]
        self.assertEqual(row["rule"], "jira_done_open_remaining")
        self.assertIn("Done", row["external_claim"])

    def test_evaluate_is_idempotent(self):
        r.create(self.conn, title="Retest", normalized_key="retest", work_item_id=self.item,
                 confidence="high", source="stage_a", occurred_at="2026-08-29T12:00:00.000Z")
        self._done()
        x.evaluate(self.conn); x.evaluate(self.conn)
        self.assertEqual(len(x.list_open(self.conn)), 1)

    def test_contradiction_resolves_when_the_condition_clears(self):
        rid = r.create(self.conn, title="Retest", normalized_key="retest", work_item_id=self.item,
                       confidence="high", source="stage_a", occurred_at="2026-08-29T12:00:00.000Z")
        self._done()
        x.evaluate(self.conn)
        r.transition(self.conn, rid, "completed", occurred_at="2026-08-30T12:00:00.000Z",
                     source="manual", confidence="high", explanation="finished")
        out = x.evaluate(self.conn)
        self.assertEqual(len(out["resolved"]), 1)
        self.assertEqual(x.list_open(self.conn), [])

    def test_assignee_mismatch(self):
        now = timeutil.utc_now_iso()
        self.conn.execute("INSERT INTO user_identities(id,kind,connection_id,value,origin_device_id,created_at,updated_at)"
                          " VALUES (?,?,?,?,?,?,?)", (ids.new_id(), "jira", "c1", "acct-me", "d", now, now))
        self.conn.execute("UPDATE work_items SET jira_assignee='acct-someone-else', jira_stale=0 WHERE id=?", (self.item,))
        aid = ids.new_id()
        self.conn.execute("INSERT INTO activities(id,work_item_id,kind,summary,started_at,confidence,source,"
                          "idempotency_key,source_hash,origin_device_id,created_at,updated_at)"
                          " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (aid, self.item, "code", "worked on it", now, "high", "stage_a", aid, "h", "d", now, now))
        out = x.evaluate(self.conn)
        self.assertTrue(any(c["rule"] == "assignee_mismatch" for c in x.list_open(self.conn)))

    def test_jira_open_but_work_complete(self):
        now = timeutil.utc_now_iso()
        self.conn.execute("UPDATE work_items SET jira_status='Open', jira_status_category='new', jira_stale=0"
                          " WHERE id=?", (self.item,))
        aid = ids.new_id()
        self.conn.execute("INSERT INTO activities(id,work_item_id,kind,summary,outcome,started_at,confidence,source,"
                          "idempotency_key,source_hash,origin_device_id,created_at,updated_at)"
                          " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (aid, self.item, "code", "finished the fix", "committed", now, "high", "stage_a",
                           aid, "h", "d", now, now))
        x.evaluate(self.conn)
        self.assertTrue(any(c["rule"] == "jira_open_work_complete" for c in x.list_open(self.conn)))

    def test_stale_jira_is_qualified_not_suppressed(self):
        r.create(self.conn, title="Retest", normalized_key="retest", work_item_id=self.item,
                 confidence="high", source="stage_a", occurred_at="2026-08-29T12:00:00.000Z")
        self.conn.execute("UPDATE work_items SET jira_status='Done', jira_status_category='done', jira_stale=1,"
                          " jira_fetched_at='2026-08-01T00:00:00.000Z' WHERE id=?", (self.item,))
        x.evaluate(self.conn)
        row = x.list_open(self.conn)[0]
        self.assertIn("stale", row["external_claim"].lower())

    def test_stale_qualifier_absent_when_fresh_flag_and_never_fetched(self):
        # IMPORTANT 2 regression: jira_stale=0 with jira_fetched_at IS NULL
        # (the brief's own `_done()` fixture) must NOT be qualified as stale
        # -- query.status only treats an absent fetch as stale-relevant when
        # checking *age*, and there is no age to check without a timestamp.
        r.create(self.conn, title="Retest", normalized_key="retest", work_item_id=self.item,
                 confidence="high", source="stage_a", occurred_at="2026-08-29T12:00:00.000Z")
        self._done()  # jira_stale=0, jira_fetched_at left NULL
        x.evaluate(self.conn)
        row = x.list_open(self.conn)[0]
        self.assertNotIn("stale", row["external_claim"].lower())

    def test_stale_qualifier_present_when_flag_set_even_with_recent_fetch(self):
        r.create(self.conn, title="Retest", normalized_key="retest", work_item_id=self.item,
                 confidence="high", source="stage_a", occurred_at="2026-08-29T12:00:00.000Z")
        now = timeutil.utc_now_iso()
        self.conn.execute("UPDATE work_items SET jira_status='Done', jira_status_category='done',"
                          " jira_stale=1, jira_fetched_at=? WHERE id=?", (now, self.item))
        x.evaluate(self.conn)
        row = x.list_open(self.conn)[0]
        self.assertIn("stale", row["external_claim"].lower())

    def test_stale_qualifier_present_when_fetch_is_older_than_threshold(self):
        r.create(self.conn, title="Retest", normalized_key="retest", work_item_id=self.item,
                 confidence="high", source="stage_a", occurred_at="2026-08-29T12:00:00.000Z")
        self.conn.execute("UPDATE work_items SET jira_status='Done', jira_status_category='done',"
                          " jira_stale=0, jira_fetched_at='2020-01-01T00:00:00.000Z' WHERE id=?",
                          (self.item,))
        x.evaluate(self.conn)
        row = x.list_open(self.conn)[0]
        self.assertIn("stale", row["external_claim"].lower())

    def test_reopened_remaining_work_still_contradicts_a_done_ticket(self):
        # Ruling 28: reopened is an open state; this contradiction must still fire.
        rid = r.create(self.conn, title="Retest on hardware", normalized_key="hardware-retest",
                       work_item_id=self.item, confidence="high", source="stage_a",
                       occurred_at="2026-08-29T12:00:00.000Z")
        r.transition(self.conn, rid, "completed", occurred_at="2026-08-30T10:00:00.000Z",
                     source="manual", confidence="high", explanation="done")
        r.transition(self.conn, rid, "reopened", occurred_at="2026-08-30T11:00:00.000Z",
                     source="manual", confidence="high", explanation="regression")
        self._done()
        x.evaluate(self.conn)
        self.assertTrue(any(c["rule"] == "jira_done_open_remaining" for c in x.list_open(self.conn)))

    def test_evaluate_never_writes_to_work_item_jira_fields(self):
        before = dict(self.conn.execute("SELECT * FROM work_items WHERE id=?", (self.item,)).fetchone())
        x.evaluate(self.conn)
        after = dict(self.conn.execute("SELECT * FROM work_items WHERE id=?", (self.item,)).fetchone())
        for k in ("jira_status", "jira_status_category", "jira_resolution", "jira_assignee", "jira_updated_at"):
            self.assertEqual(before[k], after[k])


class TestContradictionsHookMode(unittest.TestCase):
    """Minor #5 (final review): `open_contradiction`/`resolve`/`evaluate`
    must thread `hook_mode` to `db.connection.transaction`, so the
    SessionStart hook path (`jira.refresh.refresh_rest` -> `evaluate`) gives
    up on lock contention within the hook budget rather than the non-hook
    (~28s worst case) one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "t.sqlite")
        setup_conn = dbc.connect(self.db_path)
        m.apply_all(setup_conn)
        now = timeutil.utc_now_iso()
        setup_conn.execute(
            "INSERT INTO connections(id,name,adapter,origin_device_id,created_at,updated_at)"
            " VALUES ('c1','j','jira-rest','d',?,?)", (now, now))
        cl = e.upsert_client(setup_conn, "Acme")
        self.item = wi.upsert_external(setup_conn, "c1", "AB-76", title="Upload recovery", client_id=cl)
        # A live candidate for rule_jira_done_open_remaining, so evaluate()
        # actually reaches a write (not just a no-op scan).
        r.create(setup_conn, title="Retest on hardware", normalized_key="hardware-retest",
                work_item_id=self.item, confidence="high", source="stage_a",
                occurred_at="2026-08-29T12:00:00.000Z")
        setup_conn.execute(
            "UPDATE work_items SET jira_status='Done', jira_status_category='done',"
            " jira_resolution='Fixed', jira_stale=0 WHERE id=?", (self.item,))
        setup_conn.close()
        # Hold the write lock from a separate connection so any BEGIN
        # IMMEDIATE issued by the code under test is guaranteed to contend.
        self.holder = dbc.connect(self.db_path)
        self.holder.execute("BEGIN IMMEDIATE")

    def tearDown(self):
        self.holder.rollback()
        self.tmp.cleanup()

    def test_open_contradiction_hook_mode_gives_up_fast(self):
        conn = dbc.connect(self.db_path, hook_mode=True)
        t0 = time.monotonic()
        with self.assertRaises(dbc.DatabaseBusy):
            x.open_contradiction(conn, rule="jira_done_open_remaining", work_item_id=self.item,
                                 ledger_claim="x", external_claim="y", hook_mode=True)
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_resolve_hook_mode_gives_up_fast(self):
        conn = dbc.connect(self.db_path, hook_mode=True)
        t0 = time.monotonic()
        with self.assertRaises(dbc.DatabaseBusy):
            x.resolve(conn, "some-id", note="x", hook_mode=True)
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_evaluate_threads_hook_mode_through_to_its_own_writes(self):
        conn = dbc.connect(self.db_path, hook_mode=True)
        t0 = time.monotonic()
        with self.assertRaises(dbc.DatabaseBusy):
            x.evaluate(conn, hook_mode=True)
        self.assertLess(time.monotonic() - t0, 1.0)


if __name__ == "__main__":
    unittest.main()
