import os, tempfile, unittest
from work_ledger.db import connection as dbc, migrations as m
from work_ledger import entities as e, remaining as r, work_items as wi

T0, T1, T2 = "2026-08-28T15:00:00.000Z", "2026-08-29T15:00:00.000Z", "2026-08-30T15:00:00.000Z"

class TestRemaining(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite"))
        m.apply_all(self.conn)
        self.item = r.create(self.conn, title="Retest upload recovery", normalized_key="recovery-retest-upload",
                             confidence="high", source="stage_a", occurred_at=T0)
    def tearDown(self):
        self.tmp.cleanup()

    def test_creation_writes_initial_transition(self):
        h = r.history(self.conn, self.item)
        self.assertEqual(len(h), 1)
        self.assertIsNone(h[0]["from_state"])
        self.assertEqual(h[0]["to_state"], "open")

    def test_reinforce_bumps_count_without_transition(self):
        r.reinforce(self.conn, self.item, occurred_at=T1)
        r.reinforce(self.conn, self.item, occurred_at=T2)
        row = self.conn.execute("SELECT mention_count,last_seen_at FROM remaining_items WHERE id=?", (self.item,)).fetchone()
        self.assertEqual(row["mention_count"], 3)
        self.assertEqual(row["last_seen_at"], T2)
        self.assertEqual(len(r.history(self.conn, self.item)), 1)

    def test_transition_records_full_provenance(self):
        r.transition(self.conn, self.item, "blocked", occurred_at=T1, source="stage_b", confidence="medium",
                     explanation="waiting on replay hardware")
        h = r.history(self.conn, self.item)[-1]
        self.assertEqual((h["from_state"], h["to_state"], h["source"], h["confidence"]),
                         ("open", "blocked", "stage_b", "medium"))
        self.assertEqual(h["explanation"], "waiting on replay hardware")

    def test_completion_sets_resolved_at_and_reopen_clears_it(self):
        r.transition(self.conn, self.item, "completed", occurred_at=T1, source="manual",
                     confidence="high", explanation="done Friday")
        self.assertIsNotNone(self.conn.execute("SELECT resolved_at FROM remaining_items WHERE id=?", (self.item,)).fetchone()[0])
        r.transition(self.conn, self.item, "reopened", occurred_at=T2, source="manual",
                     confidence="high", explanation="regression")
        self.assertIsNone(self.conn.execute("SELECT resolved_at FROM remaining_items WHERE id=?", (self.item,)).fetchone()[0])

    def test_illegal_transition_raises(self):
        r.transition(self.conn, self.item, "completed", occurred_at=T1, source="manual", confidence="high", explanation="x")
        with self.assertRaises(r.InvalidTransition):
            r.transition(self.conn, self.item, "blocked", occurred_at=T2, source="manual", confidence="high", explanation="y")

    def test_open_items_excludes_completed_and_inbox(self):
        low = r.create(self.conn, title="Maybe fix logging", normalized_key="fix-logging-maybe",
                       confidence="low", source="stage_b", occurred_at=T1)
        self.conn.execute("UPDATE remaining_items SET in_inbox=1 WHERE id=?", (low,))
        ids_ = {row["id"] for row in r.open_items(self.conn)}
        self.assertEqual(ids_, {self.item})
        self.assertIn(low, {row["id"] for row in r.open_items(self.conn, include_inbox=True)})

    def test_reopened_counts_as_open(self):
        # Ruling 28: a reopened item must not vanish from `remaining`, `blocked` or `status`.
        self.assertIn("reopened", r.OPEN_STATES)
        r.transition(self.conn, self.item, "completed", occurred_at=T1, source="manual",
                     confidence="high", explanation="done")
        self.assertEqual(r.open_items(self.conn), [])
        r.transition(self.conn, self.item, "reopened", occurred_at=T2, source="manual",
                     confidence="high", explanation="regression")
        self.assertEqual({row["id"] for row in r.open_items(self.conn)}, {self.item})

    def test_resolved_at_uses_event_time_not_wall_clock(self):
        # A backdated transition's resolved_at must match its occurred_at
        # (event time), never the wall-clock time it was recorded at.
        r.transition(self.conn, self.item, "completed", occurred_at=T1, source="manual",
                     confidence="high", explanation="done Friday")
        resolved_at = self.conn.execute(
            "SELECT resolved_at FROM remaining_items WHERE id=?", (self.item,)
        ).fetchone()[0]
        self.assertEqual(resolved_at, T1)

    def test_reinforce_lengthens_detail_but_not_with_a_shorter_one(self):
        item = r.create(self.conn, title="Investigate flaky upload", normalized_key="flaky-upload",
                         confidence="high", source="stage_a", occurred_at=T0, detail="short note")
        r.reinforce(self.conn, item, occurred_at=T1, detail="a substantially longer follow-up note")
        row = self.conn.execute("SELECT detail FROM remaining_items WHERE id=?", (item,)).fetchone()
        self.assertEqual(row["detail"], "a substantially longer follow-up note")

        r.reinforce(self.conn, item, occurred_at=T2, detail="tiny")
        row = self.conn.execute("SELECT detail FROM remaining_items WHERE id=?", (item,)).fetchone()
        self.assertEqual(row["detail"], "a substantially longer follow-up note")

    def test_open_items_scope_filters(self):
        client_a = e.upsert_client(self.conn, "Acme")
        client_b = e.upsert_client(self.conn, "Globex")
        work_item_a = wi.create_local(self.conn, "Ship the widget", client_id=client_a, project_id=None)
        scoped_a = r.create(self.conn, title="Fix widget crash", normalized_key="fix-widget-crash",
                             work_item_id=work_item_a, client_id=client_a,
                             confidence="high", source="stage_a", occurred_at=T0)
        scoped_b = r.create(self.conn, title="Fix gizmo crash", normalized_key="fix-gizmo-crash",
                             client_id=client_b, confidence="high", source="stage_a", occurred_at=T0)

        self.assertEqual({row["id"] for row in r.open_items(self.conn, work_item_id=work_item_a)},
                          {scoped_a})
        self.assertEqual({row["id"] for row in r.open_items(self.conn, client_id=client_a)},
                          {scoped_a})
        self.assertEqual({row["id"] for row in r.open_items(self.conn, client_id=client_b)},
                          {scoped_b})
