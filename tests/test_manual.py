import os, tempfile, unittest
from datetime import datetime, timezone
from work_ledger.db import connection as dbc, migrations as m
from work_ledger import manual, entities as e, timeutil

class TestManualParse(unittest.TestCase):
    def test_keyvalue_form(self):
        s = manual.parse_add("client=Acme project=Replay ticket=AB-326\n"
                             "Tested upload recovery after the network bridge disconnected.")
        self.assertEqual((s.client, s.project, s.ticket), ("Acme", "Replay", "AB-326"))
        self.assertTrue(s.description.startswith("Tested upload recovery"))

    def test_natural_form_with_duration_and_ticket(self):
        s = manual.parse_add("Spent an hour testing AB-326 on replay hardware.")
        self.assertEqual(s.ticket, "AB-326")
        self.assertEqual(s.duration_minutes, 60)

    def test_explicit_minutes(self):
        self.assertEqual(manual.parse_add("Spent 90 minutes on AB-76").duration_minutes, 90)
        self.assertEqual(manual.parse_add("2.5 hours reviewing AB-76").duration_minutes, 150)

    def test_no_duration_means_none(self):
        self.assertIsNone(manual.parse_add("Reviewed the AB-76 patch").duration_minutes)

class TestManualRecord(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite"))
        m.apply_all(self.conn)
    def tearDown(self):
        self.tmp.cleanup()

    def test_record_creates_client_project_and_high_confidence_activity(self):
        s = manual.parse_add("client=Acme project=Replay ticket=AB-326 Tested upload recovery")
        aid = manual.record(self.conn, s, occurred_at="2026-08-29T22:00:00.000Z")
        row = self.conn.execute("SELECT * FROM activities WHERE id=?", (aid,)).fetchone()
        self.assertEqual(row["confidence"], "high")
        self.assertEqual(row["source"], "manual")
        self.assertEqual(row["in_inbox"], 0)
        self.assertIsNotNone(row["work_item_id"])
        self.assertIsNotNone(e.get_client_by_name(self.conn, "Acme"))

    def test_unticketed_manual_work_creates_a_local_item(self):
        s = manual.parse_add("client=Acme project=Ops Diagnosed the Ubuntu boot image on AMD hardware")
        aid = manual.record(self.conn, s)
        wid = self.conn.execute("SELECT work_item_id FROM activities WHERE id=?", (aid,)).fetchone()[0]
        row = self.conn.execute("SELECT kind, display_id FROM work_items WHERE id=?", (wid,)).fetchone()
        self.assertEqual(row["kind"], "local")
        self.assertTrue(row["display_id"].startswith("LOCAL-"))

    def test_description_is_redacted(self):
        s = manual.parse_add("Used token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 while testing")
        aid = manual.record(self.conn, s)
        summary = self.conn.execute("SELECT summary FROM activities WHERE id=?", (aid,)).fetchone()[0]
        self.assertNotIn("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", summary)

    def test_repeat_of_the_same_statement_creates_a_second_activity(self):
        # manual statements are user intent, not replayed evidence: they are NOT deduplicated
        s = manual.parse_add("Reviewed AB-76")
        manual.record(self.conn, s); manual.record(self.conn, s)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM activities").fetchone()[0], 2)

    def test_repeated_ticket_key_reuses_one_work_item(self):
        # Ruling 18: reuse-or-create. Two statements about AB-76 share one work item,
        # guarded by uq_work_items_orphan_key when no connection claims the key.
        manual.record(self.conn, manual.parse_add("Reviewed AB-76"))
        manual.record(self.conn, manual.parse_add("Reviewed ab-76 again"))   # different case
        rows = list(self.conn.execute(
            "SELECT id FROM work_items WHERE upper(external_key)='AB-76' AND deleted_at IS NULL"))
        self.assertEqual(len(rows), 1)
        wids = {r[0] for r in self.conn.execute(
            "SELECT DISTINCT work_item_id FROM activities WHERE work_item_id IS NOT NULL")}
        self.assertEqual(wids, {rows[0]["id"]})

    def test_ticket_key_takes_the_connection_when_exactly_one_pattern_matches(self):
        now = timeutil.utc_now_iso()
        self.conn.execute(
            "INSERT INTO connections(id,name,adapter,ticket_patterns_json,origin_device_id,created_at,updated_at)"
            " VALUES ('c1','acme-jira','jira-rest','[\"AB-*\"]','d',?,?)", (now, now))
        wid, created = manual.resolve_ticket_work_item(self.conn, "AB-76")
        self.assertTrue(created)
        self.assertEqual(self.conn.execute(
            "SELECT connection_id FROM work_items WHERE id=?", (wid,)).fetchone()[0], "c1")
        self.assertEqual(manual.matching_connection_id(self.conn, "AB-76"), "c1")

    def test_ambiguous_or_unmatched_ticket_key_leaves_the_connection_null(self):
        now = timeutil.utc_now_iso()
        for cid, name in (("c1", "a-jira"), ("c2", "b-jira")):
            self.conn.execute(
                "INSERT INTO connections(id,name,adapter,ticket_patterns_json,origin_device_id,created_at,updated_at)"
                " VALUES (?,?,'jira-rest','[\"AB-*\"]','d',?,?)", (cid, name, now, now))
        self.assertIsNone(manual.matching_connection_id(self.conn, "AB-76"))     # ambiguous
        self.assertIsNone(manual.matching_connection_id(self.conn, "ZZ-1"))      # no match
        wid, created = manual.resolve_ticket_work_item(self.conn, "AB-76")
        self.assertTrue(created)
        self.assertIsNone(self.conn.execute(
            "SELECT connection_id FROM work_items WHERE id=?", (wid,)).fetchone()[0])
        # a second statement must reuse the orphan, not violate uq_work_items_orphan_key
        wid2, created2 = manual.resolve_ticket_work_item(self.conn, "ab-76")
        self.assertEqual(wid2, wid)
        self.assertFalse(created2)
class TestResolveWhen(unittest.TestCase):
    """_resolve_when drives occurred_at for `when=` phrases (Spec Section 7).
    Deterministic via the `now` seam already on _resolve_when, pinned to a
    temp WORK_LEDGER_HOME so config defaults (America/Chicago,
    rollover_hour=3, nominal_start_hour=8) are used rather than any real
    machine's config.yaml.
    """
    # 2026-08-30T17:00:00Z == 2026-08-30 12:00:00 America/Chicago (CDT, UTC-5)
    NOW = datetime(2026, 8, 30, 17, 0, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("WORK_LEDGER_HOME")
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("WORK_LEDGER_HOME", None)
        else:
            os.environ["WORK_LEDGER_HOME"] = self._old_home
        self.tmp.cleanup()

    def test_today_is_nominal_start_hour_local(self):
        self.assertEqual(
            manual._resolve_when("today", now=self.NOW),
            "2026-08-30T13:00:00.000Z")  # 08:00 CDT

    def test_yesterday_is_the_previous_calendar_day(self):
        self.assertEqual(
            manual._resolve_when("yesterday", now=self.NOW),
            "2026-08-29T13:00:00.000Z")  # 08:00 CDT the 29th

    def test_last_night_is_21_00_local_the_prior_evening(self):
        self.assertEqual(
            manual._resolve_when("last night", now=self.NOW),
            "2026-08-30T02:00:00.000Z")  # 21:00 CDT on the 29th

    def test_this_morning_is_09_00_local_today(self):
        self.assertEqual(
            manual._resolve_when("this morning", now=self.NOW),
            "2026-08-30T14:00:00.000Z")  # 09:00 CDT

    def test_iso_date(self):
        self.assertEqual(
            manual._resolve_when("2026-08-15", now=self.NOW),
            "2026-08-15T13:00:00.000Z")  # 08:00 CDT

    def test_on_month_day(self):
        self.assertEqual(
            manual._resolve_when("on August 20", now=self.NOW),
            "2026-08-20T13:00:00.000Z")  # 08:00 CDT

    def test_underscore_form_matches_the_spaced_phrase(self):
        # when=<token> is a single whitespace-free token; underscores stand in for spaces.
        self.assertEqual(
            manual._resolve_when("this_morning", now=self.NOW),
            manual._resolve_when("this morning", now=self.NOW))

    def test_unparseable_phrase_returns_none(self):
        self.assertIsNone(manual._resolve_when("next tuesday", now=self.NOW))

    def test_unparseable_when_appends_the_space_restored_phrase_not_underscores(self):
        s = manual.parse_add("when=next_tuesday Reviewed the patch")
        self.assertEqual(s.when, "next_tuesday")
        self.assertTrue(s.description.endswith("(next tuesday)"), s.description)
        self.assertNotIn("next_tuesday", s.description)
