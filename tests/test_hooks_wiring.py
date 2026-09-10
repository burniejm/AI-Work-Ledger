import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "hooks"

# Ruling 25: exactly these seven events are registered in v1.
EXPECTED_EVENTS = {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop",
                   "SubagentStop", "PreCompact", "SessionEnd"}
DROPPED_IN_V1 = {"PostToolUseFailure", "PostCompact"}
EXPECTED_SUBCOMMANDS = {"session-start", "user-prompt-submit", "post-tool-use", "stop",
                        "subagent-stop", "pre-compact", "session-end"}

def _cfg():
    return json.loads((HOOKS / "hooks.json").read_text())

class TestHookWiring(unittest.TestCase):
    def test_only_verified_events_are_registered(self):
        # Ruling 25 / SPEC §23.2
        self.assertEqual(set(_cfg()["hooks"]), EXPECTED_EVENTS)
        for dropped in DROPPED_IN_V1:
            self.assertNotIn(dropped, _cfg()["hooks"])

    def test_no_shim_scripts_exist(self):
        # Ruling 26: hooks/ holds hooks.json and nothing else.
        self.assertEqual(sorted(p.name for p in HOOKS.iterdir()), ["hooks.json"])

    def test_every_command_invokes_bin_worklog_hook_with_a_real_subcommand(self):
        seen = set()
        for event, groups in _cfg()["hooks"].items():
            for g in groups:
                for h in g["hooks"]:
                    cmd = h["command"]
                    self.assertTrue(
                        cmd.startswith('"${CLAUDE_PLUGIN_ROOT}"/bin/worklog hook '),
                        f"{event}: unexpected command {cmd!r}")
                    sub = cmd.rsplit(" ", 1)[1]
                    seen.add(sub)
                    self.assertEqual(h["type"], "command")
        self.assertEqual(seen, EXPECTED_SUBCOMMANDS)
        entry = ROOT / "bin" / "worklog"
        self.assertTrue(entry.exists())
        self.assertTrue(os.access(entry, os.X_OK), "bin/worklog is not executable")

    def test_post_tool_use_is_async_and_stop_is_bounded(self):
        cfg = _cfg()
        self.assertTrue(cfg["hooks"]["PostToolUse"][0]["hooks"][0]["async"])
        self.assertTrue(cfg["hooks"]["UserPromptSubmit"][0]["hooks"][0]["async"])
        self.assertTrue(cfg["hooks"]["SubagentStop"][0]["hooks"][0]["async"])
        self.assertLessEqual(cfg["hooks"]["Stop"][0]["hooks"][0]["timeout"], 10)
        self.assertLessEqual(cfg["hooks"]["SessionStart"][0]["hooks"][0]["timeout"], 10)
        self.assertLessEqual(cfg["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"], 1)

    def test_every_registered_command_exits_zero_on_garbage_stdin(self):
        env = {**os.environ, "WORK_LEDGER_HOME": tempfile.mkdtemp(), "CLAUDE_PLUGIN_ROOT": str(ROOT)}
        env.pop("WORK_LEDGER_NESTED", None)
        for sub in sorted(EXPECTED_SUBCOMMANDS):
            p = subprocess.run([sys.executable, str(ROOT / "bin" / "worklog"), "hook", sub],
                               input="not json", capture_output=True, text=True, env=env, timeout=20)
            self.assertEqual(p.returncode, 0, f"{sub} exited {p.returncode}: {p.stderr}")
            self.assertEqual(p.stdout, "", f"{sub} wrote to stdout")

    def test_hook_exits_zero_immediately_when_nested(self):
        env = {**os.environ, "WORK_LEDGER_NESTED": "1", "CLAUDE_PLUGIN_ROOT": str(ROOT),
               "WORK_LEDGER_HOME": tempfile.mkdtemp()}
        p = subprocess.run([sys.executable, str(ROOT / "bin" / "worklog"), "hook", "post-tool-use"],
                           input="{}", capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, "")
        # the sentinel short-circuits BEFORE the data directory is created
        self.assertFalse((Path(env["WORK_LEDGER_HOME"]) / "work-ledger.sqlite").exists())

    def test_hook_writes_a_source_event_on_a_real_payload(self):
        home = tempfile.mkdtemp()
        env = {**os.environ, "WORK_LEDGER_HOME": home, "CLAUDE_PLUGIN_ROOT": str(ROOT)}
        env.pop("WORK_LEDGER_NESTED", None)
        payload = (Path(__file__).parent / "fixtures" / "hooks" /
                   "post_tool_use_bash_commit.json").read_text()
        p = subprocess.run([sys.executable, str(ROOT / "bin" / "worklog"), "hook", "post-tool-use"],
                           input=payload, capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(p.returncode, 0)
        import sqlite3
        c = sqlite3.connect(str(Path(home) / "work-ledger.sqlite"))
        self.assertGreaterEqual(c.execute("SELECT count(*) FROM source_events").fetchone()[0], 1)
