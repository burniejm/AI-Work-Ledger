import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from work_ledger.db import connection as dbc, migrations as m
from work_ledger.query import scoped
from work_ledger import entities as e, work_items as wi, remaining as r, ids, timeutil


def _iso_days_ago(days: int) -> str:
    # Relative to "now" rather than a hardcoded date, so the staleness
    # threshold in test_stale_report_uses_days_threshold holds regardless
    # of what day this suite actually runs on.
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class TestScoped(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite"))
        m.apply_all(self.conn)
        self.cl = e.upsert_client(self.conn, "Acme")
        self.item = wi.upsert_external(self.conn, "c1", "AB-76", title="Upload recovery", client_id=self.cl)
        self.blocked = r.create(self.conn, title="Need replay hardware", normalized_key="hardware-need-replay",
                                 work_item_id=self.item, client_id=self.cl, confidence="high",
                                 source="stage_a", occurred_at=_iso_days_ago(20))
        r.transition(self.conn, self.blocked, "blocked", occurred_at=_iso_days_ago(19),
                     source="stage_a", confidence="high", explanation="hardware is in the lab")

    def tearDown(self):
        self.tmp.cleanup()

    def test_blocked_report_includes_explanation(self):
        rep = scoped.blocked_report(self.conn)
        self.assertEqual(rep["items"][0]["title"], "Need replay hardware")
        self.assertEqual(rep["items"][0]["explanation"], "hardware is in the lab")

    def test_blocked_across_all_clients_needs_no_scope(self):
        self.assertGreaterEqual(len(scoped.blocked_report(self.conn)["items"]), 1)

    def test_reopened_items_appear_in_the_remaining_report(self):
        # Ruling 28
        from work_ledger import remaining as rr
        self.assertIn("reopened", rr.OPEN_STATES)
        other = rr.create(self.conn, title="Ship the release notes", normalized_key="notes-release-ship",
                           work_item_id=self.item, client_id=self.cl, confidence="high",
                           source="stage_a", occurred_at="2026-08-20T20:00:00.000Z")
        rr.transition(self.conn, other, "completed", occurred_at="2026-08-21T20:00:00.000Z",
                      source="manual", confidence="high", explanation="done")
        rr.transition(self.conn, other, "reopened", occurred_at="2026-08-22T20:00:00.000Z",
                      source="manual", confidence="high", explanation="regression")
        titles = {i["title"] for i in scoped.remaining_report(self.conn)["items"]}
        self.assertIn("Ship the release notes", titles)

    def test_stale_report_uses_days_threshold(self):
        rep = scoped.stale_report(self.conn, days=14, tz="America/Chicago", rollover_hour=3)
        self.assertTrue(any(i["work_item"] == "AB-76" for i in rep["items"]))
        self.assertEqual(scoped.stale_report(self.conn, days=3650, tz="America/Chicago", rollover_hour=3)["items"], [])

    def test_evidence_report_never_returns_file_contents(self):
        aid = ids.new_id(); now = timeutil.utc_now_iso()
        self.conn.execute("INSERT INTO activities(id,work_item_id,kind,summary,started_at,confidence,source,"
                           "idempotency_key,source_hash,origin_device_id,created_at,updated_at)"
                           " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (aid, self.item, "code", "s", now, "high", "stage_a", aid, "h", "d", now, now))
        self.conn.execute("INSERT INTO activity_evidence(id,activity_id,evidence_type,ref,excerpt,origin_device_id,created_at)"
                           " VALUES (?,?,?,?,?,?,?)", (ids.new_id(), aid, "file", "src/upload.go", None, "d", now))
        rep = scoped.evidence_report(self.conn, aid)
        self.assertEqual(rep["evidence"][0]["ref"], "src/upload.go")
        self.assertIsNone(rep["evidence"][0]["excerpt"])

    def test_inbox_report_only_returns_low_confidence(self):
        aid = ids.new_id(); now = timeutil.utc_now_iso()
        self.conn.execute("INSERT INTO activities(id,kind,summary,started_at,confidence,source,in_inbox,"
                           "confidence_reason,idempotency_key,source_hash,origin_device_id,created_at,updated_at)"
                           " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (aid, "code", "unclear work", now, "low", "stage_b", 1,
                            "no repository mapping; ticket key seen only in tool output", aid, "h", "d", now, now))
        rep = scoped.inbox_report(self.conn)
        self.assertEqual(len(rep["activities"]), 1)
        self.assertIn("no repository mapping", rep["activities"][0]["confidence_reason"])


if __name__ == "__main__":
    unittest.main()
