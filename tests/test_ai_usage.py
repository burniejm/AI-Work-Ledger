import os, tempfile, unittest

from work_ledger.infer import ai_usage


class TestScoreFromSignals(unittest.TestCase):
    """The pure rubric: counts in, (score, confidence, reason) out."""

    def test_no_evidence_at_all_returns_none(self):
        score, confidence, reason = ai_usage.score_from_signals(
            ai_edits=0, ai_commits=0, human_commits=0, prompts=0, sessions=0)
        self.assertIsNone(score)
        self.assertIsNone(confidence)
        self.assertIn("no evidence", reason)

    def test_read_only_sessions_score_one(self):
        # AI answered questions (sessions exist, prompts flowed) but never
        # edited a file; all code, if any, came from the human.
        score, confidence, reason = ai_usage.score_from_signals(
            ai_edits=0, ai_commits=0, human_commits=0, prompts=12, sessions=2)
        self.assertEqual(score, 1)
        self.assertIn("advisory", reason)

    def test_human_commits_with_no_ai_edits_score_one(self):
        score, _, _ = ai_usage.score_from_signals(
            ai_edits=0, ai_commits=0, human_commits=4, prompts=6, sessions=1)
        self.assertEqual(score, 1)

    def test_one_shot_heavy_ai_scores_five(self):
        # One prompt, a pile of AI edits and an AI commit, nothing human.
        score, confidence, _ = ai_usage.score_from_signals(
            ai_edits=20, ai_commits=1, human_commits=0, prompts=1, sessions=1)
        self.assertEqual(score, 5)
        self.assertEqual(confidence, "high")

    def test_iterative_ai_with_no_human_commits_scores_four(self):
        # Lots of back-and-forth steering: high prompt-to-edit ratio.
        score, _, _ = ai_usage.score_from_signals(
            ai_edits=10, ai_commits=1, human_commits=0, prompts=9, sessions=3)
        self.assertEqual(score, 4)

    def test_balanced_mix_scores_three(self):
        score, _, _ = ai_usage.score_from_signals(
            ai_edits=6, ai_commits=0, human_commits=2, prompts=4, sessions=2)
        self.assertEqual(score, 3)

    def test_human_dominant_scores_two(self):
        score, _, _ = ai_usage.score_from_signals(
            ai_edits=2, ai_commits=0, human_commits=6, prompts=3, sessions=2)
        self.assertEqual(score, 2)

    def test_sparse_evidence_lowers_confidence(self):
        score, confidence, _ = ai_usage.score_from_signals(
            ai_edits=1, ai_commits=0, human_commits=0, prompts=1, sessions=1)
        self.assertEqual(score, 4)   # AI-only work, but barely any of it
        self.assertEqual(confidence, "low")

    def test_moderate_evidence_is_medium_confidence(self):
        score, confidence, _ = ai_usage.score_from_signals(
            ai_edits=4, ai_commits=0, human_commits=0, prompts=1, sessions=1)
        self.assertEqual(confidence, "medium")

    def test_reason_is_templated_from_the_counts(self):
        _, _, reason = ai_usage.score_from_signals(
            ai_edits=8, ai_commits=2, human_commits=1, prompts=5, sessions=3)
        self.assertIn("8 AI edits", reason)
        self.assertIn("2 AI commits", reason)
        self.assertIn("1 human-only commit", reason)
        self.assertIn("3 sessions", reason)


from work_ledger.db import connection as dbc, migrations as m


class TestAiUsageSchema(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite"))
        m.apply_all(self.conn)

    def tearDown(self):
        del os.environ["WORK_LEDGER_HOME"]
        self.tmp.cleanup()

    def test_work_items_carries_the_three_ai_usage_columns(self):
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(work_items)")}
        self.assertLessEqual({"ai_usage_score", "ai_usage_confidence", "ai_usage_reason"}, cols)

    def test_score_outside_1_to_5_is_rejected(self):
        import sqlite3
        from work_ledger import work_items as wi, timeutil
        now = timeutil.utc_now_iso()
        self.conn.execute(
            "INSERT INTO connections(id,name,adapter,origin_device_id,created_at,updated_at)"
            " VALUES ('c1','j','jira-rest','d',?,?)", (now, now))
        item = wi.upsert_external(self.conn, "c1", "AB-1", title="t")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE work_items SET ai_usage_score=6 WHERE id=?", (item,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE work_items SET ai_usage_score=0 WHERE id=?", (item,))


