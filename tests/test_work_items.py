import os, tempfile, unittest
from work_ledger.db import connection as dbc, migrations as m
from work_ledger import contradictions as x, entities as e, work_items as wi

class TestWorkItems(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite"))
        m.apply_all(self.conn)
        self.c = e.upsert_client(self.conn, "Acme")
        self.p = e.upsert_project(self.conn, self.c, "Replay")
    def tearDown(self):
        self.tmp.cleanup()

    def test_display_ids_are_sequential_and_padded(self):
        a = wi.create_local(self.conn, "Diagnose Ubuntu boot image", client_id=self.c, project_id=self.p)
        b = wi.create_local(self.conn, "Replace AMD hardware", client_id=self.c, project_id=self.p)
        get = lambda i: self.conn.execute("SELECT display_id FROM work_items WHERE id=?", (i,)).fetchone()[0]
        self.assertEqual(get(a), "LOCAL-0001")
        self.assertEqual(get(b), "LOCAL-0002")

    def test_external_upsert_is_idempotent(self):
        a = wi.upsert_external(self.conn, "conn1", "AB-76", title="Upload recovery")
        b = wi.upsert_external(self.conn, "conn1", "AB-76", title="Upload recovery (renamed)")
        self.assertEqual(a, b)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_items").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT title FROM work_items").fetchone()[0], "Upload recovery (renamed)")

    def test_reuses_similar_open_local_item(self):
        first, created = wi.get_or_create_local(self.conn, "Diagnose Ubuntu boot image on AMD hardware",
                                                client_id=self.c, project_id=self.p)
        self.assertTrue(created)
        again, created2 = wi.get_or_create_local(self.conn, "Diagnosing the Ubuntu boot image on the AMD hardware",
                                                 client_id=self.c, project_id=self.p)
        self.assertEqual(first, again)
        self.assertFalse(created2)

    def test_does_not_reuse_a_materially_different_item(self):
        first, _ = wi.get_or_create_local(self.conn, "Diagnose Ubuntu boot image", client_id=self.c, project_id=self.p)
        other, created = wi.get_or_create_local(self.conn, "Write the release notes", client_id=self.c, project_id=self.p)
        self.assertNotEqual(first, other)
        self.assertTrue(created)

    def test_does_not_reuse_a_completed_item(self):
        first, _ = wi.get_or_create_local(self.conn, "Diagnose Ubuntu boot image", client_id=self.c, project_id=self.p)
        self.conn.execute("UPDATE work_items SET local_state='completed' WHERE id=?", (first,))
        again, created = wi.get_or_create_local(self.conn, "Diagnose Ubuntu boot image", client_id=self.c, project_id=self.p)
        self.assertNotEqual(first, again)
        self.assertTrue(created)

    def test_merge_preserves_history(self):
        local = wi.create_local(self.conn, "Fix upload recovery", client_id=self.c, project_id=self.p)
        self.conn.execute("INSERT INTO remaining_items(id,work_item_id,title,normalized_key,state,first_seen_at,"
                          "last_seen_at,confidence,source,origin_device_id,created_at,updated_at)"
                          " VALUES ('r1',?, 'retest','retest','open','t','t','high','stage_a','d','t','t')", (local,))
        jira = wi.upsert_external(self.conn, "conn1", "AB-76", title="Upload recovery")
        wi.merge_into(self.conn, local, jira, reason="linked after ticket was filed")
        self.assertEqual(self.conn.execute("SELECT work_item_id FROM remaining_items WHERE id='r1'").fetchone()[0], jira)
        row = self.conn.execute("SELECT merged_into_id, deleted_at FROM work_items WHERE id=?", (local,)).fetchone()
        self.assertEqual(row["merged_into_id"], jira)
        self.assertIsNotNone(row["deleted_at"])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM work_item_links WHERE link_type='merged_into'").fetchone()[0], 1)

    def test_merge_coalesces_a_colliding_open_contradiction(self):
        # Parked finding from Task 13, owned by Task 30: `uq_contradictions_open`
        # is a partial unique index on (rule, work_item_id, subject_id) WHERE
        # status='open'. If source and target both carry an open contradiction
        # with the same (rule, subject_id), repointing the source's row onto
        # the target's work_item_id used to raise IntegrityError. merge_into
        # must resolve the collision instead of crashing.
        local = wi.create_local(self.conn, "Fix upload recovery", client_id=self.c, project_id=self.p)
        jira = wi.upsert_external(self.conn, "conn1", "AB-77", title="Upload recovery")

        source_cid = x.open_contradiction(
            self.conn, rule="jira_done_open_remaining", work_item_id=local,
            ledger_claim="1 remaining item(s) still open", external_claim="Jira status Done",
        )
        target_cid = x.open_contradiction(
            self.conn, rule="jira_done_open_remaining", work_item_id=jira,
            ledger_claim="1 remaining item(s) still open", external_claim="Jira status Done",
        )
        self.assertIsNotNone(source_cid)
        self.assertIsNotNone(target_cid)

        # Would previously raise sqlite3.IntegrityError on the contradictions repoint.
        wi.merge_into(self.conn, local, jira, reason="linked after ticket was filed")

        rows = self.conn.execute(
            "SELECT id, status, work_item_id FROM contradictions WHERE id IN (?,?)",
            (source_cid, target_cid),
        ).fetchall()
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id[source_cid]["status"], "resolved")
        self.assertEqual(by_id[source_cid]["work_item_id"], jira)  # still repointed, just no longer open
        self.assertEqual(by_id[target_cid]["status"], "open")
        self.assertEqual(len(x.list_open(self.conn, work_item_id=jira)), 1)
