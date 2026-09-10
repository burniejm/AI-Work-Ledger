import os, tempfile, unittest
from work_ledger.db import connection as dbc, migrations as m
from work_ledger.infer import stage_a
from work_ledger import entities as e, ids, timeutil
from work_ledger.capture import events as ev, sessions as S

class TestStageA(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite")); m.apply_all(self.conn)
        now = timeutil.utc_now_iso()
        self.conn.execute("INSERT INTO connections(id,name,adapter,ticket_patterns_json,origin_device_id,created_at,updated_at)"
                          " VALUES ('c1','acme-jira','jira-rest','[\"AB-*\"]','d',?,?)", (now, now))
        self.cl = e.upsert_client(self.conn, "Acme"); self.pr = e.upsert_project(self.conn, self.cl, "Replay")
        r = e.upsert_repository(self.conn, "/Users/x/replay")
        e.map_repository(self.conn, r, client_id=self.cl, project_id=self.pr,
                         confidence="high", source="setup")
        S.upsert(self.conn, session_id="s1", cwd="/Users/x/replay", git_branch="feature/ab-76",
                 occurred_at="2026-08-30T12:00:00.000Z")
    def tearDown(self):
        del os.environ["WORK_LEDGER_HOME"]; self.tmp.cleanup()

    def _event(self, etype, payload, disc, at="2026-08-30T12:05:00.000Z"):
        ev.append_source_event(self.conn, session_id="s1", event_type=etype, occurred_at=at, payload=payload,
                               cwd="/Users/x/replay", git_branch="feature/ab-76",
                               idem=ev.idempotency_key("s1", etype, disc))

    def test_ticket_pattern_compiles_from_glob(self):
        pats = dict((cid, p) for cid, p in stage_a.ticket_patterns(self.conn))
        self.assertRegex("AB-76", pats["c1"])
        self.assertNotRegex("XX-76", pats["c1"])

    def test_branch_and_commit_signals_produce_high_confidence(self):
        self._event("commit", {"sha": "a1b2c3d", "subject": "AB-76 fix upload retry",
                               "author_email": "dev@example.com", "files": ["src/upload.go"]}, "c1")
        out = stage_a.run(self.conn, "s1")
        row = self.conn.execute("SELECT * FROM activities").fetchone()
        self.assertEqual(row["confidence"], "high")
        self.assertEqual(row["source"], "stage_a")
        self.assertIn("AB-76", self.conn.execute(
            "SELECT external_key FROM work_items WHERE id=?", (row["work_item_id"],)).fetchone()[0])

    def test_two_tickets_produce_two_activities(self):
        self._event("commit", {"sha": "a1", "subject": "AB-76 fix upload retry",
                               "author_email": "j@x", "files": ["src/upload.go"]}, "c1")
        self._event("commit", {"sha": "b2", "subject": "AB-326 replay hardware probe",
                               "author_email": "j@x", "files": ["src/probe.go"]}, "c2")
        stage_a.run(self.conn, "s1")
        keys = {r[0] for r in self.conn.execute(
            "SELECT w.external_key FROM activities a JOIN work_items w ON w.id=a.work_item_id")}
        self.assertEqual(keys, {"AB-76", "AB-326"})

    def test_rerun_is_idempotent(self):
        self._event("commit", {"sha": "a1", "subject": "AB-76 fix", "author_email": "j@x", "files": ["a.go"]}, "c1")
        stage_a.run(self.conn, "s1"); stage_a.run(self.conn, "s1")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM activities").fetchone()[0], 1)

    def test_test_result_parsing(self):
        self.assertEqual(stage_a.parse_test_result("pytest -q", {"exit_code": 0, "stdout": "12 passed"})["passed"], True)
        self.assertEqual(stage_a.parse_test_result("npm test", {"exit_code": 1, "stdout": "1 failing"})["passed"], False)
        self.assertIsNone(stage_a.parse_test_result("ls -la", {"exit_code": 0}))

    def test_unmapped_repo_yields_low_confidence_inbox_activity(self):
        S.upsert(self.conn, session_id="s2", cwd="/Users/x/unknown", occurred_at="2026-08-30T12:00:00.000Z")
        ev.append_source_event(self.conn, session_id="s2", event_type="tool_use",
                               occurred_at="2026-08-30T12:05:00.000Z", payload={"tool": "Write", "path": "x.py"},
                               cwd="/Users/x/unknown", idem=ev.idempotency_key("s2", "t", "1"))
        stage_a.run(self.conn, "s2")
        row = self.conn.execute("SELECT confidence, in_inbox FROM activities WHERE session_row_id IN "
                                "(SELECT id FROM sessions WHERE session_id='s2')").fetchone()
        self.assertEqual(row["confidence"], "low")
        self.assertEqual(row["in_inbox"], 1)

    def test_summary_is_templated_not_invented(self):
        # Ruling 21: template "{n} commit(s) on {branch} touching {m} file(s)" with a
        # conditional plural renders exactly this for n=1, m=2.
        self._event("commit", {"sha": "a1", "subject": "AB-76 fix", "author_email": "j@x",
                               "files": ["a.go", "b.go"]}, "c1")
        stage_a.run(self.conn, "s1")
        summary = self.conn.execute("SELECT summary FROM activities").fetchone()[0]
        self.assertIn("1 commit on feature/ab-76 touching 2 files", summary)
        self.assertNotIn("commit(s)", summary)
        self.assertNotIn("file(s)", summary)

    def test_stage_b_absence_keeps_the_summary_templated_and_high_confidence(self):
        # Ruling 6: attribution stays high; the narrative stays templated Stage A prose
        # and no Stage-B-derived row or field appears.
        self._event("commit", {"sha": "a1", "subject": "AB-76 fix", "author_email": "j@x",
                               "files": ["a.go", "b.go"]}, "c1")
        stage_a.run(self.conn, "s1")                 # Stage B never runs in this test
        row = self.conn.execute("SELECT * FROM activities").fetchone()
        self.assertEqual(row["confidence"], "high")               # NOT capped
        self.assertEqual(row["source"], "stage_a")
        self.assertIn("1 commit on feature/ab-76 touching 2 files", row["summary"])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM activities WHERE source='stage_b'").fetchone()[0], 0)

    def test_two_connections_within_the_contradiction_window_do_not_crash_run(self):
        # Regression: CONTRADICTORY_WITHIN_POINTS was deleted from stage_a.py
        # during the Task 27 extraction (moved to confidence.py) but
        # `_competing_candidates` kept referencing the bare name -- any
        # session naming two ticket candidates from different connections
        # within the contradiction window raised NameError inside
        # stage_a.run(). Two commits, each naming a ticket from a different
        # connection, credited to the same known identity so both candidates
        # score identically (30 each -- well within the 10-point window),
        # must complete and mark both activities contradictory.
        now = timeutil.utc_now_iso()
        self.conn.execute(
            "INSERT INTO connections(id,name,adapter,ticket_patterns_json,origin_device_id,"
            "created_at,updated_at) VALUES ('c2','other-jira','jira-rest','[\"VV-*\"]','d',?,?)",
            (now, now))
        self.conn.execute(
            "INSERT INTO user_identities(id,kind,value,origin_device_id,created_at,updated_at)"
            " VALUES ('u1','git','shared@x','d',?,?)", (now, now))
        S.upsert(self.conn, session_id="s3", cwd="/Users/x/replay",
                 occurred_at="2026-08-30T12:00:00.000Z")
        ev.append_source_event(self.conn, session_id="s3", event_type="commit",
                               occurred_at="2026-08-30T12:05:00.000Z",
                               payload={"sha": "d1", "subject": "AB-77 fix",
                                        "author_email": "shared@x", "files": ["a.go"]},
                               cwd="/Users/x/replay",
                               idem=ev.idempotency_key("s3", "commit", "d1"))
        ev.append_source_event(self.conn, session_id="s3", event_type="commit",
                               occurred_at="2026-08-30T12:06:00.000Z",
                               payload={"sha": "d2", "subject": "VV-12 fix",
                                        "author_email": "shared@x", "files": ["b.go"]},
                               cwd="/Users/x/replay",
                               idem=ev.idempotency_key("s3", "commit", "d2"))

        out = stage_a.run(self.conn, "s3")   # must not raise NameError

        rows = self.conn.execute(
            "SELECT confidence, confidence_reason FROM activities WHERE session_row_id IN "
            "(SELECT id FROM sessions WHERE session_id='s3')").fetchall()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["confidence"], "contradictory")
            self.assertIn("AB-77", row["confidence_reason"])
            self.assertIn("VV-12", row["confidence_reason"])


