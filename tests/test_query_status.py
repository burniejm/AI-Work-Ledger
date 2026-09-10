import os
import tempfile
import unittest

from work_ledger.db import connection as dbc, migrations as m
from work_ledger.query import status
from work_ledger import entities as e, work_items as wi, remaining as r

TZ, ROLL = "America/Chicago", 3


class TestStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite"))
        m.apply_all(self.conn)
        self.cl = e.upsert_client(self.conn, "Acme")
        self.pr = e.upsert_project(self.conn, self.cl, "Replay")
        self.item = wi.upsert_external(self.conn, "conn1", "AB-76", title="Upload recovery",
                                        client_id=self.cl, project_id=self.pr)
        r.create(self.conn, title="Retest on hardware", normalized_key="hardware-retest",
                  work_item_id=self.item, client_id=self.cl, project_id=self.pr,
                  confidence="high", source="stage_a", occurred_at="2026-08-29T20:00:00.000Z")

    def tearDown(self):
        self.tmp.cleanup()

    def test_scope_resolves_ticket_key_client_and_alias(self):
        self.assertEqual(status.resolve_scope(self.conn, "AB-76")["kind"], "work_item")
        self.assertEqual(status.resolve_scope(self.conn, "Acme")["kind"], "client")
        e.add_alias(self.conn, "client", "SC", "client", self.cl, source="manual")
        self.assertEqual(status.resolve_scope(self.conn, "SC")["id"], self.cl)
        with self.assertRaises(status.ScopeNotFound):
            status.resolve_scope(self.conn, "Nonexistent Corp")

    def test_status_includes_open_work_and_jira(self):
        self.conn.execute("UPDATE work_items SET jira_status='In Progress', jira_status_category='indeterminate',"
                           " jira_fetched_at='2026-08-30T10:00:00.000Z', jira_stale=0 WHERE id=?", (self.item,))
        rep = status.status_report(self.conn, "AB-76", tz=TZ, rollover_hour=ROLL)
        self.assertEqual(len(rep["open_work"]), 1)
        self.assertEqual(rep["jira"][0]["status"], "In Progress")
        self.assertFalse(rep["jira"][0]["stale"])

    def test_status_marks_stale_jira(self):
        rep = status.status_report(self.conn, "AB-76", tz=TZ, rollover_hour=ROLL)
        self.assertTrue(rep["jira"][0]["stale"])
        self.assertTrue(any("stale" in n.lower() for n in rep["notes"]))

    def test_status_default_window_is_seven_workdays(self):
        rep = status.status_report(self.conn, "Acme", tz=TZ, rollover_hour=ROLL)
        self.assertEqual(rep["recent"]["kind"], "range")
        self.assertEqual(rep["window_workdays"], 7)
        self.assertIsInstance(rep["notes"], list)

    def test_reopened_remaining_work_still_shows_as_open(self):
        # Ruling 28: reopened is an open state; it must not vanish from `status`.
        item = self.conn.execute("SELECT id FROM remaining_items").fetchone()["id"]
        r.transition(self.conn, item, "completed", occurred_at="2026-08-30T12:00:00.000Z",
                     source="manual", confidence="high", explanation="done")
        r.transition(self.conn, item, "reopened", occurred_at="2026-08-30T13:00:00.000Z",
                     source="manual", confidence="high", explanation="regression")
        rep = status.status_report(self.conn, "AB-76", tz=TZ, rollover_hour=ROLL)
        self.assertEqual([i["state"] for i in rep["open_work"]], ["reopened"])


if __name__ == "__main__":
    unittest.main()