import json

from work_ledger import corrections, entities, ids, timeutil, work_items as wi
from work_ledger.capture import events as ev, sessions as S


class TestRecompute(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite"))
        m.apply_all(self.conn)
        now = timeutil.utc_now_iso()
        self.conn.execute(
            "INSERT INTO connections(id,name,adapter,ticket_patterns_json,origin_device_id,"
            "created_at,updated_at) VALUES ('c1','j','jira-rest','[\"AB-*\"]','d',?,?)", (now, now))
        self.item = wi.upsert_external(self.conn, "c1", "AB-76", title="Upload recovery")

    def tearDown(self):
        del os.environ["WORK_LEDGER_HOME"]
        self.tmp.cleanup()

    def _session(self, sid):
        S.upsert(self.conn, session_id=sid, cwd="/w", occurred_at="2026-09-01T12:00:00.000Z")
        return self.conn.execute(
            "SELECT id FROM sessions WHERE session_id=?", (sid,)).fetchone()["id"]

    def _activity(self, session_row_id):
        now = timeutil.utc_now_iso()
        aid = ids.new_id()
        self.conn.execute(
            "INSERT INTO activities(id,work_item_id,session_row_id,kind,summary,started_at,"
            "confidence,source,idempotency_key,source_hash,origin_device_id,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, self.item, session_row_id, "code", "worked", now,
             "high", "stage_a", aid, "h", "d", now, now))
        return aid

    def _event(self, sid, etype, payload, disc, tool_name=None, hook_event=None):
        ev.append_source_event(
            self.conn, session_id=sid, event_type=etype,
            occurred_at="2026-09-01T12:05:00.000Z", payload=payload,
            tool_name=tool_name, hook_event=hook_event,
            idem=ev.idempotency_key(sid, etype, disc))

    def _row(self):
        return self.conn.execute(
            "SELECT ai_usage_score, ai_usage_confidence, ai_usage_reason "
            "FROM work_items WHERE id=?", (self.item,)).fetchone()

    def test_edit_heavy_one_shot_session_scores_five(self):
        srow = self._session("s1"); self._activity(srow)
        self._event("s1", "prompt", {"prompt": "fix AB-76"}, "p1")
        for i in range(6):
            self._event("s1", "tool_use", {"tool_name": "Edit"}, f"e{i}", tool_name="Edit")
        ai_usage.recompute_for_work_item(self.conn, self.item)
        row = self._row()
        self.assertEqual(row["ai_usage_score"], 5)
        self.assertIn("6 AI edits", row["ai_usage_reason"])

    def test_read_only_session_scores_one(self):
        srow = self._session("s1"); self._activity(srow)
        self._event("s1", "prompt", {"prompt": "how does upload retry work?"}, "p1")
        ai_usage.recompute_for_work_item(self.conn, self.item)
        self.assertEqual(self._row()["ai_usage_score"], 1)

    def test_reconciled_commit_in_read_only_session_counts_as_human(self):
        s1 = self._session("s1"); self._activity(s1)          # edit-active session
        for i in range(3):
            self._event("s1", "tool_use", {"tool_name": "Write"}, f"e{i}", tool_name="Write")
        s2 = self._session("s2"); self._activity(s2)          # read-only session
        self._event("s2", "commit", {"sha": "abc", "subject": "AB-76 by hand"},
                    "abc", hook_event="git_reconcile")
        ai_usage.recompute_for_work_item(self.conn, self.item)
        row = self._row()
        self.assertEqual(row["ai_usage_score"], 3)            # 3 ai units vs 5 human units
        self.assertIn("1 human-only commit", row["ai_usage_reason"])

    def test_assistant_transcript_turns_are_not_counted_as_prompts(self):
        srow = self._session("s1"); self._activity(srow)
        for i in range(6):
            self._event("s1", "tool_use", {"tool_name": "Edit"}, f"e{i}", tool_name="Edit")
        self._event("s1", "prompt", {"prompt": "one real prompt"}, "p1")
        for i in range(9):   # Claude narration reconciled from the transcript
            self._event("s1", "prompt", {"role": "assistant", "text": "narration"}, f"n{i}")
        ai_usage.recompute_for_work_item(self.conn, self.item)
        self.assertEqual(self._row()["ai_usage_score"], 5)    # still one-shot

    def test_no_evidence_leaves_score_null_with_reason(self):
        ai_usage.recompute_for_work_item(self.conn, self.item)
        row = self._row()
        self.assertIsNone(row["ai_usage_score"])
        self.assertIn("no evidence", row["ai_usage_reason"])

    def test_a_corrected_score_is_never_recomputed_over(self):
        srow = self._session("s1"); self._activity(srow)
        for i in range(6):
            self._event("s1", "tool_use", {"tool_name": "Edit"}, f"e{i}", tool_name="Edit")
        ai_usage.recompute_for_work_item(self.conn, self.item)
        self.assertEqual(self._row()["ai_usage_score"], 5)
        corrections.apply_field_update(
            self.conn, "work_items", self.item, "ai_usage_score", 2,
            reason="I actually wrote most of this")
        self.assertEqual(self._row()["ai_usage_score"], 2)
        ai_usage.recompute_for_work_item(self.conn, self.item)
        self.assertEqual(self._row()["ai_usage_score"], 2, "recompute clobbered a correction")


