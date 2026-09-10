import json, os, tempfile, time, unittest
from pathlib import Path
from work_ledger.db import connection as dbc, migrations as m
from work_ledger.capture import checkpoints, events, sessions as S

FIX = Path(__file__).parent / "fixtures" / "hooks"

class TestEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite")); m.apply_all(self.conn)
    def tearDown(self):
        del os.environ["WORK_LEDGER_HOME"]; self.tmp.cleanup()

    def test_replay_creates_exactly_one_row(self):
        key = events.idempotency_key("sess-1", "post_tool_use", "toolu_01ABC")
        for _ in range(3):
            events.append_source_event(self.conn, session_id="sess-1", event_type="tool_use",
                                       occurred_at="2026-08-30T12:00:00.000Z", payload={"a": 1}, idem=key)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM source_events").fetchone()[0], 1)

    def test_session_row_id_is_populated_when_the_session_already_exists(self):
        # Minor #8 (final review): previously always NULL.
        row_id = S.upsert(self.conn, session_id="sess-1", occurred_at="2026-08-30T12:00:00.000Z")
        events.append_source_event(
            self.conn, session_id="sess-1", event_type="tool_use",
            occurred_at="2026-08-30T12:05:00.000Z", payload={"a": 1},
            idem=events.idempotency_key("sess-1", "post_tool_use", "x"),
        )
        row = self.conn.execute(
            "SELECT session_row_id FROM source_events WHERE session_id='sess-1'"
        ).fetchone()
        self.assertEqual(row["session_row_id"], row_id)

    def test_session_row_id_is_null_when_no_session_row_exists_yet(self):
        # No sessions.upsert() call for this session_id -- append_source_event
        # never creates one; NULL is the honest answer, not a crash.
        events.append_source_event(
            self.conn, session_id="unknown-session", event_type="tool_use",
            occurred_at="2026-08-30T12:05:00.000Z", payload={"a": 1},
            idem=events.idempotency_key("unknown-session", "post_tool_use", "x"),
        )
        row = self.conn.execute(
            "SELECT session_row_id FROM source_events WHERE session_id='unknown-session'"
        ).fetchone()
        self.assertIsNone(row["session_row_id"])

    def test_payload_is_redacted_and_records_pattern_names(self):
        payload = json.loads((FIX / "post_tool_use_secret.json").read_text())
        events.append_source_event(self.conn, session_id="s", event_type="tool_use",
                                   occurred_at="2026-08-30T12:00:00.000Z", payload=payload,
                                   idem=events.idempotency_key("s", "post_tool_use", "x"))
        row = self.conn.execute("SELECT payload_json, redaction_json FROM source_events").fetchone()
        self.assertNotIn("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", row["payload_json"])
        self.assertIn("github_token", row["redaction_json"])

    def test_payload_is_size_capped_and_leaves_are_excerpt_truncated(self):
        big = {"stdout": "x" * 100_000}
        events.append_source_event(self.conn, session_id="s", event_type="tool_use",
                                   occurred_at="2026-08-30T12:00:00.000Z", payload=big,
                                   idem=events.idempotency_key("s", "k", "big"))
        row = self.conn.execute("SELECT payload_json, payload_bytes FROM source_events").fetchone()
        self.assertLessEqual(row["payload_bytes"], 8192)
        # Ruling 29: capture.excerpt_max_chars (512) bounds every individual string leaf.
        stored = json.loads(row["payload_json"])["stdout"]
        self.assertLessEqual(len(stored), 512 + len("…[truncated]"))
        self.assertTrue(stored.endswith("…[truncated]"))

    def test_noise_filter(self):
        commit = json.loads((FIX / "post_tool_use_bash_commit.json").read_text())
        read = json.loads((FIX / "post_tool_use_read.json").read_text())
        self.assertTrue(events.is_interesting(commit["tool_name"], commit["tool_input"], commit["tool_response"])[0])
        self.assertFalse(events.is_interesting(read["tool_name"], read["tool_input"], read["tool_response"])[0])
        self.assertTrue(events.is_interesting("Write", {"file_path": "a.go"}, {})[0])
        self.assertFalse(events.is_interesting("Bash", {"command": "git status"}, {"exit_code": 0})[0])
        self.assertTrue(events.is_interesting("Bash", {"command": "npm test"}, {"exit_code": 1, "stderr": "1 failing"})[0])

    def test_noise_filter_keeps_compound_commands_matching_a_keep_shape(self):
        # Ruled minor 7: keep-shapes are checked before the read-only drop
        # regex, so "cat x && npm test" is kept rather than dropped just
        # because it happens to start with "cat".
        self.assertTrue(events.is_interesting(
            "Bash", {"command": "cat x && npm test"}, {"exit_code": 0})[0])

    def test_cap_payload_bytes_handles_many_leaves_quickly_and_within_budget(self):
        # CRITICAL 1: the old implementation re-encoded the whole remaining
        # payload on every single leaf deletion (O(n^2)); 4000 leaves took
        # ~60s and still didn't reliably land under the cap. The rewrite
        # sizes every leaf once and deletes largest-first in one pass.
        payload = {f"key_{i}": "v" * 100 for i in range(4000)}
        t0 = time.monotonic()
        capped = events._cap_payload_bytes(payload, 8192)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0)
        self.assertLessEqual(len(events._canonical_bytes(capped)), 8192)

    def test_drop_list_comes_only_from_config(self):
        # Ruling 24: WebFetch/WebSearch/LS are noise because DEFAULTS says so, and a
        # narrowed config value widens capture rather than being unioned with a
        # hard-coded list.
        from work_ledger import config, paths
        self.assertEqual(config.DEFAULTS["capture"]["noise_tools"],
                         ["Read", "Glob", "Grep", "TodoWrite", "NotebookRead",
                          "WebFetch", "WebSearch", "LS"])
        for tool in ("WebFetch", "WebSearch", "LS"):
            self.assertFalse(events.is_interesting(tool, {}, {"exit_code": 0})[0])
        paths.ensure_dirs()
        paths.config_path().write_text("capture:\n  noise_tools:\n    - Read\n")
        self.assertTrue(events.is_interesting("WebFetch", {}, {"exit_code": 0})[0])
        self.assertFalse(events.is_interesting("Read", {"file_path": "a"}, {"exit_code": 0})[0])

    def test_many_leaf_payload_is_capped_quickly_end_to_end(self):
        payload = {f"key_{i}": "v" * 40 for i in range(4000)}
        idem = events.idempotency_key("s", "manyleaf", "x")
        t0 = time.monotonic()
        events.append_source_event(self.conn, session_id="s", event_type="tool_use",
                                   occurred_at="2026-08-30T12:00:00.000Z", payload=payload, idem=idem)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0)
        row = self.conn.execute(
            "SELECT payload_bytes FROM source_events WHERE idempotency_key=?", (idem,)
        ).fetchone()
        self.assertLessEqual(row["payload_bytes"], 8192)


