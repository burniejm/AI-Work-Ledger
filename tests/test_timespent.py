import os, tempfile, unittest

from work_ledger.query import timespent


class TestMergeIntervals(unittest.TestCase):
    def test_disjoint_intervals_sum_independently(self):
        merged = timespent.merge_intervals([
            ("2026-09-01T10:00:00.000Z", "2026-09-01T10:30:00.000Z"),
            ("2026-09-01T11:00:00.000Z", "2026-09-01T11:15:00.000Z"),
        ])
        self.assertEqual(len(merged), 2)
        self.assertEqual(timespent.total_minutes(merged), 45)

    def test_overlapping_intervals_do_not_double_count(self):
        merged = timespent.merge_intervals([
            ("2026-09-01T10:00:00.000Z", "2026-09-01T11:00:00.000Z"),
            ("2026-09-01T10:30:00.000Z", "2026-09-01T11:30:00.000Z"),
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(timespent.total_minutes(merged), 90)

    def test_contained_interval_adds_nothing(self):
        merged = timespent.merge_intervals([
            ("2026-09-01T10:00:00.000Z", "2026-09-01T12:00:00.000Z"),
            ("2026-09-01T10:15:00.000Z", "2026-09-01T10:20:00.000Z"),
        ])
        self.assertEqual(timespent.total_minutes(merged), 120)

    def test_empty_input_is_zero(self):
        self.assertEqual(timespent.total_minutes(timespent.merge_intervals([])), 0)


class TestFormatMinutes(unittest.TestCase):
    def test_under_an_hour(self):
        self.assertEqual(timespent.format_minutes(45), "45m")

    def test_hours_and_minutes(self):
        self.assertEqual(timespent.format_minutes(145), "2h 25m")

    def test_exact_hours_drop_the_minutes(self):
        self.assertEqual(timespent.format_minutes(120), "2h")

    def test_zero(self):
        self.assertEqual(timespent.format_minutes(0), "0m")


from work_ledger import ids, timeutil, work_items as wi
from work_ledger.db import connection as dbc, migrations as m


class TestMinutesForWorkItem(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite"))
        m.apply_all(self.conn)
        now = timeutil.utc_now_iso()
        self.conn.execute(
            "INSERT INTO connections(id,name,adapter,origin_device_id,created_at,updated_at)"
            " VALUES ('c1','j','jira-rest','d',?,?)", (now, now))
        self.item = wi.upsert_external(self.conn, "c1", "AB-76", title="t")

    def tearDown(self):
        del os.environ["WORK_LEDGER_HOME"]
        self.tmp.cleanup()

    def _activity(self, started_at, ended_at=None):
        now = timeutil.utc_now_iso()
        aid = ids.new_id()
        self.conn.execute(
            "INSERT INTO activities(id,work_item_id,kind,summary,started_at,ended_at,"
            "confidence,source,idempotency_key,source_hash,origin_device_id,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, self.item, "code", "worked", started_at, ended_at,
             "high", "stage_a", aid, "h", "d", now, now))
        return aid

    def test_overlapping_activities_merge(self):
        self._activity("2026-09-01T10:00:00.000Z", "2026-09-01T11:00:00.000Z")
        self._activity("2026-09-01T10:30:00.000Z", "2026-09-01T11:30:00.000Z")
        self.assertEqual(timespent.minutes_for_work_item(self.conn, self.item), 90)

    def test_point_activity_gets_the_floor(self):
        self._activity("2026-09-01T10:00:00.000Z")             # no ended_at
        self.assertEqual(timespent.minutes_for_work_item(self.conn, self.item), 5)

    def test_window_clips(self):
        self._activity("2026-09-01T10:00:00.000Z", "2026-09-01T12:00:00.000Z")
        minutes = timespent.minutes_for_work_item(
            self.conn, self.item,
            start_utc="2026-09-01T11:00:00.000Z", end_utc="2026-09-01T13:00:00.000Z")
        self.assertEqual(minutes, 60)

    def test_soft_deleted_activities_are_ignored(self):
        aid = self._activity("2026-09-01T10:00:00.000Z", "2026-09-01T11:00:00.000Z")
        self.conn.execute("UPDATE activities SET deleted_at=? WHERE id=?",
                          (timeutil.utc_now_iso(), aid))
        self.assertEqual(timespent.minutes_for_work_item(self.conn, self.item), 0)

    def test_malformed_bounds_are_skipped_not_negative(self):
        self._activity("2026-09-01T11:00:00.000Z", "2026-09-01T10:00:00.000Z")
        self.assertEqual(timespent.minutes_for_work_item(self.conn, self.item), 5)


if __name__ == "__main__":
    unittest.main()


from datetime import timedelta as _td

from work_ledger.capture import sessions as S
from work_ledger.query import daily as q_daily, render as q_render, status as q_status


class TestTimeSurfacing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite"))
        m.apply_all(self.conn)
        now_dt = timeutil.parse_iso(timeutil.utc_now_iso())
        self.start_iso = (now_dt - _td(minutes=60)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self.end_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        now = timeutil.utc_now_iso()
        self.conn.execute(
            "INSERT INTO connections(id,name,adapter,origin_device_id,created_at,updated_at)"
            " VALUES ('c1','j','jira-rest','d',?,?)", (now, now))
        self.item = wi.upsert_external(self.conn, "c1", "AB-76", title="t")
        aid = ids.new_id()
        self.conn.execute(
            "INSERT INTO activities(id,work_item_id,kind,summary,started_at,ended_at,"
            "confidence,source,idempotency_key,source_hash,origin_device_id,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, self.item, "code", "worked", self.start_iso, self.end_iso,
             "high", "stage_a", aid, "h", "d", now, now))

    def tearDown(self):
        del os.environ["WORK_LEDGER_HOME"]
        self.tmp.cleanup()

    def test_status_carries_and_renders_window_minutes(self):
        report = q_status.status_report(
            self.conn, "AB-76", tz="America/Chicago", rollover_hour=3)
        self.assertEqual(report["time"]["minutes"], 60)
        self.assertEqual(report["time"]["label"], "1h")
        text = q_render.render_report(report, fmt="text")
        self.assertIn("time: 1h in window", text)

    def test_day_report_carries_item_minutes_and_client_totals(self):
        workday = timeutil.workday_of(timeutil.utc_now_iso(), "America/Chicago", 3)
        report = q_daily.day_report(
            self.conn, workday, tz="America/Chicago", rollover_hour=3)
        clients = report["clients"]
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0]["minutes"], 60)
        nodes = [w for p in clients[0]["projects"] for w in p["work_items"]]
        self.assertEqual(nodes[0]["minutes"], 60)
        text = q_render.render_report(report, fmt="text")
        self.assertIn("1h", text)

    def test_client_total_does_not_double_count_a_shared_stretch(self):
        # A second ticket for the same client with the SAME hour of activity:
        # each item reports its hour, the client pools to one hour.
        now = timeutil.utc_now_iso()
        item2 = wi.upsert_external(self.conn, "c1", "AB-77", title="t2")
        aid = ids.new_id()
        self.conn.execute(
            "INSERT INTO activities(id,work_item_id,kind,summary,started_at,ended_at,"
            "confidence,source,idempotency_key,source_hash,origin_device_id,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, item2, "code", "worked", self.start_iso, self.end_iso,
             "high", "stage_a", aid, "h", "d", now, now))
        workday = timeutil.workday_of(timeutil.utc_now_iso(), "America/Chicago", 3)
        report = q_daily.day_report(
            self.conn, workday, tz="America/Chicago", rollover_hour=3)
        client = report["clients"][0]
        item_minutes = sorted(
            w["minutes"] for p in client["projects"] for w in p["work_items"])
        self.assertEqual(item_minutes, [60, 60])
        self.assertEqual(client["minutes"], 60)


if __name__ == "__main__":
    unittest.main()
