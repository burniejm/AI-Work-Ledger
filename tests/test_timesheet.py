import json, os, tempfile, unittest

from work_ledger import ids, timeutil, work_items as wi, entities as ent
from work_ledger.db import connection as dbc, migrations as m
from work_ledger.query import timesheet


class _TimesheetCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name
        self.conn = dbc.connect()
        m.apply_all(self.conn)
        now = timeutil.utc_now_iso()
        self.conn.execute(
            "INSERT INTO connections(id,name,adapter,origin_device_id,created_at,updated_at)"
            " VALUES ('c1','j','jira-rest','d',?,?)", (now, now))
        self.acme = ent.upsert_client(self.conn, "Acme")
        self.globex = ent.upsert_client(self.conn, "Globex")
        self.ab76 = wi.upsert_external(self.conn, "c1", "AB-76",
                                       title="Upload recovery", client_id=self.acme)
        self.ab77 = wi.upsert_external(self.conn, "c1", "AB-77",
                                       title="Retry probe", client_id=self.acme)
        self.gx1 = wi.upsert_external(self.conn, "c1", "GX-1",
                                      title="Other client", client_id=self.globex)

    def tearDown(self):
        del os.environ["WORK_LEDGER_HOME"]
        self.tmp.cleanup()

    def _activity(self, item, client_id, started_at, ended_at, summary="worked"):
        now = timeutil.utc_now_iso()
        aid = ids.new_id()
        self.conn.execute(
            "INSERT INTO activities(id,work_item_id,client_id,kind,summary,started_at,ended_at,"
            "confidence,source,idempotency_key,source_hash,origin_device_id,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, item, client_id, "code", summary, started_at, ended_at,
             "high", "stage_a", aid, "h", "d", now, now))
        return aid

    def _seed_two_days(self):
        # Sep 1 (workday): 1h on AB-76, 30m on AB-77. Sep 2: 50m on AB-76.
        self._activity(self.ab76, self.acme,
                       "2026-09-01T14:00:00.000Z", "2026-09-01T15:00:00.000Z",
                       summary="Fixed the retry loop")
        self._activity(self.ab77, self.acme,
                       "2026-09-01T16:00:00.000Z", "2026-09-01T16:30:00.000Z")
        self._activity(self.ab76, self.acme,
                       "2026-09-02T14:00:00.000Z", "2026-09-02T14:50:00.000Z",
                       summary="Hardened the probe")
        # Noise from the other client, same window: must never appear.
        self._activity(self.gx1, self.globex,
                       "2026-09-01T14:00:00.000Z", "2026-09-01T18:00:00.000Z")

    def _report(self, **kw):
        kw.setdefault("tz", "America/Chicago")
        kw.setdefault("rollover_hour", 3)
        return timesheet.timesheet_report(
            self.conn, "Acme", "2026-09-01", "2026-09-02", **kw)


class TestTimesheetReport(_TimesheetCase):
    def test_rows_are_grouped_by_workday_and_ticket(self):
        self._seed_two_days()
        report = self._report()
        keys = [(r["workday"], r["ticket"], r["minutes"]) for r in report["rows"]]
        self.assertEqual(keys, [
            ("2026-09-01", "AB-76", 60),
            ("2026-09-01", "AB-77", 30),
            ("2026-09-02", "AB-76", 50),
        ])
        self.assertEqual(report["client"], "Acme")
        self.assertEqual(report["total_minutes"], 140)
        self.assertEqual(report["day_totals"],
                         {"2026-09-01": 90, "2026-09-02": 50})

    def test_row_summary_comes_from_the_day_activities(self):
        self._seed_two_days()
        rows = self._report()["rows"]
        self.assertIn("Fixed the retry loop", rows[0]["summary"])
        self.assertIn("Hardened the probe", rows[2]["summary"])

    def test_rounding_rounds_each_row_up_and_totals_follow(self):
        self._seed_two_days()
        report = self._report(round_to=15)
        billed = [r["billed_minutes"] for r in report["rows"]]
        self.assertEqual(billed, [60, 30, 60])          # 50 -> 60
        self.assertEqual(report["total_minutes"], 150)
        self.assertEqual(report["day_totals"]["2026-09-02"], 60)

    def test_ai_usage_is_excluded_unless_internal(self):
        self._seed_two_days()
        self.conn.execute(
            "UPDATE work_items SET ai_usage_score=4, ai_usage_confidence='high' WHERE id=?",
            (self.ab76,))
        public = self._report()
        self.assertNotIn("ai_usage", public["rows"][0])
        self.assertNotIn("ai_usage", json.dumps(public))
        internal = self._report(internal=True)
        self.assertEqual(internal["rows"][0]["ai_usage"]["score"], 4)

    def test_unknown_client_raises_a_helpful_error(self):
        with self.assertRaises(Exception):
            timesheet.timesheet_report(
                self.conn, "Initech", "2026-09-01", "2026-09-02",
                tz="America/Chicago", rollover_hour=3)


class TestTimesheetRender(_TimesheetCase):
    def test_markdown_has_table_rows_and_totals(self):
        self._seed_two_days()
        text = timesheet.render_timesheet(self._report(), fmt="md")
        self.assertIn("| Date | Ticket |", text)
        self.assertIn("| 2026-09-01 | AB-76 | Upload recovery |", text)
        self.assertIn("1h", text)
        self.assertIn("**Total: 2h 20m**", text)

    def test_csv_is_parseable_with_a_header(self):
        import csv, io
        self._seed_two_days()
        text = timesheet.render_timesheet(self._report(), fmt="csv")
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(rows[0], ["date", "ticket", "title", "summary", "minutes"])
        self.assertEqual(len(rows), 4)

    def test_markdown_never_leaks_ai_usage_without_internal(self):
        self._seed_two_days()
        self.conn.execute(
            "UPDATE work_items SET ai_usage_score=5 WHERE id=?", (self.ab76,))
        text = timesheet.render_timesheet(self._report(), fmt="md")
        self.assertNotIn("AI", text)
        internal_text = timesheet.render_timesheet(self._report(internal=True), fmt="md")
        self.assertIn("AI", internal_text)


if __name__ == "__main__":
    unittest.main()


import contextlib, io

from work_ledger import cli


class TestTimesheetCli(_TimesheetCase):
    def _run(self, argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = cli.main(argv)
        return rc, buffer.getvalue()

    def test_contract_parse_shape(self):
        args = cli.build_parser().parse_args(["timesheet", "--format", "json"])
        self.assertEqual(args.command, "timesheet")
        self.assertEqual(args.format, "json")

    def test_markdown_by_default(self):
        self._seed_two_days()
        rc, out = self._run(["timesheet", "Acme", "--from", "2026-09-01",
                             "--to", "2026-09-02"])
        self.assertEqual(rc, 0)
        self.assertIn("| Date | Ticket |", out)
        self.assertIn("**Total: 2h 20m**", out)

    def test_json_round_and_internal_flags(self):
        self._seed_two_days()
        rc, out = self._run(["timesheet", "Acme", "--from", "2026-09-01",
                             "--to", "2026-09-02", "--format", "json",
                             "--round", "15", "--internal"])
        self.assertEqual(rc, 0)
        report = json.loads(out)
        self.assertEqual(report["total_minutes"], 150)
        self.assertTrue(report["internal"])

    def test_missing_client_is_a_runtime_error(self):
        import sys
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cli.main(["timesheet"])
        self.assertEqual(rc, 2)
        self.assertIn("client", err.getvalue())
