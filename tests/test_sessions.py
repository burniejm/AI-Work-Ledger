import json, os, subprocess, tempfile, time, unittest
from pathlib import Path
from work_ledger import config, hooks_runtime
from work_ledger.db import connection as dbc, migrations as m
from work_ledger.capture import sessions as S

FIXTURES = Path(__file__).parent / "fixtures"
STUB_BIN = str(FIXTURES / "bin")
_REAL_PATH = None


def setUpModule():
    """Put the stub `claude` (tests/fixtures/bin/claude) ahead of any real one
    on PATH for this whole module.

    Global Constraints: "No real model calls in any test (Stage B is exercised
    with a stub `claude` executable on `PATH`)." `sessions.reprocess` runs
    Stage B in the foreground and the Stop hook spawns it detached, so three
    tests here reach `stage_b.invoke_claude` without meaning to -- and the
    detached children inherit this PATH too. Restored in tearDownModule.
    """
    global _REAL_PATH
    _REAL_PATH = os.environ["PATH"]
    os.environ["PATH"] = STUB_BIN + os.pathsep + _REAL_PATH


def tearDownModule():
    if _REAL_PATH is not None:
        os.environ["PATH"] = _REAL_PATH


def sh(cwd, *args, env=None):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, **env} if env else None)


class TestSessions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite")); m.apply_all(self.conn)
    def tearDown(self):
        del os.environ["WORK_LEDGER_HOME"]; self.tmp.cleanup()

    def test_upsert_is_idempotent_and_advances_last_seen(self):
        a = S.upsert(self.conn, session_id="s1", cwd="/tmp", occurred_at="2026-08-30T12:00:00.000Z")
        b = S.upsert(self.conn, session_id="s1", cwd="/tmp", occurred_at="2026-08-30T13:00:00.000Z")
        self.assertEqual(a, b)
        self.assertEqual(self.conn.execute("SELECT last_seen_at FROM sessions").fetchone()[0], "2026-08-30T13:00:00.000Z")

    def test_finalize_is_idempotent(self):
        S.upsert(self.conn, session_id="s1", occurred_at="2026-08-30T12:00:00.000Z")
        S.finalize(self.conn, "s1", ended_at="2026-08-30T14:00:00.000Z", reason="session_end")
        S.finalize(self.conn, "s1", ended_at="2026-08-30T15:00:00.000Z", reason="idle_finalized")
        row = self.conn.execute("SELECT ended_at, end_reason FROM sessions").fetchone()
        self.assertEqual(row["ended_at"], "2026-08-30T14:00:00.000Z")
        self.assertEqual(row["end_reason"], "session_end")

    def test_stale_sessions_are_finalized_lazily(self):
        S.upsert(self.conn, session_id="old", occurred_at="2026-08-30T10:00:00.000Z")
        S.upsert(self.conn, session_id="fresh", occurred_at="2026-08-30T12:55:00.000Z")
        done = S.finalize_stale(self.conn, now="2026-08-30T13:00:00.000Z", idle_minutes=30)
        self.assertEqual(done, ["old"])
        self.assertEqual(self.conn.execute(
            "SELECT end_reason FROM sessions WHERE session_id='old'").fetchone()[0], "idle_finalized")
        self.assertIsNone(self.conn.execute(
            "SELECT ended_at FROM sessions WHERE session_id='fresh'").fetchone()[0])
        # ended_at is event time (the session's own last_seen_at), not the
        # sweep time -- the sweep time is only when we noticed.
        self.assertEqual(self.conn.execute(
            "SELECT ended_at FROM sessions WHERE session_id='old'").fetchone()[0],
            "2026-08-30T10:00:00.000Z")

    def test_transcript_reconciliation_is_idempotent(self):
        src = Path(__file__).parent / "fixtures" / "transcript" / "sample.jsonl"
        dest = Path(self.tmp.name) / "t.jsonl"; dest.write_text(src.read_text())
        S.upsert(self.conn, session_id="s1", transcript_path=str(dest), occurred_at="2026-08-30T12:00:00.000Z")
        first = S.reconcile_transcript(self.conn, "s1")
        self.assertGreater(first, 0)
        self.assertEqual(S.reconcile_transcript(self.conn, "s1"), 0)   # watermark held
        count = self.conn.execute("SELECT count(*) FROM source_events").fetchone()[0]
        self.assertEqual(count, first)

    def test_reprocess_creates_no_duplicates(self):
        src = Path(__file__).parent / "fixtures" / "transcript" / "sample.jsonl"
        dest = Path(self.tmp.name) / "t.jsonl"; dest.write_text(src.read_text())
        S.upsert(self.conn, session_id="s1", transcript_path=str(dest), occurred_at="2026-08-30T12:00:00.000Z")
        S.reconcile_transcript(self.conn, "s1")
        before_events = self.conn.execute("SELECT count(*) FROM source_events").fetchone()[0]
        S.reprocess(self.conn, "s1")
        S.reprocess(self.conn, "s1")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM source_events").fetchone()[0], before_events)

    def test_touch_only_moves_last_seen_forward(self):
        S.upsert(self.conn, session_id="s1", occurred_at="2026-08-30T12:00:00.000Z")
        S.touch(self.conn, "s1", occurred_at="2026-08-30T11:00:00.000Z")   # out of order
        self.assertEqual(self.conn.execute("SELECT last_seen_at FROM sessions").fetchone()[0],
                         "2026-08-30T12:00:00.000Z")
        S.touch(self.conn, "s1", occurred_at="2026-08-30T12:30:00.000Z")
        self.assertEqual(self.conn.execute("SELECT last_seen_at FROM sessions").fetchone()[0],
                         "2026-08-30T12:30:00.000Z")
        S.touch(self.conn, "unknown-session", occurred_at="2026-08-30T13:00:00.000Z")  # no-op
        self.assertEqual(self.conn.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)

    def test_finalize_normalizes_an_unrecognized_reason(self):
        S.upsert(self.conn, session_id="s1", occurred_at="2026-08-30T12:00:00.000Z")
        S.finalize(self.conn, "s1", ended_at="2026-08-30T14:00:00.000Z", reason="prompt_input_exit")
        self.assertEqual(self.conn.execute("SELECT end_reason FROM sessions").fetchone()[0], "unknown")

    def test_transcript_reconciliation_resumes_after_a_bounded_pass(self):
        src = FIXTURES / "transcript" / "sample.jsonl"
        dest = Path(self.tmp.name) / "t.jsonl"; dest.write_text(src.read_text())
        S.upsert(self.conn, session_id="s1", transcript_path=str(dest), occurred_at="2026-08-30T12:00:00.000Z")
        self.assertEqual(S.reconcile_transcript(self.conn, "s1", max_records=2), 2)
        rest = S.reconcile_transcript(self.conn, "s1")       # resumes from the watermark
        self.assertEqual(rest, 3)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM source_events").fetchone()[0], 5)

    # --- gap G4: sidechain records must be flagged -------------------------

    def test_sidechain_records_are_flagged(self):
        src = FIXTURES / "transcript" / "sample.jsonl"
        dest = Path(self.tmp.name) / "t.jsonl"; dest.write_text(src.read_text())
        S.upsert(self.conn, session_id="s1", transcript_path=str(dest), occurred_at="2026-08-30T12:00:00.000Z")
        S.reconcile_transcript(self.conn, "s1")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM source_events WHERE is_sidechain=1").fetchone()[0], 1)

    def test_reconciled_events_carry_occurred_at_from_the_transcript(self):
        src = FIXTURES / "transcript" / "sample.jsonl"
        dest = Path(self.tmp.name) / "t.jsonl"; dest.write_text(src.read_text())
        S.upsert(self.conn, session_id="s1", transcript_path=str(dest), occurred_at="2026-08-30T12:00:00.000Z")
        S.reconcile_transcript(self.conn, "s1")
        row = self.conn.execute(
            "SELECT occurred_at, git_branch FROM source_events ORDER BY occurred_at LIMIT 1").fetchone()
        self.assertEqual(row["occurred_at"], "2026-08-30T10:00:00.000Z")
        self.assertEqual(row["git_branch"], "feature/work-ledger-v1")

    def test_reconcile_transcript_with_no_transcript_is_zero_not_an_error(self):
        S.upsert(self.conn, session_id="s1", occurred_at="2026-08-30T12:00:00.000Z")
        self.assertEqual(S.reconcile_transcript(self.conn, "s1"), 0)
        self.assertEqual(S.reconcile_transcript(self.conn, "no-such-session"), 0)

    def test_new_evidence_reopens_an_idle_finalized_session(self):
        S.upsert(self.conn, session_id="s1", occurred_at="2026-08-30T10:00:00.000Z")
        S.finalize_stale(self.conn, now="2026-08-30T13:00:00.000Z", idle_minutes=30)
        # a resumed session: evidence arrives after the lazy sweep guessed it was over
        S.touch(self.conn, "s1", occurred_at="2026-08-30T14:00:00.000Z")
        row = self.conn.execute("SELECT * FROM sessions").fetchone()
        self.assertIsNone(row["ended_at"])
        self.assertIsNone(row["end_reason"])
        self.assertEqual(row["last_seen_at"], "2026-08-30T14:00:00.000Z")
        # ... and upsert reopens it too
        S.finalize_stale(self.conn, now="2026-08-30T18:00:00.000Z", idle_minutes=30)
        S.upsert(self.conn, session_id="s1", occurred_at="2026-08-30T19:00:00.000Z")
        self.assertIsNone(self.conn.execute("SELECT ended_at FROM sessions").fetchone()[0])

    def test_an_out_of_order_delivery_does_not_reopen(self):
        S.upsert(self.conn, session_id="s1", occurred_at="2026-08-30T12:00:00.000Z")
        S.finalize_stale(self.conn, now="2026-08-30T18:00:00.000Z", idle_minutes=30)
        # a late-arriving hook carrying an OLDER timestamp is not fresh evidence
        S.upsert(self.conn, session_id="s1", occurred_at="2026-08-30T11:00:00.000Z")
        row = self.conn.execute("SELECT ended_at, end_reason FROM sessions").fetchone()
        self.assertIsNotNone(row["ended_at"])
        self.assertEqual(row["end_reason"], "idle_finalized")

    def test_an_observed_end_is_never_reopened(self):
        S.upsert(self.conn, session_id="s1", occurred_at="2026-08-30T10:00:00.000Z")
        S.finalize(self.conn, "s1", ended_at="2026-08-30T11:00:00.000Z", reason="session_end")
        S.touch(self.conn, "s1", occurred_at="2026-08-30T14:00:00.000Z")
        row = self.conn.execute("SELECT ended_at, end_reason FROM sessions").fetchone()
        self.assertEqual(row["ended_at"], "2026-08-30T11:00:00.000Z")
        self.assertEqual(row["end_reason"], "session_end")

    # --- gap G5: git reconciliation ---------------------------------------

    def test_reconcile_git_appends_commit_events_once(self):
        repo = tempfile.TemporaryDirectory(); self.addCleanup(repo.cleanup)
        sh(repo.name, "git", "init", "-q", "-b", "main")
        sh(repo.name, "git", "config", "user.email", "dev@example.com")
        sh(repo.name, "git", "config", "user.name", "Test User")
        Path(repo.name, "a.txt").write_text("hello")
        sh(repo.name, "git", "add", "a.txt")
        sh(repo.name, "git", "commit", "-q", "-m", "AB-76 add the upload retry")

        S.upsert(self.conn, session_id="s1", cwd=repo.name, occurred_at="1970-01-01T00:00:00.000Z")
        first = S.reconcile_git(self.conn, "s1")
        self.assertEqual(first, 1)
        self.assertEqual(S.reconcile_git(self.conn, "s1"), 0)   # idempotent
        row = self.conn.execute(
            "SELECT payload_json FROM source_events WHERE event_type='commit'").fetchone()
        self.assertIn("AB-76 add the upload retry", row["payload_json"])

    def _repo_with_commits(self, count, *, same_second=False):
        repo = tempfile.TemporaryDirectory(); self.addCleanup(repo.cleanup)
        sh(repo.name, "git", "init", "-q", "-b", "main")
        sh(repo.name, "git", "config", "user.email", "dev@example.com")
        sh(repo.name, "git", "config", "user.name", "Test User")
        for i in range(count):
            Path(repo.name, f"f{i}.txt").write_text(str(i))
            sh(repo.name, "git", "add", ".")
            # Explicit dates: deterministic ordering with no sleeps.
            stamp = ("2026-08-20T10:00:00-05:00" if same_second
                     else f"2026-08-20T10:{i // 60:02d}:{i % 60:02d}-05:00")
            sh(repo.name, "git", "commit", "-q", "-m", f"commit {i}",
               env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})
        return repo.name

    def _commit_count(self):
        return self.conn.execute(
            "SELECT count(*) FROM source_events WHERE event_type='commit'").fetchone()[0]

    def _drain_git(self, max_passes=60):
        """Run reconcile_git until it reports itself caught up."""
        for passes in range(1, max_passes + 1):
            S.reconcile_git(self.conn, "s1")
            if not S.git_watermark(self.conn, "s1")["truncated"]:
                return passes
        self.fail(f"reconcile_git never caught up: {self._commit_count()} commits after "
                  f"{max_passes} passes")

    def _use_git_limit(self, limit):
        config.save({**config.load(),
                     "git": {"subprocess_timeout_seconds": 2, "max_commits_per_reconcile": limit}})

    def test_a_backlog_far_past_the_window_is_fully_ingested(self):
        # A newest-first --max-count window drops the older tail; the old
        # widen-with-a-cap approach plateaued and lost the oldest commits
        # forever. Repeated passes must ingest EVERYTHING.
        repo = self._repo_with_commits(30)
        self._use_git_limit(2)
        S.upsert(self.conn, session_id="s1", cwd=repo, occurred_at="1970-01-01T00:00:00.000Z")

        self.assertEqual(S.reconcile_git(self.conn, "s1"), 2)       # first window
        self.assertTrue(S.git_watermark(self.conn, "s1")["truncated"])
        self._drain_git()
        self.assertEqual(self._commit_count(), 30)                  # nothing lost

        state = S.git_watermark(self.conn, "s1")
        self.assertIsNone(state["upper"])                           # back to steady state
        self.assertFalse(state["truncated"])
        S.reconcile_git(self.conn, "s1")                            # still idempotent
        self.assertEqual(self._commit_count(), 30)

    def test_a_cluster_sharing_one_timestamp_still_drains(self):
        # The one case the backwards cursor cannot move through: more
        # commits at a single timestamp than the window holds.
        repo = self._repo_with_commits(10, same_second=True)
        self._use_git_limit(2)
        S.upsert(self.conn, session_id="s1", cwd=repo, occurred_at="1970-01-01T00:00:00.000Z")
        S.reconcile_git(self.conn, "s1")
        self._drain_git()
        self.assertEqual(self._commit_count(), 10)
        self.assertEqual(S.git_watermark(self.conn, "s1")["limit"], 2)   # window resets

    def _watermark_rows(self):
        return self.conn.execute(
            "SELECT count(*) FROM capture_checkpoints WHERE hook_event='git_watermark'"
        ).fetchone()[0]

    def test_a_drained_cluster_settles_instead_of_flapping(self):
        # `len(batch) >= limit` alone cannot tell a new backlog from the
        # inclusive --since boundary re-matching an already-ingested
        # cluster: the state used to flip limit 2->4->8->16->reset forever,
        # appending a watermark row on nearly every call and reporting
        # git_backlog=pending for good.
        repo = self._repo_with_commits(10, same_second=True)
        self._use_git_limit(2)
        S.upsert(self.conn, session_id="s1", cwd=repo, occurred_at="1970-01-01T00:00:00.000Z")
        S.reconcile_git(self.conn, "s1")
        self._drain_git()
        self.assertEqual(self._commit_count(), 10)

        settled = self._watermark_rows()
        for _ in range(14):
            self.assertEqual(S.reconcile_git(self.conn, "s1"), 0)
        self.assertEqual(self._watermark_rows(), settled)        # append-only: no new rows
        state = S.git_watermark(self.conn, "s1")
        self.assertFalse(state["truncated"])                     # no phantom backlog
        self.assertEqual(state["limit"], 2)                      # window did not creep
        self.assertIsNone(state["upper"])

        # ... and a genuinely new commit is still picked up afterwards.
        Path(repo, "new.txt").write_text("new")
        sh(repo, "git", "add", ".")
        sh(repo, "git", "commit", "-q", "-m", "brand new",
           env={"GIT_AUTHOR_DATE": "2026-08-25T09:00:00-05:00",
                "GIT_COMMITTER_DATE": "2026-08-25T09:00:00-05:00"})
        S.reconcile_git(self.conn, "s1")
        self._drain_git()
        self.assertEqual(self._commit_count(), 11)

    def test_git_watermark_advances_so_later_passes_narrow_the_window(self):
        repo = self._repo_with_commits(2)
        S.upsert(self.conn, session_id="s1", cwd=repo, occurred_at="1970-01-01T00:00:00.000Z")
        self.assertEqual(S.reconcile_git(self.conn, "s1"), 2)
        state = S.git_watermark(self.conn, "s1")
        self.assertFalse(state["truncated"])
        self.assertIsNone(state["upper"])
        self.assertGreater(state["since"], "1970-01-01T00:00:00.000Z")
        self.assertEqual(S.reconcile_git(self.conn, "s1"), 0)

    def test_reconcile_git_outside_a_repo_is_zero(self):
        with tempfile.TemporaryDirectory() as d:
            S.upsert(self.conn, session_id="s1", cwd=d, occurred_at="1970-01-01T00:00:00.000Z")
            self.assertEqual(S.reconcile_git(self.conn, "s1"), 0)

    def test_reprocess_reports_what_it_did(self):
        src = FIXTURES / "transcript" / "sample.jsonl"
        dest = Path(self.tmp.name) / "t.jsonl"; dest.write_text(src.read_text())
        S.upsert(self.conn, session_id="s1", transcript_path=str(dest), occurred_at="2026-08-30T12:00:00.000Z")
        result = S.reprocess(self.conn, "s1", stage="both")
        self.assertEqual(result["session_id"], "s1")
        self.assertGreater(result["transcript_events"], 0)
        self.assertIn("stage_a", result)
        self.assertIn("stage_b", result)

    def test_recent_lists_sessions_with_capture_counts(self):
        src = FIXTURES / "transcript" / "sample.jsonl"
        dest = Path(self.tmp.name) / "t.jsonl"; dest.write_text(src.read_text())
        S.upsert(self.conn, session_id="s1", transcript_path=str(dest), occurred_at="2026-08-30T12:00:00.000Z")
        S.reconcile_transcript(self.conn, "s1")
        rows = S.recent(self.conn, limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session_id"], "s1")
        self.assertGreater(rows[0]["event_count"], 0)


class TestSessionHooks(unittest.TestCase):
    """The session-start / stop / session-end handlers in hooks_runtime."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name
        self.repo = tempfile.TemporaryDirectory(); self.addCleanup(self.repo.cleanup)
        sh(self.repo.name, "git", "init", "-q", "-b", "main")
        sh(self.repo.name, "git", "config", "user.email", "dev@example.com")
        sh(self.repo.name, "git", "config", "user.name", "Test User")
        Path(self.repo.name, "a.txt").write_text("hello")
        sh(self.repo.name, "git", "add", "a.txt")
        sh(self.repo.name, "git", "commit", "-q", "-m", "AB-76 add the upload retry")

    def tearDown(self):
        del os.environ["WORK_LEDGER_HOME"]; self.tmp.cleanup()

    def _conn(self):
        return dbc.connect(str(Path(self.tmp.name) / "work-ledger.sqlite"))

    def test_session_start_creates_the_session_row_and_event(self):
        payload = {"session_id": "sess-1", "cwd": self.repo.name,
                   "transcript_path": "/tmp/t.jsonl", "hook_event_name": "SessionStart",
                   "source": "startup", "version": "1.2.3"}
        self.assertEqual(hooks_runtime.handle("session-start", payload), 0)
        conn = self._conn()
        row = conn.execute("SELECT * FROM sessions").fetchone()
        self.assertEqual(row["session_id"], "sess-1")
        self.assertEqual(row["claude_version"], "1.2.3")
        self.assertIsNotNone(row["repository_id"])          # gap: repo resolved from cwd
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM source_events WHERE event_type='session_start'").fetchone()[0], 1)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM capture_checkpoints WHERE hook_event='session_start'").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT count(*) FROM repositories").fetchone()[0], 1)

    def test_session_start_finalizes_stale_sessions(self):
        conn = self._conn(); m.apply_all(conn)
        S.upsert(conn, session_id="ancient", occurred_at="2000-01-01T00:00:00.000Z")
        conn.close()
        hooks_runtime.handle("session-start", {"session_id": "sess-1", "cwd": self.repo.name})
        conn = self._conn()
        self.assertEqual(conn.execute(
            "SELECT end_reason FROM sessions WHERE session_id='ancient'").fetchone()[0], "idle_finalized")

    def test_later_events_inherit_the_sessions_repository(self):
        hooks_runtime.handle("session-start", {"session_id": "sess-1", "cwd": self.repo.name})
        hooks_runtime.handle("post-tool-use", {
            "session_id": "sess-1", "tool_name": "Write", "tool_use_id": "toolu_9",
            "tool_input": {"file_path": "a.txt"}, "tool_response": {},
        })
        conn = self._conn()
        row = conn.execute(
            "SELECT repository_id FROM source_events WHERE event_type='tool_use'").fetchone()
        self.assertIsNotNone(row["repository_id"])

    def test_stop_reconciles_the_transcript_and_checkpoints(self):
        dest = Path(self.tmp.name) / "t.jsonl"
        dest.write_text((FIXTURES / "transcript" / "sample.jsonl").read_text())
        hooks_runtime.handle("session-start", {"session_id": "sess-1", "cwd": self.repo.name,
                                               "transcript_path": str(dest)})
        self.assertEqual(hooks_runtime.handle("stop", {
            "session_id": "sess-1", "cwd": self.repo.name, "transcript_path": str(dest),
            "stop_hook_active": False}), 0)
        conn = self._conn()
        self.assertGreater(conn.execute(
            "SELECT count(*) FROM source_events WHERE hook_event='transcript'").fetchone()[0], 0)
        row = conn.execute(
            "SELECT status FROM capture_checkpoints WHERE hook_event='stop'").fetchone()
        self.assertEqual(row["status"], "ok")

    def test_stop_honors_stop_hook_active(self):
        dest = Path(self.tmp.name) / "t.jsonl"
        dest.write_text((FIXTURES / "transcript" / "sample.jsonl").read_text())
        hooks_runtime.handle("session-start", {"session_id": "sess-1", "cwd": self.repo.name,
                                               "transcript_path": str(dest)})
        hooks_runtime.handle("stop", {"session_id": "sess-1", "transcript_path": str(dest),
                                      "stop_hook_active": True})
        conn = self._conn()
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM source_events WHERE hook_event='transcript'").fetchone()[0], 0)
        self.assertEqual(conn.execute(
            "SELECT status FROM capture_checkpoints WHERE hook_event='stop'").fetchone()[0], "skipped")

    def test_stop_folds_the_drop_counter_into_its_checkpoint(self):
        hooks_runtime.handle("session-start", {"session_id": "sess-1", "cwd": self.repo.name})
        hooks_runtime.handle("post-tool-use", json.loads(
            (FIXTURES / "hooks" / "post_tool_use_read.json").read_text()))
        drops = Path(self.tmp.name) / "state" / "drops-sess-1.json"
        self.assertTrue(drops.exists())
        hooks_runtime.handle("stop", {"session_id": "sess-1", "cwd": self.repo.name})
        conn = self._conn()
        detail = conn.execute(
            "SELECT detail FROM capture_checkpoints WHERE hook_event='stop'").fetchone()["detail"]
        self.assertIn("dropped=1", detail)
        self.assertIn("stage_a=ran", detail)         # Stage A landed in Task 26
        self.assertFalse(drops.exists())             # counter drained by the Stop

    def test_mid_session_hooks_keep_the_session_alive(self):
        # IMPORTANT 2: without touch(), last_seen_at only moved at
        # SessionStart/Stop, so a long turn could be swept mid-session.
        conn = self._conn(); m.apply_all(conn)
        S.upsert(conn, session_id="sess-1", occurred_at="2020-01-01T00:00:00.000Z")
        S.finalize_stale(conn, now="2020-01-02T00:00:00.000Z", idle_minutes=30)
        self.assertIsNotNone(conn.execute("SELECT ended_at FROM sessions").fetchone()[0])
        conn.close()

        hooks_runtime.handle("user-prompt-submit", {"session_id": "sess-1", "prompt": "go on"})
        conn = self._conn()
        row = conn.execute("SELECT ended_at, last_seen_at FROM sessions").fetchone()
        self.assertIsNone(row["ended_at"])                      # reopened by new evidence
        self.assertGreater(row["last_seen_at"], "2020-01-02T00:00:00.000Z")

        before = conn.execute("SELECT last_seen_at FROM sessions").fetchone()[0]
        conn.close()
        time.sleep(0.002)
        hooks_runtime.handle("post-tool-use", {
            "session_id": "sess-1", "tool_name": "Write", "tool_use_id": "toolu_1",
            "tool_input": {"file_path": "a.txt"}, "tool_response": {}})
        conn = self._conn()
        self.assertGreater(conn.execute("SELECT last_seen_at FROM sessions").fetchone()[0], before)

    def test_stop_notes_a_pending_git_backlog(self):
        config.save({**config.load(), "git": {"subprocess_timeout_seconds": 2,
                                              "max_commits_per_reconcile": 1}})
        Path(self.repo.name, "b.txt").write_text("second")
        sh(self.repo.name, "git", "add", ".")
        sh(self.repo.name, "git", "commit", "-q", "-m", "second commit")
        conn = self._conn(); m.apply_all(conn)
        S.upsert(conn, session_id="sess-1", cwd=self.repo.name,
                 occurred_at="1970-01-01T00:00:00.000Z")
        conn.close()
        hooks_runtime.handle("stop", {"session_id": "sess-1", "cwd": self.repo.name})
        conn = self._conn()
        detail = conn.execute(
            "SELECT detail FROM capture_checkpoints WHERE hook_event='stop'").fetchone()["detail"]
        self.assertIn("git_backlog=pending", detail)

    def test_session_end_finalizes_and_marks(self):
        hooks_runtime.handle("session-start", {"session_id": "sess-1", "cwd": self.repo.name})
        self.assertEqual(hooks_runtime.handle(
            "session-end", {"session_id": "sess-1", "reason": "clear"}), 0)
        conn = self._conn()
        row = conn.execute("SELECT ended_at, end_reason FROM sessions").fetchone()
        self.assertIsNotNone(row["ended_at"])
        self.assertEqual(row["end_reason"], "clear")
        end_row = conn.execute(
            "SELECT repository_id FROM source_events WHERE event_type='session_end'").fetchone()
        self.assertIsNotNone(end_row["repository_id"])   # inherited from the session row

    def test_handlers_never_raise_when_the_database_is_unusable(self):
        db = Path(self.tmp.name) / "work-ledger.sqlite"
        db.write_bytes(b"this is not a database")
        for event in ("session-start", "stop", "session-end"):
            self.assertEqual(hooks_runtime.handle(
                event, {"session_id": "sess-1", "cwd": self.repo.name}), 0)


class TestGitWatermarkState(unittest.TestCase):
    """`_reconcile_git_state` is a pure function: the whole state machine is
    exercised here without a git repository or a database."""

    def batch(self, *stamps):
        return [{"authored_at": s} for s in stamps]

    def test_short_window_collapses_to_steady_state(self):
        state = S._reconcile_git_state(
            self.batch("2026-08-20T10:00:01Z"), 1,
            since="2026-08-20T09:00:00.000Z", upper=None, high=None, limit=2, base_limit=2)
        self.assertEqual(state["since"], "2026-08-20T10:00:01.000Z")
        self.assertIsNone(state["upper"])
        self.assertFalse(state["truncated"])

    def test_full_window_with_new_commits_starts_a_backfill(self):
        state = S._reconcile_git_state(
            self.batch("2026-08-20T10:00:09Z", "2026-08-20T10:00:08Z"), 2,
            since="2026-08-20T09:00:00.000Z", upper=None, high=None, limit=2, base_limit=2)
        self.assertTrue(state["truncated"])
        self.assertEqual(state["upper"], "2026-08-20T10:00:08.000Z")   # cursor moved older
        self.assertEqual(state["high"], "2026-08-20T10:00:09.000Z")
        self.assertEqual(state["limit"], 2)                            # no need to widen

    def test_stuck_cursor_mid_backfill_widens_the_window(self):
        state = S._reconcile_git_state(
            self.batch("2026-08-20T10:00:00Z", "2026-08-20T10:00:00Z"), 0,
            since="2026-08-20T09:00:00.000Z", upper="2026-08-20T10:00:00.000Z",
            high="2026-08-20T10:00:00.000Z", limit=2, base_limit=2)
        self.assertTrue(state["truncated"])
        self.assertEqual(state["limit"], 4)

    def test_full_window_of_known_commits_at_steady_state_settles(self):
        # The MEDIUM finding: at steady state (upper is None) a full window
        # in which nothing was new is the inclusive boundary re-match, not a
        # backlog -- collapse instead of escalating forever.
        prior_high = "2026-08-20T10:00:00.000Z"
        state = S._reconcile_git_state(
            self.batch("2026-08-20T10:00:00Z", "2026-08-20T10:00:00Z"), 0,
            since=prior_high, upper=None, high=prior_high, limit=2, base_limit=2)
        self.assertFalse(state["truncated"])
        self.assertIsNone(state["upper"])
        self.assertEqual(state["limit"], 2)
        self.assertEqual(state["since"], prior_high)
        # and it is a fixed point: feeding it back changes nothing
        self.assertEqual(S._reconcile_git_state(
            self.batch("2026-08-20T10:00:00Z", "2026-08-20T10:00:00Z"), 0,
            since=state["since"], upper=state["upper"], high=state["high"],
            limit=state["limit"], base_limit=2), state)

    def test_a_cluster_too_large_to_widen_past_stops_rather_than_spinning(self):
        state = S._reconcile_git_state(
            self.batch("2026-08-20T10:00:00Z"), 0,
            since="2026-08-20T09:00:00.000Z", upper="2026-08-20T10:00:00.000Z",
            high="2026-08-20T10:00:00.000Z",
            limit=S._GIT_ABSOLUTE_MAX_COMMITS, base_limit=2)
        self.assertFalse(state["truncated"])
        self.assertEqual(state["limit"], 2)


class TestHookBlocks(unittest.TestCase):
    """A budget-deferred block is normal and self-healing; only a block that
    raised makes the checkpoint 'partial'."""

    def test_deferred_alone_keeps_the_checkpoint_ok(self):
        blocks = hooks_runtime._Blocks()
        expired = hooks_runtime._Budget(seconds=-1)
        self.assertIsNone(hooks_runtime._guarded("late", expired, lambda: 1, blocks))
        self.assertEqual(blocks.status(), "ok")
        self.assertEqual(blocks.notes(), ["deferred=late"])

    def test_a_raising_block_is_partial_and_named(self):
        blocks = hooks_runtime._Blocks()
        budget = hooks_runtime._Budget()

        def boom():
            raise RuntimeError("nope")

        self.assertEqual(hooks_runtime._guarded("risky", budget, boom, blocks, default=0), 0)
        self.assertEqual(blocks.status(), "partial")
        self.assertEqual(blocks.notes(), ["failed_blocks=risky"])


class TestSessionCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name
    def tearDown(self):
        del os.environ["WORK_LEDGER_HOME"]; self.tmp.cleanup()

    def test_reports_sweep_stale_sessions(self):
        # IMPORTANT 1 / DESIGN G1: the sweep rides every read path, not just
        # SessionStart, so a crashed session does not stay "live" forever.
        from work_ledger import cli
        conn = dbc.connect(); m.apply_all(conn)
        S.upsert(conn, session_id="crashed", occurred_at="2020-01-01T00:00:00.000Z")
        conn.close()
        self.assertEqual(cli.main(["today", "--format", "json"]), 0)
        conn = dbc.connect()
        row = conn.execute("SELECT ended_at, end_reason FROM sessions").fetchone()
        self.assertIsNotNone(row["ended_at"])
        self.assertEqual(row["end_reason"], "idle_finalized")

    def test_sessions_listing_sweeps_before_rendering(self):
        # `worklog sessions` renders ended_at/end_reason verbatim, so without
        # the sweep it shows a crashed session as [live] indefinitely.
        from work_ledger import cli
        conn = dbc.connect(); m.apply_all(conn)
        S.upsert(conn, session_id="crashed", occurred_at="2020-01-01T00:00:00.000Z")
        conn.close()
        self.assertEqual(cli.main(["sessions", "--format", "json"]), 0)
        conn = dbc.connect()
        self.assertEqual(conn.execute(
            "SELECT end_reason FROM sessions").fetchone()[0], "idle_finalized")

    def test_evidence_and_inferences_sweep_too(self):
        from work_ledger import cli
        conn = dbc.connect(); m.apply_all(conn)
        S.upsert(conn, session_id="crashed", occurred_at="2020-01-01T00:00:00.000Z")
        conn.close()
        self.assertEqual(cli.main(["inferences", "--format", "json"]), 0)
        conn = dbc.connect()
        self.assertEqual(conn.execute(
            "SELECT end_reason FROM sessions").fetchone()[0], "idle_finalized")

    def test_a_report_still_runs_when_the_sweep_cannot(self):
        # The sweep is bookkeeping the user did not ask for: on an
        # un-migrated database it must not break the command.
        from work_ledger import cli
        self.assertEqual(cli.main(["doctor", "--format", "json"]), 0)

    def test_sessions_and_reprocess_subcommands(self):
        from work_ledger import cli
        conn = dbc.connect(); m.apply_all(conn)
        dest = Path(self.tmp.name) / "t.jsonl"
        dest.write_text((FIXTURES / "transcript" / "sample.jsonl").read_text())
        S.upsert(conn, session_id="s1", transcript_path=str(dest),
                 occurred_at="2026-08-30T12:00:00.000Z")
        conn.close()
        self.assertEqual(cli.main(["sessions", "--limit", "5", "--format", "json"]), 0)
        self.assertEqual(cli.main(["sessions"]), 0)                        # text rendering
        self.assertEqual(cli.main(["reprocess", "nope", "--format", "json"]), 1)
        self.assertEqual(cli.main(["reprocess", "s1", "--format", "json"]), 0)
        self.assertEqual(cli.main(["reprocess"]), 2)          # missing session id
