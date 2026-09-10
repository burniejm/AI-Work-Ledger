import os, tempfile, unittest
from work_ledger.db import connection as dbc, migrations as m
from work_ledger.query import daily, render
from work_ledger import entities as e, work_items as wi, ids, timeutil

TZ, ROLL = "America/Chicago", 3

class TestDaily(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite"))
        m.apply_all(self.conn)
        self.cl = e.upsert_client(self.conn, "Acme")
        self.pr = e.upsert_project(self.conn, self.cl, "Replay")
        self.wi = wi.upsert_external(self.conn, "conn1", "AB-76", title="Upload recovery",
                                     client_id=self.cl, project_id=self.pr)

    def _activity(self, started, *, confidence="high", inbox=0, summary="did work"):
        aid = ids.new_id(); now = timeutil.utc_now_iso()
        self.conn.execute("INSERT INTO activities(id,work_item_id,client_id,project_id,kind,summary,started_at,"
                          "confidence,source,in_inbox,idempotency_key,source_hash,origin_device_id,created_at,updated_at)"
                          " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (aid, self.wi, self.cl, self.pr, "code", summary, started, confidence,
                           "stage_a", inbox, aid, "h", "dev1", now, now))
        return aid
    def tearDown(self):
        self.tmp.cleanup()

    def test_midnight_crossing_belongs_to_the_starting_workday(self):
        self._activity("2026-08-29T20:00:00.000Z")   # Aug 29 15:00 CDT
        self._activity("2026-08-30T06:30:00.000Z")   # Aug 30 01:30 CDT -> still Aug 29 workday
        rep = daily.day_report(self.conn, "2026-08-29", tz=TZ, rollover_hour=ROLL)
        acts = rep["clients"][0]["projects"][0]["work_items"][0]["activities"]
        self.assertEqual(len(acts), 2)

    def test_after_rollover_belongs_to_the_new_workday(self):
        self._activity("2026-08-30T08:30:00.000Z")   # Aug 30 03:30 CDT
        self.assertEqual(daily.day_report(self.conn, "2026-08-29", tz=TZ, rollover_hour=ROLL)["clients"], [])
        self.assertEqual(len(daily.day_report(self.conn, "2026-08-30", tz=TZ, rollover_hour=ROLL)["clients"]), 1)

    def test_low_confidence_is_excluded_and_counted(self):
        self._activity("2026-08-29T20:00:00.000Z")
        self._activity("2026-08-29T21:00:00.000Z", confidence="low", inbox=1)
        rep = daily.day_report(self.conn, "2026-08-29", tz=TZ, rollover_hour=ROLL)
        self.assertEqual(len(rep["clients"][0]["projects"][0]["work_items"][0]["activities"]), 1)
        self.assertEqual(rep["excluded_low_confidence_count"], 1)

    def test_medium_confidence_is_marked_inferred(self):
        self._activity("2026-08-29T20:00:00.000Z", confidence="medium")
        rep = daily.day_report(self.conn, "2026-08-29", tz=TZ, rollover_hour=ROLL)
        self.assertTrue(rep["clients"][0]["projects"][0]["work_items"][0]["activities"][0]["inferred"])

    def test_evidence_omitted_by_default(self):
        aid = self._activity("2026-08-29T20:00:00.000Z")
        self.conn.execute("INSERT INTO activity_evidence(id,activity_id,evidence_type,ref,origin_device_id,created_at)"
                          " VALUES (?,?,?,?,?,?)", (ids.new_id(), aid, "file", "src/main.go", "dev1", "t"))
        plain = daily.day_report(self.conn, "2026-08-29", tz=TZ, rollover_hour=ROLL)
        self.assertNotIn("evidence", plain["clients"][0]["projects"][0]["work_items"][0]["activities"][0])
        with_ev = daily.day_report(self.conn, "2026-08-29", tz=TZ, rollover_hour=ROLL, include_evidence=True)
        self.assertEqual(with_ev["clients"][0]["projects"][0]["work_items"][0]["activities"][0]["evidence"][0]["ref"],
                         "src/main.go")

    def test_range_report_spans_workdays(self):
        self._activity("2026-08-28T20:00:00.000Z"); self._activity("2026-08-29T20:00:00.000Z")
        rep = daily.range_report(self.conn, "2026-08-28", "2026-08-29", tz=TZ, rollover_hour=ROLL)
        self.assertEqual(rep["kind"], "range")
        self.assertEqual(sum(len(w["activities"]) for c in rep["clients"] for p in c["projects"]
                             for w in p["work_items"]), 2)

    def test_render_text_is_readable_and_has_no_file_paths_by_default(self):
        self._activity("2026-08-29T20:00:00.000Z", summary="Fixed the upload retry loop")
        out = render.render_report(daily.day_report(self.conn, "2026-08-29", tz=TZ, rollover_hour=ROLL), fmt="text")
        self.assertIn("Acme", out); self.assertIn("AB-76", out); self.assertIn("Fixed the upload retry loop", out)
        self.assertNotIn("src/", out)
