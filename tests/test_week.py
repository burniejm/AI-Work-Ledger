import contextlib, io, json, os, tempfile, unittest
from datetime import date, timedelta
from pathlib import Path

from work_ledger import cli, entities as ent, ids, timeutil, work_items as wi
from work_ledger.db import connection as dbc, migrations as m
from work_ledger.query import week


class _WeekCase(unittest.TestCase):
    TZ = "America/Chicago"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WORK_LEDGER_HOME"] = os.path.join(self.tmp.name, "wl")
        self.conn = dbc.connect()
        m.apply_all(self.conn)
        now = timeutil.utc_now_iso()
        self.conn.execute(
            "INSERT INTO connections(id,name,adapter,origin_device_id,created_at,updated_at)"
            " VALUES ('c1','j','jira-rest','d',?,?)", (now, now))
        self.acme = ent.upsert_client(self.conn, "Acme")
        self.item = wi.upsert_external(self.conn, "c1", "AB-76",
                                       title="Upload recovery", client_id=self.acme)
        self.today = timeutil.workday_of(timeutil.utc_now_iso(), self.TZ, 3)

    def tearDown(self):
        del os.environ["WORK_LEDGER_HOME"]
        self.tmp.cleanup()

    def _activity_on(self, workday, summary="Fixed the retry loop", minutes=60,
                     state=None, item=None):
        start_utc, _ = timeutil.workday_bounds(workday, self.TZ, 3)
        started = timeutil.parse_iso(start_utc) + timedelta(hours=14)
        ended = started + timedelta(minutes=minutes)
        now = timeutil.utc_now_iso()
        aid = ids.new_id()
        self.conn.execute(
            "INSERT INTO activities(id,work_item_id,client_id,kind,summary,started_at,ended_at,"
            "confidence,source,idempotency_key,source_hash,origin_device_id,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, item or self.item, self.acme, "code", summary,
             started.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
             ended.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
             "high", "stage_a", aid, "h", "d", now, now))
        return aid


class TestWeekReport(_WeekCase):
    def test_week_starts_monday_and_rolls_up_per_ticket(self):
        monday = week.monday_of(self.today)
        self.assertEqual(date.fromisoformat(monday).weekday(), 0)
        self._activity_on(self.today)
        report = week.week_report(self.conn, None, ending=self.today,
                                  tz=self.TZ, rollover_hour=3)
        self.assertEqual(report["start_workday"], monday)
        self.assertEqual(report["end_workday"], self.today)
        client = report["clients"][0]
        self.assertEqual(client["client"], "Acme")
        item = client["items"][0]
        self.assertEqual(item["ticket"], "AB-76")
        self.assertEqual(item["minutes"], 60)
        self.assertIn("Fixed the retry loop", item["summaries"])
        self.assertEqual(client["minutes"], 60)
        self.assertEqual(report["total_minutes"], 60)

    def test_scope_filters_to_one_client(self):
        globex = ent.upsert_client(self.conn, "Globex")
        other = wi.upsert_external(self.conn, "c1", "GX-1", title="o", client_id=globex)
        now = timeutil.utc_now_iso()
        aid = ids.new_id()
        self.conn.execute(
            "INSERT INTO activities(id,work_item_id,client_id,kind,summary,started_at,"
            "confidence,source,idempotency_key,source_hash,origin_device_id,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, other, globex, "code", "other work", now,
             "high", "stage_a", aid, "h", "d", now, now))
        self._activity_on(self.today)
        report = week.week_report(self.conn, "Acme", ending=self.today,
                                  tz=self.TZ, rollover_hour=3)
        self.assertEqual([c["client"] for c in report["clients"]], ["Acme"])

    def test_ai_and_cost_fields_are_internal_only(self):
        self.conn.execute(
            "UPDATE work_items SET ai_usage_score=4 WHERE id=?", (self.item,))
        self._activity_on(self.today)
        public = week.week_report(self.conn, None, ending=self.today,
                                  tz=self.TZ, rollover_hour=3)
        self.assertNotIn("ai_usage", json.dumps(public))
        internal = week.week_report(self.conn, None, ending=self.today,
                                    tz=self.TZ, rollover_hour=3, internal=True)
        self.assertEqual(internal["clients"][0]["items"][0]["ai_usage"]["score"], 4)

    def test_markdown_render(self):
        self._activity_on(self.today)
        report = week.week_report(self.conn, None, ending=self.today,
                                  tz=self.TZ, rollover_hour=3)
        text = week.render_week(report, fmt="md")
        self.assertIn("# Week of", text)
        self.assertIn("AB-76", text)
        self.assertIn("1h", text)


class TestStandup(_WeekCase):
    def test_three_sections(self):
        yesterday = (date.fromisoformat(self.today) - timedelta(days=1)).isoformat()
        self._activity_on(yesterday, summary="Shipped the fix")
        self._activity_on(self.today, summary="Started the probe")
        from work_ledger import remaining as rem
        rid = rem.create(self.conn, title="Waiting on Acme creds",
                         normalized_key="acme-creds", work_item_id=self.item,
                         confidence="high", source="manual",
                         occurred_at=timeutil.utc_now_iso())
        rem.transition(self.conn, rid, "blocked", occurred_at=timeutil.utc_now_iso(),
                       source="manual", confidence="high", explanation="no VPN account yet")
        report = week.standup_report(self.conn, None, tz=self.TZ, rollover_hour=3)
        self.assertEqual(report["yesterday"]["workday"], yesterday)
        text = week.render_standup(report)
        self.assertIn("Yesterday:", text)
        self.assertIn("Shipped the fix", text)
        self.assertIn("Today:", text)
        self.assertIn("Started the probe", text)
        self.assertIn("Blockers:", text)
        self.assertIn("no VPN account yet", text)

    def test_quiet_days_render_gracefully(self):
        report = week.standup_report(self.conn, None, tz=self.TZ, rollover_hour=3)
        text = week.render_standup(report)
        self.assertIn("nothing recorded", text)


class TestCli(_WeekCase):
    def _run(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(argv)
        return rc, out.getvalue()

    def test_contract_parse_shapes(self):
        for name in ("week", "standup"):
            args = cli.build_parser().parse_args([name, "--format", "json"])
            self.assertEqual(args.command, name)

    def test_week_and_standup_run(self):
        self._activity_on(self.today)
        rc, out = self._run(["week", "--format", "json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["total_minutes"], 60)
        rc, out = self._run(["standup"])
        self.assertEqual(rc, 0)
        self.assertIn("Today:", out)


if __name__ == "__main__":
    unittest.main()