class TestStageAFactExtraction(TestStageA):
    """Regression cover for the fact-extraction findings of the Task 26 review."""

    def _tool_event(self, session_id, payload, disc, at="2026-08-30T12:05:00.000Z",
                    tool_name=None, tool_use_id=None):
        ev.append_source_event(self.conn, session_id=session_id, event_type="tool_use",
                               occurred_at=at, payload=payload, cwd="/Users/x/replay",
                               git_branch="feature/ab-76", tool_name=tool_name,
                               tool_use_id=tool_use_id,
                               idem=ev.idempotency_key(session_id, "tool_use", disc))

    # CRITICAL 1 ------------------------------------------------------------
    def test_transcript_tool_use_without_a_result_records_no_verdict(self):
        # The tool_result lives in the NEXT transcript record, so the block
        # itself has neither an exit code nor output. Guessing "failed" there
        # reported every passing suite as a failure.
        self.assertIsNone(stage_a.parse_test_result("pytest -q", {}))
        self.assertIsNone(stage_a.parse_test_result("pytest -q", {"stdout": "   "}))

    def test_dual_capture_of_one_bash_call_is_one_passing_test_result(self):
        # Hook row (has the exit code) + transcript row (has none), same
        # tool_use_id. One test result, and it is the passing one.
        self._tool_event("s1", {"tool_name": "Bash", "tool_input": {"command": "pytest -q"},
                                "tool_response": {"exit_code": 0, "stdout": "12 passed"}},
                         "hook", tool_name="Bash", tool_use_id="toolu_1")
        self._tool_event("s1", {"tool_uses": [{"id": "toolu_1", "name": "Bash",
                                               "input": {"command": "pytest -q"}}],
                                "tool_results": []},
                         "transcript", at="2026-08-30T12:05:30.000Z")
        drafts = stage_a.split_activities(self.conn, "s1")
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].summary, "Ran the test suite: pytest passed")
        self.assertEqual(drafts[0].outcome, "tests passing")

    # IMPORTANT 2 -----------------------------------------------------------
    def test_push_only_session_is_not_a_commit(self):
        self._event("git_commit_signal",
                    {"command": "git push origin feature/ab-76", "subject": None,
                     "exit_code": 0}, "push")
        self.assertEqual(stage_a.split_activities(self.conn, "s1"), [])
        self.assertEqual(stage_a.run(self.conn, "s1")["created"], 0)

    def test_git_commit_signal_with_a_subject_is_a_commit(self):
        self._event("git_commit_signal",
                    {"command": "git commit -m 'AB-76 fix'", "subject": "AB-76 fix",
                     "exit_code": 0}, "commit")
        drafts = stage_a.split_activities(self.conn, "s1")
        self.assertEqual(len(drafts), 1)
        self.assertIn("1 commit on feature/ab-76 touching 0 files", drafts[0].summary)

    # IMPORTANT 3 -----------------------------------------------------------
    def test_a_command_that_merely_mentions_a_runner_is_not_a_test_run(self):
        for command in ('grep -rn pytest .',
                        'git commit -m "fix pytest flake"',
                        'echo "run pytest later"',
                        'cat pytest.ini'):
            self.assertIsNone(stage_a.parse_test_result(command, {"exit_code": 0}), command)

    def test_wrapped_and_chained_test_commands_are_still_recognized(self):
        for command in ("python3 -m pytest -q", "CI=1 npx jest", "make build && pytest -q",
                        "/usr/local/bin/pytest tests", "poetry run pytest"):
            result = stage_a.parse_test_result(command, {"exit_code": 0, "stdout": "1 passed"})
            if "jest" in command:
                self.assertIsNone(result)      # jest is not in the v1 runner set
            else:
                self.assertIsNotNone(result, command)
                self.assertTrue(result["passed"], command)

    # IMPORTANT 4 -----------------------------------------------------------
    def test_zero_failures_is_a_pass(self):
        result = stage_a.parse_test_result(
            "cargo test", {"exit_code": 0, "stdout": "test result: ok. 5 passed; 0 failed;"})
        self.assertTrue(result["passed"])
        self.assertEqual(result["detail"], "cargo test passed")

    # IMPORTANT 5 -----------------------------------------------------------
    def test_commit_message_credit_requires_a_configured_identity(self):
        events_ = [r for r in self.conn.execute("SELECT * FROM source_events WHERE 1=0")]
        self._event("commit", {"sha": "a1", "subject": "AB-326 probe",
                               "author_email": "someone@else", "files": ["a.go"]}, "c1")
        rows = self.conn.execute(
            "SELECT * FROM source_events WHERE session_id='s1'").fetchall()
        by_key = {c.key: c for c in stage_a.extract_ticket_candidates(
            self.conn, rows, branch=None)}
        self.assertNotIn("ticket_in_commit_message", by_key["AB-326"].signals)
        self.assertEqual(by_key["AB-326"].signals.get("ticket_in_tool_io"), 1)

        now = timeutil.utc_now_iso()
        self.conn.execute(
            "INSERT INTO user_identities(id,kind,value,origin_device_id,created_at,updated_at)"
            " VALUES ('u1','git','someone@else','d',?,?)", (now, now))
        by_key = {c.key: c for c in stage_a.extract_ticket_candidates(
            self.conn, rows, branch=None)}
        self.assertEqual(by_key["AB-326"].signals.get("ticket_in_commit_message"), 1)
        self.assertEqual(events_, [])

    # IMPORTANT 6 -----------------------------------------------------------
    def test_unticketed_work_reuses_one_local_item_across_sessions(self):
        # Two sessions of unticketed work on the same repo and branch. The
        # local item's title is scope-derived, so the second session reuses
        # the first item instead of minting LOCAL-0002.
        for index, (session_id, path, at) in enumerate((
                ("u1", "src/a.py", "2026-08-30T12:05:00.000Z"),
                ("u2", "src/b.py", "2026-08-31T09:05:00.000Z"))):
            S.upsert(self.conn, session_id=session_id, cwd="/Users/x/replay",
                     git_branch="main", occurred_at=at)
            ev.append_source_event(
                self.conn, session_id=session_id, event_type="tool_use", occurred_at=at,
                payload={"tool_name": "Write", "tool_input": {"file_path": path}},
                cwd="/Users/x/replay", git_branch="main", tool_name="Write",
                idem=ev.idempotency_key(session_id, "tool_use", f"w{index}"))
            stage_a.run(self.conn, session_id)

        local_ids = [r[0] for r in self.conn.execute(
            "SELECT display_id FROM work_items WHERE kind='local'")]
        self.assertEqual(len(local_ids), 1, local_ids)
        title = self.conn.execute(
            "SELECT title FROM work_items WHERE kind='local'").fetchone()[0]
        self.assertEqual(title, "replay: main")

    # IMPORTANT 8 -----------------------------------------------------------
    def test_file_evidence_links_back_to_its_source_event(self):
        self._tool_event("s1", {"tool_name": "Write", "tool_input": {"file_path": "src/a.py"}},
                         "w1", tool_name="Write")
        stage_a.run(self.conn, "s1")
        row = self.conn.execute(
            "SELECT source_event_id FROM activity_evidence WHERE evidence_type='file'").fetchone()
        self.assertIsNotNone(row["source_event_id"])
        self.assertIsNotNone(self.conn.execute(
            "SELECT id FROM source_events WHERE id=?", (row["source_event_id"],)).fetchone())

    # MINOR: identical globs on two connections must not double-score --------
    def test_identical_globs_on_two_connections_score_once(self):
        now = timeutil.utc_now_iso()
        self.conn.execute(
            "INSERT INTO connections(id,name,adapter,ticket_patterns_json,origin_device_id,"
            "created_at,updated_at) VALUES ('c2','other-jira','jira-rest','[\"AB-*\"]','d',?,?)",
            (now, now))
        rows = self.conn.execute("SELECT * FROM source_events WHERE 1=0").fetchall()
        candidates = stage_a.extract_ticket_candidates(self.conn, rows, branch="feature/ab-76")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].signals["ticket_in_branch"], 1)
        self.assertEqual(candidates[0].score, 35)
        self.assertIsNone(candidates[0].connection_id)   # ambiguous: two claimants