from work_ledger import entities as ent
from work_ledger.infer import stage_a


class TestEditToolsLiteralStaysInSync(unittest.TestCase):
    def test_sql_literal_matches_the_module_constant(self):
        # gather_signals hardcodes the IN list as a SQL literal so the
        # security AST prover can verify it; this guard fails if EDIT_TOOLS
        # and that literal ever drift apart.
        import inspect
        source = inspect.getsource(ai_usage.gather_signals)
        for tool in ai_usage.EDIT_TOOLS:
            self.assertIn(f"'{tool}'", source)
        self.assertEqual(len(ai_usage.EDIT_TOOLS), 4)


class TestStageAWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite"))
        m.apply_all(self.conn)
        now = timeutil.utc_now_iso()
        self.conn.execute(
            "INSERT INTO connections(id,name,adapter,ticket_patterns_json,origin_device_id,"
            "created_at,updated_at) VALUES ('c1','j','jira-rest','[\"AB-*\"]','d',?,?)", (now, now))
        cl = ent.upsert_client(self.conn, "Acme")
        pr = ent.upsert_project(self.conn, cl, "Atlas")
        repo = ent.upsert_repository(self.conn, "/w/atlas")
        ent.map_repository(self.conn, repo, client_id=cl, project_id=pr,
                           confidence="high", source="setup")
        S.upsert(self.conn, session_id="s1", cwd="/w/atlas", git_branch="feature/ab-76",
                 occurred_at="2026-09-01T12:00:00.000Z")

    def tearDown(self):
        del os.environ["WORK_LEDGER_HOME"]
        self.tmp.cleanup()

    def test_stage_a_run_recomputes_the_score_for_touched_items(self):
        ev.append_source_event(
            self.conn, session_id="s1", event_type="tool_use",
            occurred_at="2026-09-01T12:04:00.000Z",
            payload={"tool_name": "Edit"}, tool_name="Edit",
            idem=ev.idempotency_key("s1", "tool_use", "e1"))
        ev.append_source_event(
            self.conn, session_id="s1", event_type="commit",
            occurred_at="2026-09-01T12:05:00.000Z",
            payload={"sha": "a1", "subject": "AB-76 fix upload retry",
                     "author_email": "dev@example.com", "files": ["u.go"]},
            cwd="/w/atlas", git_branch="feature/ab-76",
            idem=ev.idempotency_key("s1", "commit", "a1"))
        stage_a.run(self.conn, "s1")
        row = self.conn.execute(
            "SELECT ai_usage_score FROM work_items WHERE external_key='AB-76'").fetchone()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row["ai_usage_score"])


