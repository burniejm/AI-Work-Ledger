import os, tempfile, unittest
from work_ledger.db import connection as dbc, migrations as m
from work_ledger import corrections as c, entities as e, work_items as wi, ids, timeutil
from work_ledger.capture import events as ev, sessions as S
from work_ledger.infer import stage_a

class TestCorrections(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite"))
        m.apply_all(self.conn)
        self.cl = e.upsert_client(self.conn, "Acme")
        self.pr = e.upsert_project(self.conn, self.cl, "Replay")
        self.a = wi.upsert_external(self.conn, "conn1", "AB-326", title="Replay hardware")
        self.b = wi.upsert_external(self.conn, "conn1", "AB-76", title="Upload recovery")
        now = timeutil.utc_now_iso()
        self.act = ids.new_id()
        self.conn.execute("INSERT INTO activities(id,work_item_id,client_id,project_id,kind,summary,started_at,"
                          "confidence,source,idempotency_key,source_hash,origin_device_id,created_at,updated_at)"
                          " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (self.act, self.a, self.cl, self.pr, "test", "Tested upload recovery", now,
                           "medium", "stage_a", "idem-1", "h", "dev1", now, now))
    def tearDown(self):
        self.tmp.cleanup()

    def _work_item_of_activity(self):
        return self.conn.execute("SELECT work_item_id FROM activities WHERE id=?", (self.act,)).fetchone()[0]

    def test_move_activity_records_audit(self):
        g = c.move_activity(self.conn, self.act, work_item_id=self.b, reason="wrong ticket")
        self.assertEqual(self._work_item_of_activity(), self.b)
        rows = c.history(self.conn, "activities", self.act)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["operation"], "move")
        self.assertEqual(rows[0]["reason"], "wrong ticket")
        self.assertIn(self.a, rows[0]["old_value_json"])
        self.assertEqual(rows[0]["correction_group_id"], g)

    def test_undo_restores_and_is_itself_audited(self):
        g = c.move_activity(self.conn, self.act, work_item_id=self.b, reason="wrong ticket")
        c.undo(self.conn, g)
        self.assertEqual(self._work_item_of_activity(), self.a)
        rows = c.history(self.conn, "activities", self.act)
        self.assertEqual(len(rows), 2)                     # original + inverse; nothing deleted
        # Ruling 7: the original row is NEVER mutated. "Undone" is derived by lookup.
        self.assertIsNone(rows[0]["undone_at"])
        self.assertEqual(rows[1]["undo_of_correction_id"], rows[0]["id"])
        self.assertTrue(c.is_undone(self.conn, rows[0]["id"]))
        self.assertFalse(c.is_undone(self.conn, rows[1]["id"]))

    def test_undo_never_updates_an_existing_correction_row(self):
        # Ruling 7: pin append-only behaviour by snapshotting the row before undo.
        g = c.move_activity(self.conn, self.act, work_item_id=self.b, reason="wrong ticket")
        before = dict(c.history(self.conn, "activities", self.act)[0])
        c.undo(self.conn, g)
        after = dict(c.history(self.conn, "activities", self.act)[0])
        self.assertEqual(before, after)

    def test_double_undo_raises(self):
        g = c.move_activity(self.conn, self.act, work_item_id=self.b, reason="x")
        c.undo(self.conn, g)
        with self.assertRaises(c.AlreadyUndone):
            c.undo(self.conn, g)

    def test_rejects_unknown_table_and_field(self):
        with self.assertRaises(ValueError):
            c.apply_field_update(self.conn, "source_events", "x", "payload_json", "{}", reason="nope")
        with self.assertRaises(ValueError):
            c.apply_field_update(self.conn, "activities", self.act, "id", "hacked", reason="nope")

    def test_soft_delete_is_reversible(self):
        g = c.soft_delete_record(self.conn, "activities", self.act, reason="not my work")
        self.assertIsNotNone(self.conn.execute("SELECT deleted_at FROM activities WHERE id=?", (self.act,)).fetchone()[0])
        c.undo(self.conn, g)
        self.assertIsNone(self.conn.execute("SELECT deleted_at FROM activities WHERE id=?", (self.act,)).fetchone()[0])

    def test_learned_alias_is_removed_by_undo(self):
        globex = e.upsert_client(self.conn, "Globex")
        g = c.move_activity(self.conn, self.act, client_id=globex, reason="Portal belongs to Globex", learn_alias=True)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM aliases WHERE deleted_at IS NULL").fetchone()[0], 1)
        c.undo(self.conn, g)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM aliases WHERE deleted_at IS NULL").fetchone()[0], 0)

    def test_learn_alias_skipped_when_attribution_unchanged(self):
        # Important 4: re-supplying the activity's *current* client_id with
        # learn_alias=True must not learn a self-referential alias.
        c.move_activity(self.conn, self.act, client_id=self.cl, reason="no real change", learn_alias=True)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM aliases WHERE deleted_at IS NULL").fetchone()[0], 0)

    def test_undo_of_restore_re_deletes(self):
        # Important 1: undo(undo(soft_delete)) must re-delete, not raise.
        g1 = c.soft_delete_record(self.conn, "activities", self.act, reason="oops")
        g2 = c.undo(self.conn, g1)
        self.assertIsNone(self.conn.execute(
            "SELECT deleted_at FROM activities WHERE id=?", (self.act,)).fetchone()[0])
        c.undo(self.conn, g2)
        self.assertIsNotNone(self.conn.execute(
            "SELECT deleted_at FROM activities WHERE id=?", (self.act,)).fetchone()[0])

    def test_correction_invalidates_summary_cache(self):
        self.conn.execute("INSERT INTO summary_cache(id,cache_key,scope_kind,inputs_hash,payload_json,built_at)"
                          " VALUES ('sc1','k','day','h','{}','t')")
        c.move_activity(self.conn, self.act, work_item_id=self.b, reason="x")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM summary_cache").fetchone()[0], 0)

    def test_move_without_a_session_row_leaves_idempotency_key_alone(self):
        # self.act (from setUp) is a bare stage_a-sourced row with no
        # session_row_id -- there is no session for a rerun to reconcile
        # against, so the key is left untouched (nothing to rewrite it to).
        c.move_activity(self.conn, self.act, work_item_id=self.b, reason="x")
        self.assertEqual(
            self.conn.execute("SELECT idempotency_key FROM activities WHERE id=?", (self.act,)).fetchone()[0],
            "idem-1",
        )


