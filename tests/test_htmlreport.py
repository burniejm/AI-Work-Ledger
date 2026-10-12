import contextlib, io, json, os, tempfile, unittest
from datetime import timedelta
from pathlib import Path

from work_ledger import cli, entities as ent, ids, timeutil, work_items as wi
from work_ledger.db import connection as dbc, migrations as m
from work_ledger.query import htmlreport


class _ReportCase(unittest.TestCase):
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

    def _activity_on(self, workday, summary="Fixed the retry loop", minutes=60):
        start_utc, _ = timeutil.workday_bounds(workday, self.TZ, 3)
        started = timeutil.parse_iso(start_utc) + timedelta(hours=14)
        ended = started + timedelta(minutes=minutes)
        now = timeutil.utc_now_iso()
        aid = ids.new_id()
        self.conn.execute(
            "INSERT INTO activities(id,work_item_id,client_id,kind,summary,started_at,ended_at,"
            "confidence,source,idempotency_key,source_hash,origin_device_id,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, self.item, self.acme, "code", summary,
             started.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
             ended.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
             "high", "stage_a", aid, "h", "d", now, now))


class TestBuildData(_ReportCase):
    def test_today_period_has_one_day_and_totals(self):
        self._activity_on(self.today)
        data = htmlreport.build_data(self.conn, None, period="today",
                                     tz=self.TZ, rollover_hour=3)
        self.assertEqual(data["start_workday"], self.today)
        self.assertEqual(data["end_workday"], self.today)
        self.assertEqual([d["workday"] for d in data["days"]], [self.today])
        self.assertEqual(data["days"][0]["minutes"], 60)
        self.assertEqual(data["total_minutes"], 60)
        self.assertEqual(data["clients"][0]["client"], "Acme")

    def test_week_period_spans_monday_to_today(self):
        from work_ledger.query import week as q_week
        self._activity_on(self.today)
        data = htmlreport.build_data(self.conn, None, period="week",
                                     tz=self.TZ, rollover_hour=3)
        self.assertEqual(data["start_workday"], q_week.monday_of(self.today))
        self.assertEqual(data["days"][-1]["workday"], self.today)
        self.assertEqual(sum(d["minutes"] for d in data["days"]), 60)

    def test_internal_gating_strips_ai_and_cost(self):
        self.conn.execute(
            "UPDATE work_items SET ai_usage_score=4 WHERE id=?", (self.item,))
        self._activity_on(self.today)
        public = htmlreport.build_data(self.conn, None, period="today",
                                       tz=self.TZ, rollover_hour=3)
        self.assertNotIn("ai_usage", json.dumps(public))
        internal = htmlreport.build_data(self.conn, None, period="today",
                                         tz=self.TZ, rollover_hour=3, internal=True)
        self.assertIn("ai_usage", json.dumps(internal))