class TestMoveActivityRefreshesScores(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite"))
        m.apply_all(self.conn)
        now = timeutil.utc_now_iso()
        self.conn.execute(
            "INSERT INTO connections(id,name,adapter,ticket_patterns_json,origin_device_id,"
            "created_at,updated_at) VALUES ('c1','j','jira-rest','[\"AB-*\"]','d',?,?)", (now, now))
        self.src_item = wi.upsert_external(self.conn, "c1", "AB-76", title="src")
        self.dst_item = wi.upsert_external(self.conn, "c1", "AB-77", title="dst")
        S.upsert(self.conn, session_id="s1", cwd="/w", occurred_at="2026-09-01T12:00:00.000Z")
        srow = self.conn.execute(
            "SELECT id FROM sessions WHERE session_id='s1'").fetchone()["id"]
        aid = ids.new_id()
        self.conn.execute(
            "INSERT INTO activities(id,work_item_id,session_row_id,kind,summary,started_at,"
            "confidence,source,idempotency_key,source_hash,origin_device_id,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, self.src_item, srow, "code", "worked", now,
             "high", "stage_a", aid, "h", "d", now, now))
        self.activity = aid
        for i in range(6):
            ev.append_source_event(
                self.conn, session_id="s1", event_type="tool_use",
                occurred_at="2026-09-01T12:05:00.000Z", payload={"tool_name": "Edit"},
                tool_name="Edit", idem=ev.idempotency_key("s1", "tool_use", f"e{i}"))
        from work_ledger.infer import ai_usage as au
        au.recompute_for_work_item(self.conn, self.src_item)

    def tearDown(self):
        del os.environ["WORK_LEDGER_HOME"]
        self.tmp.cleanup()

    def _scores(self):
        rows = self.conn.execute(
            "SELECT external_key, ai_usage_score FROM work_items ORDER BY external_key").fetchall()
        return {r["external_key"]: r["ai_usage_score"] for r in rows}

    def test_move_refreshes_both_source_and_destination(self):
        self.assertEqual(self._scores(), {"AB-76": 5, "AB-77": None})
        corrections.move_activity(
            self.conn, self.activity, work_item_id=self.dst_item, reason="wrong ticket")
        scores = self._scores()
        self.assertIsNone(scores["AB-76"], "source kept a score with no evidence left")
        self.assertEqual(scores["AB-77"], 5, "destination not refreshed after move")


from work_ledger.infer import stage_b


class TestStageBRefinement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite"))
        m.apply_all(self.conn)
        now = timeutil.utc_now_iso()
        self.conn.execute(
            "INSERT INTO connections(id,name,adapter,ticket_patterns_json,origin_device_id,"
            "created_at,updated_at) VALUES ('c1','j','jira-rest','[\"AB-*\"]','d',?,?)", (now, now))
        self.item = wi.upsert_external(self.conn, "c1", "AB-76", title="Upload recovery")
        S.upsert(self.conn, session_id="s1", cwd="/w", occurred_at="2026-09-01T12:00:00.000Z")
        srow = self.conn.execute("SELECT id FROM sessions WHERE session_id='s1'").fetchone()["id"]
        aid = ids.new_id()
        self.conn.execute(
            "INSERT INTO activities(id,work_item_id,session_row_id,kind,summary,started_at,"
            "confidence,source,idempotency_key,source_hash,origin_device_id,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, self.item, srow, "code", "worked", now,
             "high", "stage_a", aid, "h", "d", now, now))
        self._n = 0
        for i in range(9):
            self._event("tool_use", {"tool_name": "Edit"}, tool_name="Edit")
        self._event("prompt", {"prompt": "one-shot: fix AB-76"})
        ai_usage.recompute_for_work_item(self.conn, self.item)
        row = self._row()
        assert row["ai_usage_score"] == 5 and row["ai_usage_confidence"] == "medium", dict(row)

    def tearDown(self):
        del os.environ["WORK_LEDGER_HOME"]
        self.tmp.cleanup()

    def _event(self, etype, payload, tool_name=None):
        self._n += 1
        ev.append_source_event(
            self.conn, session_id="s1", event_type=etype,
            occurred_at="2026-09-01T12:05:00.000Z", payload=payload, tool_name=tool_name,
            idem=ev.idempotency_key("s1", etype, str(self._n)))

    def _row(self):
        return self.conn.execute(
            "SELECT ai_usage_score, ai_usage_confidence, ai_usage_reason"
            " FROM work_items WHERE id=?", (self.item,)).fetchone()

    def _merge(self, model_score):
        parsed = stage_b.validate_output(json.dumps({
            "schema_version": 1,
            "activities": [{"kind": "code", "summary": "Fixed retry",
                            "ticket_hint": "AB-76", "confidence": "medium",
                            "evidence_event_ids": [],
                            "ai_usage_score": model_score}]}))
        return stage_b.merge(self.conn, "s1", parsed, "hash-1")

    def test_validate_accepts_an_in_range_score_and_rejects_junk(self):
        parsed = stage_b.validate_output(json.dumps({
            "schema_version": 1,
            "activities": [{"kind": "code", "summary": "s", "confidence": "low",
                            "evidence_event_ids": [], "ai_usage_score": 3}]}))
        self.assertEqual(parsed["activities"][0]["ai_usage_score"], 3)
        for bad in (0, 6, "three", 2.5):
            with self.assertRaises(stage_b.StageBSchemaError):
                stage_b.validate_output(json.dumps({
                    "schema_version": 1,
                    "activities": [{"kind": "code", "summary": "s", "confidence": "low",
                                    "evidence_event_ids": [], "ai_usage_score": bad}]}))

    def test_model_refinement_is_clamped_to_one_step(self):
        self._merge(2)                              # model says 2, baseline 5
        row = self._row()
        self.assertEqual(row["ai_usage_score"], 4)  # clamped to 5-1
        self.assertEqual(row["ai_usage_confidence"], "medium")
        self.assertIn("Stage B", row["ai_usage_reason"])

    def test_agreeing_model_changes_nothing(self):
        before = dict(self._row())
        self._merge(5)
        self.assertEqual(dict(self._row()), before)

    def test_refinement_never_touches_a_corrected_score(self):
        corrections.apply_field_update(
            self.conn, "work_items", self.item, "ai_usage_score", 1,
            reason="hand-written, AI just reviewed")
        self._merge(4)
        self.assertEqual(self._row()["ai_usage_score"], 1)