class TestMoveActivityAutoLocalItem(unittest.TestCase):
    """A client/project-only correction on a work-item-less activity must
    not leave it permanently title-less in "Unassigned" -- it should get a
    local work item, the same way Stage A would at capture time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite"))
        m.apply_all(self.conn)
        self.cl = e.upsert_client(self.conn, "Acme")
        self.pr = e.upsert_project(self.conn, self.cl, "Replay")

    def tearDown(self):
        self.tmp.cleanup()

    def _unattributed_activity(self, summary="Ran the setup script"):
        now = timeutil.utc_now_iso()
        aid = ids.new_id()
        self.conn.execute(
            "INSERT INTO activities(id,work_item_id,client_id,project_id,kind,summary,started_at,"
            "confidence,source,idempotency_key,source_hash,origin_device_id,created_at,updated_at)"
            " VALUES (?,NULL,NULL,NULL,?,?,?,?,?,?,?,?,?,?)",
            (aid, "test", summary, now, "medium", "stage_a", aid, "h", "dev1", now, now))
        return aid

    def _work_item_of(self, activity_id):
        return self.conn.execute(
            "SELECT work_item_id FROM activities WHERE id=?", (activity_id,)).fetchone()[0]

    def test_client_and_project_correction_creates_a_local_item(self):
        act = self._unattributed_activity()
        c.move_activity(self.conn, act, client_id=self.cl, project_id=self.pr, reason="fix scope")
        wid = self._work_item_of(act)
        self.assertIsNotNone(wid)
        row = self.conn.execute(
            "SELECT kind, client_id, project_id, title FROM work_items WHERE id=?", (wid,)).fetchone()
        self.assertEqual(row["kind"], "local")
        self.assertEqual(row["client_id"], self.cl)
        self.assertEqual(row["project_id"], self.pr)
        self.assertIn("Replay", row["title"])

    def test_client_only_correction_creates_a_local_item_scoped_to_the_client(self):
        act = self._unattributed_activity()
        c.move_activity(self.conn, act, client_id=self.cl, reason="fix scope")
        wid = self._work_item_of(act)
        row = self.conn.execute(
            "SELECT project_id, title FROM work_items WHERE id=?", (wid,)).fetchone()
        self.assertIsNone(row["project_id"])
        self.assertIn("Acme", row["title"])

    def test_repeated_corrections_into_the_same_scope_reuse_one_local_item(self):
        act1 = self._unattributed_activity("Ran the setup script")
        act2 = self._unattributed_activity("Fixed a bug")
        c.move_activity(self.conn, act1, client_id=self.cl, project_id=self.pr, reason="fix scope")
        c.move_activity(self.conn, act2, client_id=self.cl, project_id=self.pr, reason="fix scope")
        self.assertEqual(self._work_item_of(act1), self._work_item_of(act2))

    def test_activity_that_already_has_a_work_item_is_not_given_a_new_one(self):
        item = wi.upsert_external(self.conn, "conn1", "AB-76", title="Upload recovery")
        now = timeutil.utc_now_iso()
        act = ids.new_id()
        self.conn.execute(
            "INSERT INTO activities(id,work_item_id,client_id,project_id,kind,summary,started_at,"
            "confidence,source,idempotency_key,source_hash,origin_device_id,created_at,updated_at)"
            " VALUES (?,?,NULL,NULL,?,?,?,?,?,?,?,?,?,?)",
            (act, item, "test", "Tested upload recovery", now, "medium", "stage_a", act, "h", "dev1", now, now))
        c.move_activity(self.conn, act, client_id=self.cl, reason="fix scope")
        self.assertEqual(self._work_item_of(act), item)

    def test_undo_restores_the_work_item_less_state(self):
        act = self._unattributed_activity()
        g = c.move_activity(self.conn, act, client_id=self.cl, project_id=self.pr, reason="fix scope")
        c.undo(self.conn, g)
        self.assertIsNone(self._work_item_of(act))


class TestMoveActivityIdempotencyKey(unittest.TestCase):
    """Important #2 (final review): `move_activity` must rewrite a
    `stage_a`-sourced, session-backed activity's `idempotency_key` to match
    its new attribution, using `infer.stage_a`'s own key function -- so a
    later Stage A re-run over the same session can never re-materialize the
    corrected-away attribution as a duplicate activity."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("WORK_LEDGER_HOME")
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite"))
        m.apply_all(self.conn)
        now = timeutil.utc_now_iso()
        self.conn.execute(
            "INSERT INTO connections(id,name,adapter,ticket_patterns_json,origin_device_id,created_at,updated_at)"
            " VALUES ('c1','acme-jira','jira-rest','[\"AB-*\"]','d',?,?)", (now, now))
        self.sky = e.upsert_client(self.conn, "Acme")
        self.replay = e.upsert_project(self.conn, self.sky, "Replay")
        self.globex = e.upsert_client(self.conn, "Globex")
        self.globex_pr = e.upsert_project(self.conn, self.globex, "Portal")
        self.repo = e.upsert_repository(self.conn, "/Users/x/replay")
        e.map_repository(self.conn, self.repo, client_id=self.sky, project_id=self.replay,
                         confidence="high", source="setup")
        S.upsert(self.conn, session_id="s1", cwd="/Users/x/replay", git_branch="feature/ab-76",
                 occurred_at="2026-08-30T12:00:00.000Z")
        ev.append_source_event(
            self.conn, session_id="s1", event_type="commit",
            occurred_at="2026-08-30T12:05:00.000Z",
            payload={"sha": "a1b2c3d", "subject": "AB-76 fix upload retry",
                    "author_email": "j@x", "files": ["src/upload.go"]},
            cwd="/Users/x/replay", git_branch="feature/ab-76",
            idem=ev.idempotency_key("s1", "commit", "a1b2c3d"),
        )
        stage_a.run(self.conn, "s1")
        self.act = self.conn.execute("SELECT id FROM activities").fetchone()[0]

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("WORK_LEDGER_HOME", None)
        else:
            os.environ["WORK_LEDGER_HOME"] = self._old_home
        self.tmp.cleanup()

    def _row(self):
        return self.conn.execute(
            "SELECT * FROM activities WHERE id=?", (self.act,)
        ).fetchone()

    def test_move_rewrites_idempotency_key_to_match_the_new_attribution(self):
        before = self._row()
        self.assertEqual(before["client_id"], self.sky)

        c.move_activity(self.conn, self.act, client_id=self.globex, project_id=self.globex_pr,
                        reason="wrong client", learn_alias=True)

        after = self._row()
        self.assertEqual(after["client_id"], self.globex)
        self.assertNotEqual(after["idempotency_key"], before["idempotency_key"])
        expected = stage_a._idempotency_key(
            "s1", c._KeyAttribution(client_id=self.globex, project_id=self.globex_pr,
                                    work_item_id=before["work_item_id"]),
        )
        self.assertEqual(after["idempotency_key"], expected)

        # Recorded in the same correction group as the field changes, and
        # invertible like any other field.
        history_rows = c.history(self.conn, "activities", self.act)
        fields = {r["field"] for r in history_rows}
        self.assertIn("idempotency_key", fields)

    def test_undo_restores_the_original_idempotency_key(self):
        before_key = self._row()["idempotency_key"]
        g = c.move_activity(self.conn, self.act, client_id=self.globex, project_id=self.globex_pr,
                            reason="wrong client", learn_alias=True)
        c.undo(self.conn, g)
        self.assertEqual(self._row()["idempotency_key"], before_key)
        self.assertEqual(self._row()["client_id"], self.sky)

    def test_correct_with_alias_then_reprocess_yields_exactly_one_activity(self):
        # The review's recommended regression (Important #2): a
        # `--learn-alias` correction followed by a Stage A re-run whose
        # deterministic attribution inputs have since come to agree with the
        # correction (here, the repository mapping is updated the way a
        # later `worklog map`/repository-discovery pass would) must not
        # re-materialize the corrected-away attribution as a second
        # activity -- there is exactly one activity for the session, and it
        # carries the corrected attribution.
        c.move_activity(self.conn, self.act, client_id=self.globex, project_id=self.globex_pr,
                        reason="Portal belongs to Globex, not Acme", learn_alias=True)

        # Simulate the repository mapping catching up with the correction
        # (a new repository mapping, per the review's second named trigger).
        e.map_repository(self.conn, self.repo, client_id=self.globex, project_id=self.globex_pr,
                         confidence="high", source="correction")

        stage_a.run(self.conn, "s1")
        stage_a.run(self.conn, "s1")  # idempotent re-run, same as `worklog reprocess`

        rows = self.conn.execute(
            "SELECT client_id, project_id FROM activities WHERE session_row_id IN "
            "(SELECT id FROM sessions WHERE session_id='s1')"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["client_id"], self.globex)
        self.assertEqual(rows[0]["project_id"], self.globex_pr)