class TestHookModeThreading(unittest.TestCase):
    """IMPORTANT 3: hook_mode must reach connection.transaction (via
    entities.current_device) from both capture.events.append_source_event
    and capture.checkpoints.checkpoint, so a hook write gives up on lock
    contention within the hook budget instead of the ~3s+ non-hook budget."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name
        self.db_path = os.path.join(self.tmp.name, "t.sqlite")
        setup_conn = dbc.connect(self.db_path)
        m.apply_all(setup_conn)
        setup_conn.close()
        # Hold the write lock from a separate connection so any BEGIN
        # IMMEDIATE issued by the code under test is guaranteed to contend.
        self.holder = dbc.connect(self.db_path)
        self.holder.execute("BEGIN IMMEDIATE")
        self._orig_timeout = dbc.HOOK_BUSY_TIMEOUT_MS
        self._orig_delays = dbc.HOOK_RETRY_DELAYS_MS
        dbc.HOOK_BUSY_TIMEOUT_MS = 50
        dbc.HOOK_RETRY_DELAYS_MS = (10, 10)

    def tearDown(self):
        dbc.HOOK_BUSY_TIMEOUT_MS = self._orig_timeout
        dbc.HOOK_RETRY_DELAYS_MS = self._orig_delays
        self.holder.rollback()
        del os.environ["WORK_LEDGER_HOME"]
        self.tmp.cleanup()

    def test_checkpoint_hook_mode_gives_up_fast(self):
        conn = dbc.connect(self.db_path, hook_mode=True)  # picks up the patched busy_timeout
        t0 = time.monotonic()
        with self.assertRaises(dbc.DatabaseBusy):
            checkpoints.checkpoint(
                conn, session_id="s", hook_event="post_tool_use", key="k-hookmode",
                status="ok", started_at="2026-08-30T12:00:00.000Z", hook_mode=True,
            )
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_append_source_event_hook_mode_gives_up_fast(self):
        conn = dbc.connect(self.db_path, hook_mode=True)
        t0 = time.monotonic()
        with self.assertRaises(dbc.DatabaseBusy):
            events.append_source_event(
                conn, session_id="s", event_type="tool_use",
                occurred_at="2026-08-30T12:00:00.000Z", payload={"a": 1},
                idem=events.idempotency_key("s", "k", "hookmode"), hook_mode=True,
            )
        self.assertLess(time.monotonic() - t0, 1.0)