from work_ledger.query import daily as q_daily, render as q_render, status as q_status


class TestReportSurfacing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite"))
        m.apply_all(self.conn)
        now = timeutil.utc_now_iso()
        self.conn.execute(
            "INSERT INTO connections(id,name,adapter,ticket_patterns_json,origin_device_id,"
            "created_at,updated_at) VALUES ('c1','j','jira-rest','[\"AB-*\"]','d',?,?)", (now, now))
        self.item = wi.upsert_external(self.conn, "c1", "AB-76", title="Upload recovery")
        S.upsert(self.conn, session_id="s1", cwd="/w", occurred_at="2026-09-01T12:00:00.000Z")
        srow = self.conn.execute("SELECT id FROM sessions WHERE session_id='s1'").fetchone()["id"]
        aid = ids.new_id()
        self.conn.execute(
            "INSERT INTO activities(id,work_item_id,session_row_id,kind,summary,started_at,"
            "confidence,source,idempotency_key,source_hash,origin_device_id,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, self.item, srow, "code", "worked",
             "2026-09-01T12:05:00.000Z",
             "high", "stage_a", aid, "h", "d", now, now))
        for i in range(9):
            ev.append_source_event(
                self.conn, session_id="s1", event_type="tool_use",
                occurred_at="2026-09-01T12:05:00.000Z", payload={"tool_name": "Edit"},
                tool_name="Edit", idem=ev.idempotency_key("s1", "tool_use", f"e{i}"))
        ai_usage.recompute_for_work_item(self.conn, self.item)

    def tearDown(self):
        del os.environ["WORK_LEDGER_HOME"]
        self.tmp.cleanup()

    def test_status_carries_and_renders_the_score(self):
        report = q_status.status_report(
            self.conn, "AB-76", tz="America/Chicago", rollover_hour=3)
        self.assertEqual(report["ai_usage"]["score"], 5)
        self.assertEqual(report["ai_usage"]["confidence"], "medium")
        self.assertIn("9 AI edits", report["ai_usage"]["reason"])
        text = q_render.render_report(report, fmt="text")
        self.assertIn("AI usage: 5/5 (medium)", text)

    def test_status_jira_entries_carry_the_score(self):
        report = q_status.status_report(
            self.conn, "AB-76", tz="America/Chicago", rollover_hour=3)
        entry = report["jira"][0]
        self.assertEqual(entry["ai_usage"]["score"], 5)

    def test_day_report_carries_and_renders_the_score(self):
        report = q_daily.day_report(
            self.conn, "2026-09-01", tz="America/Chicago", rollover_hour=3)
        nodes = [w for c in report["clients"] for p in c["projects"]
                 for w in p["work_items"]]
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["ai_usage"]["score"], 5)
        text = q_render.render_report(report, fmt="text")
        self.assertIn("AI 5/5", text)


if __name__ == "__main__":
    unittest.main()