class TestRenderHtml(_ReportCase):
    def _data(self, **kw):
        kw.setdefault("tz", self.TZ)
        kw.setdefault("rollover_hour", 3)
        kw.setdefault("period", "today")
        return htmlreport.build_data(self.conn, None, **kw)

    def test_html_is_self_contained_with_tiles_and_table(self):
        self._activity_on(self.today)
        html = htmlreport.render_html(self._data())
        self.assertTrue(html.lstrip().startswith("<!DOCTYPE html>"))
        # A single-day report is a stat tile, not a one-bar chart.
        self.assertNotIn("<svg", html)
        self.assertIn("AB-76", html)
        self.assertIn("Fixed the retry loop", html)
        self.assertIn("1h", html)                         # stat tile total
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)                # no external requests
        self.assertIn("prefers-color-scheme: dark", html) # dark mode selected

    def test_multi_day_report_gets_the_bar_chart(self):
        from datetime import date
        yesterday = (date.fromisoformat(self.today) - timedelta(days=1)).isoformat()
        self._activity_on(yesterday, minutes=30)
        self._activity_on(self.today)
        data = self._data(period="month")     # always >= 2 days after the 1st
        if len(data["days"]) < 2:
            self.skipTest("month window is one day today")
        html = htmlreport.render_html(data)
        self.assertIn("<svg", html)
        self.assertIn("Minutes per day", html)
        self.assertIn("<title>", html)        # native hover tooltips per bar

    def test_summaries_are_escaped(self):
        self._activity_on(self.today, summary='<script>alert("x")</script> fix')
        html = htmlreport.render_html(self._data())
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)

    def test_bar_heights_scale_with_minutes(self):
        from datetime import date
        yesterday = (date.fromisoformat(self.today) - timedelta(days=1)).isoformat()
        self._activity_on(yesterday, minutes=30)
        self._activity_on(self.today, minutes=60)
        data = htmlreport.build_data(self.conn, None, period="week",
                                     tz=self.TZ, rollover_hour=3)
        if len(data["days"]) < 2:      # Monday: yesterday fell in last week
            self.skipTest("week window too small today for a two-bar assertion")
        bars = htmlreport._bar_geometry(data["days"], width=560, height=120)
        by_day = {b["workday"]: b for b in bars}
        self.assertAlmostEqual(by_day[yesterday]["h"] * 2, by_day[self.today]["h"], delta=1)

    def test_chart_bars_show_a_visible_value_not_only_a_hover_tooltip(self):
        from datetime import date
        yesterday = (date.fromisoformat(self.today) - timedelta(days=1)).isoformat()
        self._activity_on(yesterday, minutes=30)
        self._activity_on(self.today, minutes=60)
        data = self._data(period="week")
        if len(data["days"]) < 2:
            self.skipTest("week window too small today for a two-bar assertion")
        html = htmlreport.render_html(data)
        # "1h" must appear as visible chart text, not merely inside the
        # <title> hover tooltip -- a static/printed view has no hover.
        self.assertRegex(html, r'<text class="value"[^>]*>1h</text>')

    def test_multiple_activity_summaries_render_as_separate_list_items(self):
        self._activity_on(self.today, summary="Fixed the retry loop")
        self._activity_on(self.today, summary="Added a regression test")
        html = htmlreport.render_html(self._data())
        self.assertEqual(html.count("<li>"), 2)
        self.assertIn("Fixed the retry loop</li>", html)
        self.assertIn("Added a regression test</li>", html)
        self.assertNotIn("Fixed the retry loop; Added a regression test", html)

    def test_each_activity_shows_its_own_date_and_time(self):
        self._activity_on(self.today, summary="Fixed the retry loop", minutes=45)
        html = htmlreport.render_html(self._data())
        self.assertRegex(
            html, rf'<span class="when">{self.today} · 45m</span> Fixed the retry loop')

    def test_activity_minutes_use_the_stated_duration_when_given(self):
        self._activity_on(self.today, summary="Fixed the retry loop", minutes=45)
        self.conn.execute(
            "UPDATE activities SET stated_duration_minutes=90 WHERE summary='Fixed the retry loop'")
        html = htmlreport.render_html(self._data())
        self.assertIn('· 1h 30m</span> Fixed the retry loop', html)

    def test_table_cells_align_to_the_top_not_centered_against_the_summary_list(self):
        html = htmlreport.render_html(self._data())
        self.assertIn("vertical-align: top", html)

    def test_page_is_centered_and_uses_most_of_the_viewport(self):
        html = htmlreport.render_html(self._data())
        self.assertIn("max-width: 75%", html)
        self.assertIn("margin: 0 auto", html)

    def test_ai_column_appears_only_internal(self):
        self.conn.execute(
            "UPDATE work_items SET ai_usage_score=4, ai_usage_confidence='high'"
            " WHERE id=?", (self.item,))
        self._activity_on(self.today)
        public = htmlreport.render_html(self._data())
        self.assertNotIn("AI usage", public)
        internal = htmlreport.render_html(self._data(internal=True))
        self.assertIn("AI usage", internal)
        self.assertIn("4/5", internal)


class TestCli(_ReportCase):
    def _run(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(argv)
        return rc, out.getvalue()

    def test_contract_parse_shape(self):
        args = cli.build_parser().parse_args(["report", "--format", "json"])
        self.assertEqual(args.command, "report")

    def test_report_writes_a_file_and_prints_its_path(self):
        self._activity_on(self.today)
        rc, out = self._run(["report", "--period", "today", "--no-open"])
        self.assertEqual(rc, 0)
        path = Path(out.strip())
        self.assertTrue(path.is_file(), out)
        self.assertTrue(str(path).endswith(".html"))
        self.assertIn("AB-76", path.read_text())

    def test_json_format_prints_the_data(self):
        self._activity_on(self.today)
        rc, out = self._run(["report", "--format", "json", "--no-open"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["total_minutes"], 60)


if __name__ == "__main__":
    unittest.main()


class TestDunderMainSeesEveryCommand(unittest.TestCase):
    def test_python_dash_m_registers_all_commands(self):
        # Regression: the __main__ guard must be the LAST statement in
        # cli.py. When commands are registered after it, `python3 -m
        # work_ledger.cli` (used by the git post-commit hook shim path)
        # silently loses them: the guard runs main() before they exist.
        import subprocess, sys
        from work_ledger import cli as cli_mod
        repo_src = str(Path(cli_mod.__file__).resolve().parents[1])
        out = subprocess.run(
            [sys.executable, "-m", "work_ledger.cli", "definitely-not-a-command"],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": repo_src})
        for name in cli_mod.registered_names():
            self.assertIn(name, out.stderr,
                          f"{name!r} missing from -m invocation's commands")
