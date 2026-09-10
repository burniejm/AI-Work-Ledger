import json, os, sqlite3, tempfile, time, unittest
from pathlib import Path
from work_ledger import hooks_runtime
from work_ledger.db import connection as dbc, migrations as m

FIX = Path(__file__).parent / "fixtures" / "hooks"

class TestHooksRuntime(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name
    def tearDown(self):
        del os.environ["WORK_LEDGER_HOME"]; self.tmp.cleanup()

    def test_returns_zero_on_valid_payload(self):
        payload = json.loads((FIX / "post_tool_use_bash_commit.json").read_text())
        self.assertEqual(hooks_runtime.handle("post-tool-use", payload), 0)

    def test_returns_zero_on_garbage_and_logs(self):
        self.assertEqual(hooks_runtime.handle("post-tool-use", {"nonsense": True}), 0)
        self.assertEqual(hooks_runtime.handle("not-an-event", {}), 0)
        log = Path(self.tmp.name) / "logs" / "hooks.log"
        self.assertTrue(log.exists())

    def test_never_raises_even_when_the_database_is_unusable(self):
        db = Path(self.tmp.name) / "work-ledger.sqlite"
        db.write_bytes(b"this is not a database")
        payload = json.loads((FIX / "post_tool_use_bash_commit.json").read_text())
        self.assertEqual(hooks_runtime.handle("post-tool-use", payload), 0)

    def test_nested_sentinel_short_circuits(self):
        os.environ["WORK_LEDGER_NESTED"] = "1"
        try:
            payload = json.loads((FIX / "post_tool_use_bash_commit.json").read_text())
            self.assertEqual(hooks_runtime.handle("post-tool-use", payload), 0)
            conn = dbc.connect(str(Path(self.tmp.name) / "work-ledger.sqlite")); m.apply_all(conn)
            self.assertEqual(conn.execute("SELECT count(*) FROM source_events").fetchone()[0], 0)
        finally:
            del os.environ["WORK_LEDGER_NESTED"]

    def test_post_tool_use_is_fast(self):
        payload = json.loads((FIX / "post_tool_use_bash_commit.json").read_text())
        hooks_runtime.handle("post-tool-use", payload)          # warm the schema
        t0 = time.monotonic()
        for i in range(10):
            p = dict(payload); p["tool_use_id"] = f"toolu_{i}"
            hooks_runtime.handle("post-tool-use", p)
        self.assertLess((time.monotonic() - t0) / 10, 0.2)      # < 200 ms per call

    def test_bash_git_commit_emits_a_git_commit_signal_event(self):
        # Ruling 30(g): DESIGN §4.2 mandates a derived kind=git_commit_signal row.
        payload = json.loads((FIX / "post_tool_use_bash_commit.json").read_text())
        hooks_runtime.handle("post-tool-use", payload)
        hooks_runtime.handle("post-tool-use", payload)          # replay: still one of each
        conn = dbc.connect(str(Path(self.tmp.name) / "work-ledger.sqlite"))
        rows = list(conn.execute("SELECT event_type, count(*) c FROM source_events GROUP BY event_type"))
        counts = {r["event_type"]: r["c"] for r in rows}
        self.assertEqual(counts.get("tool_use"), 1)
        self.assertEqual(counts.get("git_commit_signal"), 1)
        read = json.loads((FIX / "post_tool_use_read.json").read_text())
        hooks_runtime.handle("post-tool-use", read)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM source_events WHERE event_type='git_commit_signal'").fetchone()[0], 1)

    def test_two_subagent_stop_events_create_two_rows(self):
        # IMPORTANT 2: a content-hash discriminator would collapse two
        # identical SubagentStop payloads (no tool_use_id) into one row.
        payload = {"session_id": "sess-1", "transcript_path": "/tmp/t.jsonl",
                   "hook_event_name": "SubagentStop"}
        hooks_runtime.handle("subagent-stop", payload)
        time.sleep(0.002)
        hooks_runtime.handle("subagent-stop", dict(payload))
        conn = dbc.connect(str(Path(self.tmp.name) / "work-ledger.sqlite"))
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM source_events WHERE event_type='subagent_stop'").fetchone()[0], 2)

    def test_same_prompt_submitted_twice_creates_two_rows(self):
        # IMPORTANT 2: identical prompt text submitted twice in one session
        # must not collapse into a single row.
        payload = {"session_id": "sess-1", "hook_event_name": "UserPromptSubmit",
                   "prompt": "do the thing"}
        hooks_runtime.handle("user-prompt-submit", payload)
        time.sleep(0.002)
        hooks_runtime.handle("user-prompt-submit", dict(payload))
        conn = dbc.connect(str(Path(self.tmp.name) / "work-ledger.sqlite"))
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM source_events WHERE event_type='prompt'").fetchone()[0], 2)

    def test_dropped_events_create_no_checkpoint_rows(self):
        # IMPORTANT 5: a dropped (noise) PostToolUse event must not grow the
        # append-only capture_checkpoints table at full tool-call rate.
        read = json.loads((FIX / "post_tool_use_read.json").read_text())
        hooks_runtime.handle("post-tool-use", read)
        conn = dbc.connect(str(Path(self.tmp.name) / "work-ledger.sqlite")); m.apply_all(conn)
        self.assertEqual(conn.execute("SELECT count(*) FROM capture_checkpoints").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT count(*) FROM source_events").fetchone()[0], 0)

    def test_garbage_payloads_create_no_source_events_rows(self):
        # IMPORTANT 6: a payload missing session_id (or, for post-tool-use,
        # tool_name) must be rejected before anything touches the database.
        self.assertEqual(hooks_runtime.handle("post-tool-use", {}), 0)
        self.assertEqual(hooks_runtime.handle("post-tool-use", {"nonsense": True}), 0)
        db_path = Path(self.tmp.name) / "work-ledger.sqlite"
        if not db_path.exists():
            return  # nothing was ever touched -- trivially zero rows
        conn = dbc.connect(str(db_path)); m.apply_all(conn)
        self.assertEqual(conn.execute("SELECT count(*) FROM source_events").fetchone()[0], 0)
