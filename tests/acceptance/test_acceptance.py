"""Acceptance suite for Work Ledger v1 -- Spec Section 20, criteria 1-24.

Task 39. Every test method here is named `test_ac<NN>_...` and maps to one
numbered criterion in Section 20. See docs/ACCEPTANCE.md for the full
criterion -> test map, including the two criteria (13, 15) that are
necessarily verified against a stub rather than a live Jira/Keychain.

Driven end to end through the public surfaces only: `hooks_runtime.handle`
(the same dispatch `worklog hook <event>` uses -- Ruling 26 removed the
shim layer so there is nothing else to call), and the `worklog` CLI via
`cli.main`. Fixture setup (creating a client/work item/remaining item
before exercising the behaviour under test) uses the same module-level
helpers every other test file in this suite uses to build fixtures --
that is the established convention here, not a shortcut around the
criterion itself. No test spawns the real `claude` binary (Stage B uses
the stub at tests/fixtures/bin/claude, exactly like test_stage_b.py) and
no test reaches a real network host (every Jira interaction talks to a
loopback http.server instance this file starts itself).
"""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from work_ledger import cli
from work_ledger import config as config_mod
from work_ledger import connections as connections_mod
from work_ledger import entities
from work_ledger import hooks_runtime
from work_ledger import paths
from work_ledger import remaining
from work_ledger import timeutil
from work_ledger import work_items as wi
from work_ledger.capture import events as ev
from work_ledger.capture import sessions as S
from work_ledger.db import connection as dbc
from work_ledger.db import migrations as m

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "work_ledger"
HOOK_FIX = Path(__file__).resolve().parents[1] / "fixtures" / "hooks"
STUB_CLAUDE_DIR = str(Path(__file__).resolve().parents[1] / "fixtures" / "bin")

def _run_cli(argv):
    """Invoke `cli.main` in-process, capturing stdout -- the same entry
    point `bin/worklog` calls. Returns (returncode, stdout_text)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(argv)
    return rc, buf.getvalue()

def _run_cli_json(argv):
    rc, out = _run_cli(argv)
    return rc, json.loads(out)

def _concurrent_writer(path, session, n):
    """Top-level (picklable) multiprocessing target for test_ac11 -- one OS
    process hammering `append_source_event` against a shared SQLite file.
    Mirrors tests/test_concurrency.py's `writer` function."""
    import sys as _sys
    if str(SRC.parent) not in _sys.path:
        _sys.path.insert(0, str(SRC.parent))
    from work_ledger.db import connection as _dbc, migrations as _m
    from work_ledger.capture import events as _ev
    conn = _dbc.connect(path)
    _m.apply_all(conn)
    for i in range(n):
        _ev.append_source_event(
            conn, session_id=session, event_type="tool_use",
            occurred_at="2026-08-30T12:00:00.000Z", payload={"i": i},
            idem=_ev.idempotency_key(session, "t", str(i)))

def _git_repo(path):
    os.makedirs(path, exist_ok=True)
    for args in (["git", "init", "-q"],
                 ["git", "config", "user.email", "dev@example.com"],
                 ["git", "config", "user.name", "Test User"]):
        subprocess.run(args, cwd=path, check=True, capture_output=True)
    return path

ISSUE_TEMPLATE = {
    "id": "1", "key": "AB-000",
    "fields": {
        "summary": "placeholder", "updated": "2026-08-29T18:00:00.000+0000",
        "status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
        "resolution": None, "assignee": {"accountId": "acct-1", "displayName": "Jim"},
        "project": {"key": "SK"},
    },
}

def _issue(key, *, status="In Progress", category="indeterminate", resolution=None,
           summary="Upload recovery"):
    return {
        "id": key, "key": key,
        "fields": {
            "summary": summary, "updated": "2026-08-29T18:00:00.000+0000",
            "status": {"name": status, "statusCategory": {"key": category}},
            "resolution": {"name": resolution} if resolution else None,
            "assignee": {"accountId": "acct-1", "displayName": "Jim"},
            "project": {"key": "SK"},
        },
    }

class JiraHandler(BaseHTTPRequestHandler):
    """A minimal, configurable fake Jira REST endpoint (loopback only).
    Class attributes are reset by `_start_jira_server` before each use --
    the whole suite runs in one process, one server per test."""

    mode = "ok"           # ok | auth | down
    issue = None          # a single-issue payload, or None for ISSUE_TEMPLATE
    mutating_calls = []   # method names of any non-GET request ever received

    def log_message(self, *a):
        pass

    def _issue_payload(self):
        return self.issue or ISSUE_TEMPLATE

    def do_GET(self):
        if JiraHandler.mode == "auth":
            self.send_response(401); self.end_headers(); return
        if JiraHandler.mode == "down":
            self.send_response(503); self.end_headers(); return
        if "myself" in self.path:
            body = {"accountId": "acct-1", "displayName": "Jim", "emailAddress": "j@x"}
        elif "/issue/" in self.path:
            body = self._issue_payload()
        else:
            body = {"issues": [self._issue_payload()]}
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _reject_mutation(self):
        JiraHandler.mutating_calls.append(self.command)
        self.send_response(405)
        self.end_headers()

    do_POST = _reject_mutation
    do_PUT = _reject_mutation
    do_DELETE = _reject_mutation
    do_PATCH = _reject_mutation

class TestAcceptance(unittest.TestCase):
    """One isolated WORK_LEDGER_HOME per test (unittest gives each test
    method its own instance, so setUp/tearDown already provide that
    isolation -- the same idiom every other test file in this suite uses)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("WORK_LEDGER_HOME")
        self._old_nested = os.environ.pop("WORK_LEDGER_NESTED", None)
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name
        self.conn = dbc.connect()  # default path -- honours WORK_LEDGER_HOME
        m.apply_all(self.conn)
        self._env_cleanup = []
        self._cwd = os.getcwd()
        # Global Constraints: no real model call in any test. `Stop` always
        # spawns a DETACHED background `worklog stage-b` process (Ruling 8 --
        # the hook never waits on it), so merely calling the Stop hook could
        # otherwise shell out to a real `claude` on PATH from underneath a
        # test that never asked for Stage B at all. Disabling it here by
        # config (checked inside the spawned child, before it would ever look
        # for `claude`) makes every hook-driven test in this file inert to
        # Stage B regardless of the ambient PATH; test_ac07 explicitly
        # re-enables it with the stub `claude` on PATH, exactly like
        # tests/test_stage_b.py.
        config_mod.save({"stage_b": {"enabled": False}})

    def tearDown(self):
        os.chdir(self._cwd)
        self.conn.close()
        for name in self._env_cleanup:
            os.environ.pop(name, None)
        if self._old_home is None:
            os.environ.pop("WORK_LEDGER_HOME", None)
        else:
            os.environ["WORK_LEDGER_HOME"] = self._old_home
        if self._old_nested is not None:
            os.environ["WORK_LEDGER_NESTED"] = self._old_nested
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _set_env(self, name, value):
        os.environ[name] = value
        self._env_cleanup.append(name)

    def _start_jira_server(self, *, mode="ok", issue=None):
        JiraHandler.mode = mode
        JiraHandler.issue = issue
        JiraHandler.mutating_calls = []
        srv = HTTPServer(("127.0.0.1", 0), JiraHandler)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        return f"http://127.0.0.1:{srv.server_port}"

    def _rest_connection(self, name, base_url, *, token_env, ticket_patterns=("AB-*",)):
        self._set_env(token_env, "tok-" + name)
        return connections_mod.upsert_connection(
            self.conn, name=name, adapter="jira-rest", base_url=base_url,
            username="j@x", auth_method="cloud-api-token",
            credential_reference=f"env://{token_env}",
            ticket_patterns=list(ticket_patterns),
        )

    def _hook_session_start(self, session_id, cwd, *, transcript_path=None):
        payload = {"session_id": session_id, "cwd": cwd, "hook_event_name": "SessionStart",
                   "permission_mode": "default", "source": "startup"}
        if transcript_path:
            payload["transcript_path"] = transcript_path
        self.assertEqual(hooks_runtime.handle("session-start", payload), 0)

    def _hook_commit(self, session_id, cwd, ticket, *, tool_use_id, branch="main"):
        payload = {
            "session_id": session_id, "cwd": cwd, "hook_event_name": "PostToolUse",
            "permission_mode": "default", "tool_name": "Bash", "tool_use_id": tool_use_id,
            "git_branch": branch,
            "tool_input": {"command": f'git commit -m "{ticket} did the work"'},
            "tool_response": {"stdout": f"[{branch} a1b2c3d] {ticket} did the work\n 1 file changed",
                               "stderr": "", "exit_code": 0},
        }
        self.assertEqual(hooks_runtime.handle("post-tool-use", payload), 0)

    def _hook_stop(self, session_id, cwd):
        payload = {"session_id": session_id, "cwd": cwd, "hook_event_name": "Stop",
                   "permission_mode": "default", "stop_hook_active": False}
        self.assertEqual(hooks_runtime.handle("stop", payload), 0)

    def _hook_prompt(self, session_id, text):
        payload = {"session_id": session_id, "hook_event_name": "UserPromptSubmit", "prompt": text}
        self.assertEqual(hooks_runtime.handle("user-prompt-submit", payload), 0)

    def _activities_for_session(self):
        rows = self.conn.execute(
            "SELECT a.id, wi.external_key FROM activities a "
            "JOIN activity_evidence ae ON ae.activity_id = a.id "
            "JOIN source_events se ON se.id = ae.source_event_id "
            "LEFT JOIN work_items wi ON wi.id = a.work_item_id "
            "WHERE a.source='stage_a'"
        ).fetchall()
        return rows

    # ------------------------------------------------------------------
    # 1. Session touching two Jira issues -> two correctly scoped activities
    # ------------------------------------------------------------------

    def test_ac01_two_issues_two_activities(self):
        connections_mod.upsert_connection(self.conn, name="ac01-conn", adapter="jira-rest",
                                          ticket_patterns=["AB-*"])
        cwd = str(Path(self.tmp.name) / "repo")
        self._hook_session_start("sess-ac01", cwd)
        self._hook_commit("sess-ac01", cwd, "AB-201", tool_use_id="toolu_ac01_a")
        self._hook_commit("sess-ac01", cwd, "AB-202", tool_use_id="toolu_ac01_b")
        self._hook_stop("sess-ac01", cwd)

        rows = self.conn.execute(
            "SELECT wi.external_key, count(*) c FROM activities a "
            "JOIN work_items wi ON wi.id = a.work_item_id "
            "WHERE a.source='stage_a' GROUP BY wi.external_key ORDER BY wi.external_key"
        ).fetchall()
        keys = {r["external_key"] for r in rows}
        self.assertEqual(keys, {"AB-201", "AB-202"})
        total = self.conn.execute(
            "SELECT count(*) FROM activities WHERE source='stage_a'"
        ).fetchone()[0]
        self.assertEqual(total, 2)

    # ------------------------------------------------------------------
    # 2. Reprocessing the same session produces no duplicates
    # ------------------------------------------------------------------

    def test_ac02_reprocess_is_idempotent(self):
        connections_mod.upsert_connection(self.conn, name="ac02-conn", adapter="jira-rest",
                                          ticket_patterns=["AB-*"])
        cwd = str(Path(self.tmp.name) / "repo")
        self._hook_session_start("sess-ac02", cwd)
        self._hook_commit("sess-ac02", cwd, "AB-301", tool_use_id="toolu_ac02_a")
        self._hook_stop("sess-ac02", cwd)

        def counts():
            return (
                self.conn.execute("SELECT count(*) FROM source_events").fetchone()[0],
                self.conn.execute("SELECT count(*) FROM activities").fetchone()[0],
            )

        baseline = counts()
        for _ in range(3):
            rc, result = _run_cli_json(["reprocess", "sess-ac02", "--format", "json"])
            self.assertEqual(rc, 0, result)
            self.assertTrue(result["found"])
            self.assertEqual(counts(), baseline,
                              "reprocessing must not create duplicate events or activities")

    # ------------------------------------------------------------------
    # 3. A session crossing midnight reports under the correct workday
    # ------------------------------------------------------------------

    def test_ac03_midnight_rollover(self):
        connections_mod.upsert_connection(self.conn, name="ac03-conn", adapter="jira-rest",
                                          ticket_patterns=["AB-*"])
        cwd = str(Path(self.tmp.name) / "repo")
        # A holder rather than a strict iterator: every hook invocation (and
        # anything it calls internally, e.g. device/timestamp bookkeeping)
        # calls utc_now_iso() one or more times and must see the SAME "now"
        # within that one invocation -- exactly the assumption hooks_runtime
        # itself makes (`now = timeutil.utc_now_iso()` once, reused). Only
        # the value set immediately before each hook call matters.
        current = {"v": "2026-08-29T19:00:00.000Z"}

        def fake_now():
            return current["v"]

        with mock.patch("work_ledger.timeutil.utc_now_iso", side_effect=fake_now):
            current["v"] = "2026-08-29T19:00:00.000Z"    # session-start: Aug 29 14:00 CDT
            self._hook_session_start("sess-ac03", cwd)
            current["v"] = "2026-08-29T19:30:00.000Z"
            self._hook_prompt("sess-ac03", "Please fix AB-401 today")   # boosts attribution above the inbox
            current["v"] = "2026-08-29T20:00:00.000Z"    # commit 1: Aug 29 15:00 CDT
            self._hook_commit("sess-ac03", cwd, "AB-401", tool_use_id="toolu_ac03_a")
            current["v"] = "2026-08-30T06:00:00.000Z"
            self._hook_prompt("sess-ac03", "Now also fix AB-402")
            current["v"] = "2026-08-30T06:30:00.000Z"    # commit 2: Aug 30 01:30 CDT -> still Aug-29 workday
            self._hook_commit("sess-ac03", cwd, "AB-402", tool_use_id="toolu_ac03_b")
            current["v"] = "2026-08-30T06:35:00.000Z"    # stop
            self._hook_stop("sess-ac03", cwd)

        rc, aug29 = _run_cli_json(["day", "2026-08-29", "--format", "json"])
        self.assertEqual(rc, 0)
        keys_29 = {
            wi_row["work_item"]
            for c in aug29["clients"] for p in c["projects"] for wi_row in p["work_items"]
        }
        self.assertEqual(keys_29, {"AB-401", "AB-402"},
                          "both commits, including the 01:30-CDT one, belong to the Aug-29 workday")

        rc, aug30 = _run_cli_json(["day", "2026-08-30", "--format", "json"])
        self.assertEqual(rc, 0)
        self.assertEqual(aug30["clients"], [], "nothing should land on the calendar-day-after report")

    # ------------------------------------------------------------------
    # 4. "What remains on AB-76?" combines ledger + Jira status
    # ------------------------------------------------------------------

    def test_ac04_remaining_with_jira_status(self):
        base = self._start_jira_server(mode="ok", issue=_issue(
            "AB-76", status="In Progress", category="indeterminate"))
        cid = self._rest_connection("ac04-jira", base, token_env="WL_AC04_TOKEN")
        wid = wi.upsert_external(self.conn, cid, "AB-76", title="Upload recovery")
        remaining.create(self.conn, title="Retest on replay hardware",
                          normalized_key="ac04-retest-hardware", work_item_id=wid,
                          confidence="high", source="stage_a", occurred_at=timeutil.utc_now_iso())

        rc, refreshed = _run_cli_json(["jira", "refresh", "--connection", "ac04-jira",
                                       "--format", "json"])
        self.assertEqual(rc, 0)
        self.assertEqual(refreshed["updated"], 1)

        rc, rep = _run_cli_json(["status", "AB-76", "--format", "json"])
        self.assertEqual(rc, 0)
        self.assertEqual([i["title"] for i in rep["open_work"]], ["Retest on replay hardware"])
        self.assertEqual(rep["jira"][0]["status"], "In Progress")
        self.assertFalse(rep["jira"][0]["stale"])

    # ------------------------------------------------------------------
    # 5. Jira Done + open ledger work -> visible contradiction
    # ------------------------------------------------------------------

    def test_ac05_done_ticket_contradiction(self):
        base = self._start_jira_server(mode="ok", issue=_issue(
            "AB-76", status="Done", category="done", resolution="Fixed"))
        cid = self._rest_connection("ac05-jira", base, token_env="WL_AC05_TOKEN")
        wid = wi.upsert_external(self.conn, cid, "AB-76", title="Upload recovery")
        remaining.create(self.conn, title="Retest on hardware",
                          normalized_key="ac05-retest-hardware", work_item_id=wid,
                          confidence="high", source="stage_a", occurred_at=timeutil.utc_now_iso())

        rc, _ = _run_cli_json(["jira", "refresh", "--connection", "ac05-jira", "--format", "json"])
        self.assertEqual(rc, 0)

        rc, rep = _run_cli_json(["contradictions", "--format", "json"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(rep["items"]), 1)
        self.assertEqual(rep["items"][0]["rule"], "jira_done_open_remaining")
        self.assertIn("Done", rep["items"][0]["external_claim"])

    # ------------------------------------------------------------------
    # 6. Unticketed work creates or reuses an appropriate local work item
    # ------------------------------------------------------------------

    def test_ac06_local_work_item(self):
        rc, out = _run_cli(["add", "client=Acme project=Ops "
                             "Diagnosed the Ubuntu boot image on AMD hardware"])
        self.assertEqual(rc, 0)
        first_activity = out.strip()

        rc, out = _run_cli(["add", "client=Acme project=Ops "
                             "Diagnosing the Ubuntu boot image on the AMD hardware"])
        self.assertEqual(rc, 0)
        second_activity = out.strip()
        self.assertNotEqual(first_activity, second_activity)

        rows = self.conn.execute(
            "SELECT DISTINCT wi.id, wi.display_id, wi.kind FROM activities a "
            "JOIN work_items wi ON wi.id = a.work_item_id "
            "WHERE a.id IN (?, ?)", (first_activity, second_activity),
        ).fetchall()
        self.assertEqual(len(rows), 1, "both statements must resolve to the same local item")
        self.assertEqual(rows[0]["kind"], "local")
        self.assertTrue(rows[0]["display_id"].startswith("LOCAL-"))

    # ------------------------------------------------------------------
    # 7. Repeated remaining-work mentions reinforce rather than duplicate
    # ------------------------------------------------------------------

    def test_ac07_remaining_reinforced(self):
        config_mod.save({"stage_b": {"enabled": True}})  # setUp disables it by default
        old_path = os.environ["PATH"]
        os.environ["PATH"] = STUB_CLAUDE_DIR + os.pathsep + old_path
        self._set_env("STUB_CLAUDE_MODE", "ok")
        self.addCleanup(lambda: os.environ.__setitem__("PATH", old_path))

        S.upsert(self.conn, session_id="sess-ac07", cwd="/tmp/x",
                 occurred_at="2026-08-30T12:00:00.000Z")
        ev.append_source_event(self.conn, session_id="sess-ac07", event_type="commit",
                               occurred_at="2026-08-30T12:05:00.000Z",
                               payload={"sha": "a1", "subject": "AB-76 fix upload retry"},
                               idem=ev.idempotency_key("sess-ac07", "commit", "a1"))

        for _ in range(3):
            rc, result = _run_cli_json(["stage-b", "--session", "sess-ac07", "--force",
                                        "--format", "json"])
            self.assertEqual(rc, 0)
            self.assertTrue(result["ran"], result)

        rows = self.conn.execute(
            "SELECT title, mention_count FROM remaining_items"
        ).fetchall()
        self.assertEqual(len(rows), 1, "repeated mentions must reinforce one row, not duplicate")
        self.assertEqual(rows[0]["mention_count"], 3)

    # ------------------------------------------------------------------
    # 8. A correction updates subsequent reports and preserves audit history
    # ------------------------------------------------------------------

    def test_ac08_correction_updates_reports(self):
        def _summary_present(report, needle):
            return any(
                needle in (a.get("summary") or "")
                for c in report["recent"]["clients"] for p in c["projects"]
                for w in p["work_items"] for a in w["activities"]
            )

        rc, out = _run_cli(["add", "ticket=AB-501 Fixed the wrong-ticket bug"])
        self.assertEqual(rc, 0)
        activity_id = out.strip()
        wi.upsert_external(self.conn, "conn-ac08", "AB-502", title="The real ticket")

        rc, before = _run_cli_json(["status", "AB-501", "--format", "json"])
        self.assertEqual(rc, 0)
        self.assertTrue(_summary_present(before, "wrong-ticket bug"),
                         "before the correction, the activity must show up under AB-501")

        rc, out = _run_cli(["correct", "move-activity", "--activity", activity_id,
                            "--to-ticket", "AB-502", "--reason", "wrong ticket picked up"])
        self.assertEqual(rc, 0)
        group_id = out.strip()

        row = self.conn.execute(
            "SELECT wi.external_key FROM activities a JOIN work_items wi ON wi.id=a.work_item_id "
            "WHERE a.id=?", (activity_id,)).fetchone()
        self.assertEqual(row["external_key"], "AB-502")

        rc, hist = _run_cli_json(["correct", "history", "--table", "activities",
                                  "--id", activity_id, "--format", "json"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["reason"], "wrong ticket picked up")
        self.assertEqual(hist[0]["correction_group_id"], group_id)

        rc, after_501 = _run_cli_json(["status", "AB-501", "--format", "json"])
        self.assertEqual(rc, 0)
        self.assertFalse(_summary_present(after_501, "wrong-ticket bug"),
                          "after the correction, the activity must no longer show up under AB-501")

        rc, after_502 = _run_cli_json(["status", "AB-502", "--format", "json"])
        self.assertEqual(rc, 0)
        self.assertTrue(_summary_present(after_502, "wrong-ticket bug"),
                         "the corrected activity must show up under its new ticket")

    # ------------------------------------------------------------------
    # 9. Git or Jira failure does not lose Claude-session activity
    # ------------------------------------------------------------------

    def test_ac09_jira_outage_preserves_activity(self):
        # A connection pointed at a closed local port -- guaranteed to refuse
        # the connection immediately (Global Constraints: no network access
        # in any test; 127.0.0.1 with nothing listening never leaves the box).
        self._rest_connection("ac09-jira", "http://127.0.0.1:1", token_env="WL_AC09_TOKEN")

        cwd = str(Path(self.tmp.name) / "repo")
        self._hook_session_start("sess-ac09", cwd)   # session-start's own bounded Jira refresh fires here
        self._hook_commit("sess-ac09", cwd, "AB-900", tool_use_id="toolu_ac09_a")
        self._hook_stop("sess-ac09", cwd)

        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM source_events WHERE event_type='session_start'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM source_events WHERE event_type='git_commit_signal'").fetchone()[0], 1)
        row = self.conn.execute(
            "SELECT wi.external_key FROM activities a JOIN work_items wi ON wi.id=a.work_item_id "
            "WHERE a.source='stage_a'").fetchone()
        self.assertEqual(row["external_key"], "AB-900",
                          "the Claude-session activity must survive a Jira outage")
        health = self.conn.execute(
            "SELECT health_state FROM connections WHERE name='ac09-jira'").fetchone()[0]
        self.assertIn(health, ("degraded", "missing", "unknown"))

        # The "Git" half of the criterion: a real repository (so session-start
        # resolves a repository_id and Stop's `reconcile_git` genuinely
        # attempts a `git log`), with the underlying git call itself made to
        # fail (`gitevidence.commits_since` is the one seam a test can force
        # to raise without corrupting a real .git directory). The Stop hook's
        # `reconcile_git` block is independently guarded (Ruling 8) from the
        # Bash-tool-detected commit that actually attributes AB-901, so the
        # session's Claude activity must still land.
        git_cwd = str(Path(self.tmp.name) / "gitrepo")
        _git_repo(git_cwd)
        self._hook_session_start("sess-ac09b", git_cwd)
        with mock.patch("work_ledger.gitevidence.commits_since",
                        side_effect=RuntimeError("git boom")):
            self._hook_commit("sess-ac09b", git_cwd, "AB-901", tool_use_id="toolu_ac09_b")
            self._hook_stop("sess-ac09b", git_cwd)

        row2 = self.conn.execute(
            "SELECT wi.external_key FROM activities a JOIN work_items wi ON wi.id=a.work_item_id "
            "WHERE a.source='stage_a' AND wi.external_key='AB-901'").fetchone()
        self.assertIsNotNone(row2, "the Claude-session activity must survive a git failure")
        checkpoint = self.conn.execute(
            "SELECT status, detail FROM capture_checkpoints WHERE hook_event='stop' "
            "AND session_id='sess-ac09b' ORDER BY created_at DESC, id DESC LIMIT 1").fetchone()
        self.assertEqual(checkpoint["status"], "partial")
        self.assertIn("reconcile_git", checkpoint["detail"])

    # ------------------------------------------------------------------
    # 10. Low-confidence attribution stays out of reports, appears in inbox
    # ------------------------------------------------------------------

    def test_ac10_low_confidence_inbox(self):
        # Drives a real Stage A attribution into `low` rather than seeding
        # the row directly: a ticket key mentioned only in tool input/output
        # (no branch or prompt signal to back it) scores 24 points under
        # DESIGN §6 -- below the medium threshold -- so this is exactly the
        # weak-attribution shape the criterion describes, produced by the
        # real scoring code, not asserted by construction.
        connections_mod.upsert_connection(self.conn, name="ac10-conn", adapter="jira-rest",
                                          ticket_patterns=["AB-*"])
        cwd = str(Path(self.tmp.name) / "repo")
        self._hook_session_start("sess-ac10", cwd)
        self._hook_commit("sess-ac10", cwd, "AB-950", tool_use_id="toolu_ac10_a")
        self._hook_stop("sess-ac10", cwd)

        row = self.conn.execute(
            "SELECT confidence, in_inbox, confidence_reason FROM activities "
            "WHERE source='stage_a'").fetchone()
        self.assertEqual(row["confidence"], "low")
        self.assertEqual(row["in_inbox"], 1)

        rc, today = _run_cli_json(["today", "--format", "json"])
        self.assertEqual(rc, 0)
        self.assertEqual(today["clients"], [])
        self.assertGreaterEqual(today["excluded_low_confidence_count"], 1)

        rc, inbox = _run_cli_json(["inferences", "--format", "json"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(inbox["activities"]), 1)
        self.assertEqual(inbox["activities"][0]["confidence_reason"], row["confidence_reason"])
        self.assertIn("ticket key in tool input/output", row["confidence_reason"])

    # ------------------------------------------------------------------
    # 11. Concurrent Claude sessions do not corrupt the DB or lock unboundedly
    # ------------------------------------------------------------------

    def test_ac11_concurrent_sessions(self):
        # Reuses the Task 38 concurrency-hardening pattern (tests/test_concurrency.py):
        # N OS processes hammering append_source_event against the same
        # SQLite file concurrently, via the module-level `_concurrent_writer`
        # below (must be a top-level function -- multiprocessing's "spawn"
        # start method pickles the target, and a nested/local function is
        # not picklable).
        import multiprocessing as mp
        import sqlite3

        db_path = str(paths.db_path())

        baseline = self.conn.execute("SELECT count(*) FROM source_events").fetchone()[0]
        ctx = mp.get_context("spawn")
        procs = [ctx.Process(target=_concurrent_writer, args=(db_path, f"conc-{i}", 25))
                 for i in range(6)]
        t0 = time.monotonic()
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=120)
        elapsed = time.monotonic() - t0

        self.assertTrue(all(p.exitcode == 0 for p in procs), [p.exitcode for p in procs])
        self.assertLess(elapsed, 60, "concurrent writers must not deadlock / lock unboundedly")
        c = sqlite3.connect(db_path)
        self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(
            c.execute("SELECT count(*) FROM source_events").fetchone()[0], baseline + 150)

    # ------------------------------------------------------------------
    # 12. Full transcripts and source files are not copied into SQLite
    # ------------------------------------------------------------------

    def test_ac12_no_bulk_copy(self):
        sentinel = "ACCEPTANCE12FILLER" + ("Q" * 2_000_000)
        transcript_path = Path(self.tmp.name) / "big-transcript.jsonl"
        record = {
            "type": "user", "uuid": "rec-1", "parentUuid": None,
            "timestamp": "2026-08-30T12:00:00.000Z", "sessionId": "sess-ac12",
            "message": {"role": "user", "content": sentinel},
        }
        transcript_path.write_text(json.dumps(record) + "\n")
        transcript_bytes = transcript_path.stat().st_size
        self.assertGreater(transcript_bytes, 2_000_000)

        cwd = str(Path(self.tmp.name) / "repo")
        self._hook_session_start("sess-ac12", cwd, transcript_path=str(transcript_path))

        one_mb_file = "F" * 1_000_000
        payload = {
            "session_id": "sess-ac12", "cwd": cwd, "hook_event_name": "PostToolUse",
            "permission_mode": "default", "tool_name": "Write", "tool_use_id": "toolu_ac12_a",
            "tool_input": {"file_path": "big.txt", "content": one_mb_file},
            "tool_response": {"stdout": "", "stderr": "", "exit_code": 0},
        }
        self.assertEqual(hooks_runtime.handle("post-tool-use", payload), 0)
        self._hook_stop("sess-ac12", cwd)

        conn = dbc.connect()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        db_bytes = os.path.getsize(paths.db_path())

        self.assertLess(db_bytes, 2 * 1024 * 1024,
                         "the database must stay well under the 2 MB of raw input")
        self.assertLess(db_bytes, transcript_bytes)

        cap = config_mod.get("capture.payload_max_bytes", 8192)
        rows = self.conn.execute("SELECT payload_bytes, payload_json FROM source_events").fetchall()
        self.assertGreater(len(rows), 0)
        for row in rows:
            self.assertLessEqual(row["payload_bytes"], cap)
            self.assertNotIn(sentinel, row["payload_json"])

    # ------------------------------------------------------------------
    # 13. Setup detects and reuses an authenticated Jira MCP connection
    # ------------------------------------------------------------------

    def test_ac13_mcp_reuse(self):
        # STUB: verified against a fake ~/.claude.json + a disabled `claude
        # mcp list` call, not a live authenticated MCP session. See
        # docs/ACCEPTANCE.md for the manual live-verification procedure.
        fake_home = Path(self.tmp.name) / "fakehome"
        fake_home.mkdir()
        (fake_home / ".claude.json").write_text(json.dumps({
            "mcpServers": {
                "atlassian": {
                    "command": "npx", "args": ["-y", "@example/mcp-atlassian"],
                    "env": {"ATLASSIAN_API_TOKEN": "ATATT3xFfGF0SUPERSECRETVALUE0000"},
                    "tools": ["mcp__atlassian__getJiraIssue",
                              "mcp__atlassian__atlassianUserInfo",
                              "mcp__atlassian__searchJiraIssuesUsingJql"],
                }
            }
        }))
        dev_root = Path(self.tmp.name) / "dev"
        _git_repo(dev_root / "repo")
        answers_path = Path(self.tmp.name) / "answers.json"
        answers_path.write_text(json.dumps({
            "timezone": "America/Chicago", "rollover_hour": 3,
            "dev_roots": [str(dev_root)],
            "jira": {"mode": "mcp", "run_cli": False},
        }))

        with mock.patch("pathlib.Path.home", return_value=fake_home):
            rc, rep = _run_cli_json(["setup", "--non-interactive", "--answers",
                                     str(answers_path), "--format", "json"])
        self.assertEqual(rc, 0, rep)

        rows = connections_mod.list_connections(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["adapter"], "mcp")
        self.assertEqual(rows[0]["mcp_server_name"], "atlassian")
        self.assertEqual(rows[0]["health_state"], "healthy",
                          "capabilities discovered from the existing MCP config make it usable "
                          "without any new authentication step")

    # ------------------------------------------------------------------
    # 14. MCP credentials are not copied into the ledger
    # ------------------------------------------------------------------

    def test_ac14_no_mcp_credentials(self):
        secret = "ATATT3xFfGF0SUPERSECRETVALUE0000"
        fake_home = Path(self.tmp.name) / "fakehome"
        fake_home.mkdir()
        (fake_home / ".claude.json").write_text(json.dumps({
            "mcpServers": {
                "atlassian": {
                    "command": "npx",
                    "env": {"ATLASSIAN_API_TOKEN": secret},
                    "tools": ["mcp__atlassian__getJiraIssue",
                              "mcp__atlassian__atlassianUserInfo"],
                }
            }
        }))
        dev_root = Path(self.tmp.name) / "dev"
        _git_repo(dev_root / "repo")
        answers_path = Path(self.tmp.name) / "answers.json"
        answers_path.write_text(json.dumps({
            "timezone": "America/Chicago", "rollover_hour": 3,
            "dev_roots": [str(dev_root)],
            "jira": {"mode": "mcp", "run_cli": False},
        }))

        with mock.patch("pathlib.Path.home", return_value=fake_home):
            rc, _rep = _run_cli_json(["setup", "--non-interactive", "--answers",
                                      str(answers_path), "--format", "json"])
        self.assertEqual(rc, 0)

        self.conn.close()
        db_bytes = paths.db_path().read_bytes()
        self.assertNotIn(secret.encode(), db_bytes)
        cfg_text = paths.config_path().read_text() if paths.config_path().exists() else ""
        self.assertNotIn(secret, cfg_text)
        for p in Path(self.tmp.name).rglob("*"):
            if p.is_file() and p != (fake_home / ".claude.json"):
                self.assertNotIn(secret.encode(), p.read_bytes(), p)
        self.conn = dbc.connect()  # tearDown expects an open connection

    # ------------------------------------------------------------------
    # 15. Direct Jira credentials are stored using an OS credential provider
    # ------------------------------------------------------------------

    def test_ac15_credentials_in_keychain(self):
        # STUB: `credentials.KeychainStore` calls the absolute path
        # `/usr/bin/security` (see tests/test_credentials.py), so it cannot
        # be redirected via PATH -- `subprocess.run` is mocked instead,
        # exactly like tests/test_credentials.py's own TestKeychainStore.
        # This suite must not touch the developer's actual keychain (on
        # macOS or otherwise). See docs/ACCEPTANCE.md for the manual
        # verification procedure against a real keychain entry.
        #
        # Drives the REAL code path (setupwizard._store_token, Spec §12.4)
        # rather than pre-seeding a connection row by hand: a raw `token` is
        # handed to `worklog setup` alongside a `keychain://` reference, and
        # the assertions are on what that code actually did with it.
        token = "s3cr3t"
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            if len(argv) > 1 and argv[1] == "add-generic-password":
                return mock.Mock(returncode=0, stdout="", stderr="")
            if len(argv) > 1 and argv[1] == "find-generic-password":
                return mock.Mock(returncode=0, stdout=token + "\n", stderr="")
            return mock.Mock(returncode=1, stdout="", stderr="not found")

        dev_root = Path(self.tmp.name) / "dev"
        dev_root.mkdir()
        answers_path = Path(self.tmp.name) / "answers.json"
        answers_path.write_text(json.dumps({
            "timezone": "America/Chicago", "rollover_hour": 3,
            "dev_roots": [str(dev_root)],
            # base_url is a closed local port -- Global Constraints: no
            # network access in any test -- so the connection health check
            # `_configure_rest` runs afterward fails fast and locally; it is
            # irrelevant to what this criterion verifies (credential storage).
            "jira": {"mode": "rest", "name": "ac15-jira", "base_url": "http://127.0.0.1:1",
                     "username": "j@x", "credential_reference": "keychain://work-ledger/ac15",
                     "token": token},
        }))

        with mock.patch("subprocess.run", side_effect=fake_run):
            rc, rep = _run_cli_json(["setup", "--non-interactive", "--answers",
                                     str(answers_path), "--format", "json"])
        self.assertEqual(rc, 0, rep)

        add_calls = [c for c in calls if len(c) > 1 and c[1] == "add-generic-password"]
        self.assertEqual(len(add_calls), 1,
                          "setupwizard._store_token must call `security add-generic-password` "
                          "exactly once for a raw token paired with a keychain:// reference")
        self.assertEqual(add_calls[0][:2], ["/usr/bin/security", "add-generic-password"])

        row = self.conn.execute(
            "SELECT credential_reference FROM connections WHERE name='ac15-jira'").fetchone()
        self.assertEqual(row["credential_reference"], "keychain://work-ledger/ac15")

        self.conn.close()
        db_bytes = paths.db_path().read_bytes()
        self.assertNotIn(token.encode(), db_bytes, "the secret itself must never reach SQLite")
        self.conn = dbc.connect()  # tearDown expects an open connection

    # ------------------------------------------------------------------
    # 16. Multiple Jira sites can be configured independently
    # ------------------------------------------------------------------

    def test_ac16_multiple_sites(self):
        base_a = self._start_jira_server(mode="ok", issue=_issue("AB-1", status="In Progress"))
        cida = self._rest_connection("site-a", base_a, token_env="WL_AC16_A")
        wi.upsert_external(self.conn, cida, "AB-1", title="Site A work")

        # a second, independent server/site that is down -- must not affect site-a
        cidb = connections_mod.upsert_connection(
            self.conn, name="site-b", adapter="jira-rest", base_url="http://127.0.0.1:1",
            username="j@x", auth_method="cloud-api-token",
            credential_reference="env://WL_AC16_B", ticket_patterns=["VV-*"],
        )
        self._set_env("WL_AC16_B", "tok-b")
        wi.upsert_external(self.conn, cidb, "VV-1", title="Site B work")

        rc, out_a = _run_cli_json(["jira", "refresh", "--connection", "site-a", "--format", "json"])
        self.assertEqual(rc, 0)
        self.assertEqual(out_a["updated"], 1)

        rc, out_b = _run_cli_json(["jira", "refresh", "--connection", "site-b", "--format", "json"])
        self.assertEqual(rc, 0)
        self.assertEqual(out_b["updated"], 0)

        rc, connlist = _run_cli_json(["connections", "--format", "json"])
        self.assertEqual(rc, 0)
        by_name = {c["name"]: c for c in connlist}
        self.assertEqual(by_name["site-a"]["health_state"], "healthy")
        self.assertIn(by_name["site-b"]["health_state"], ("degraded", "missing", "unknown"))

    # ------------------------------------------------------------------
    # 17. Setup identifies the Jira and Git identities belonging to the user
    # ------------------------------------------------------------------

    def test_ac17_identities_detected(self):
        base = self._start_jira_server(mode="ok")
        self._set_env("WL_AC17_TOKEN", "tok")
        dev_root = Path(self.tmp.name) / "dev"
        repo = _git_repo(dev_root / "repo")
        os.chdir(repo)
        answers_path = Path(self.tmp.name) / "answers.json"
        answers_path.write_text(json.dumps({
            "timezone": "America/Chicago", "rollover_hour": 3,
            "dev_roots": [str(dev_root)],
            "jira": {"mode": "rest", "name": "ac17-jira", "base_url": base,
                     "username": "j@x", "credential_reference": "env://WL_AC17_TOKEN"},
        }))
        rc, rep = _run_cli_json(["setup", "--non-interactive", "--answers",
                                 str(answers_path), "--format", "json"])
        self.assertEqual(rc, 0, rep)

        git_values = {r[0] for r in self.conn.execute(
            "SELECT value FROM user_identities WHERE kind='git'")}
        self.assertIn("dev@example.com", git_values)

        jira_values = {r[0] for r in self.conn.execute(
            "SELECT value FROM user_identities WHERE kind='jira'")}
        self.assertIn("acct-1", jira_values)

    # ------------------------------------------------------------------
    # 18. Re-running setup does not duplicate connections/identities/repos
    # ------------------------------------------------------------------

    def test_ac18_setup_idempotent(self):
        base = self._start_jira_server(mode="ok")
        self._set_env("WL_AC18_TOKEN", "tok")
        dev_root = Path(self.tmp.name) / "dev"
        repo = _git_repo(dev_root / "repo")
        os.chdir(repo)
        answers_path = Path(self.tmp.name) / "answers.json"
        answers_path.write_text(json.dumps({
            "timezone": "America/Chicago", "rollover_hour": 3,
            "dev_roots": [str(dev_root)],
            "jira": {"mode": "rest", "name": "ac18-jira", "base_url": base,
                     "username": "j@x", "credential_reference": "env://WL_AC18_TOKEN"},
        }))

        rc, _rep = _run_cli_json(["setup", "--non-interactive", "--answers",
                                  str(answers_path), "--format", "json"])
        self.assertEqual(rc, 0)

        def counts():
            return tuple(
                self.conn.execute(
                    f"SELECT count(*) FROM {t} WHERE deleted_at IS NULL").fetchone()[0]
                for t in ("connections", "user_identities", "repositories")
            )

        before = counts()
        rc, _rep2 = _run_cli_json(["setup", "--non-interactive", "--answers",
                                   str(answers_path), "--format", "json"])
        self.assertEqual(rc, 0)
        self.assertEqual(counts(), before)

    # ------------------------------------------------------------------
    # 19. An expired Jira connection degrades gracefully, local capture continues
    # ------------------------------------------------------------------

    def test_ac19_expired_connection(self):
        base = self._start_jira_server(mode="auth")
        cid = self._rest_connection("ac19-jira", base, token_env="WL_AC19_TOKEN")
        wi.upsert_external(self.conn, cid, "AB-76", title="Upload recovery")

        rc, out = _run_cli_json(["jira", "refresh", "--connection", "ac19-jira", "--format", "json"])
        self.assertEqual(rc, 0)
        self.assertEqual(out["connections"]["ac19-jira"], "expired")

        row = self.conn.execute(
            "SELECT count(*) FROM work_items WHERE external_key='AB-76'").fetchone()[0]
        self.assertEqual(row, 1, "history must survive an expired connection")

        # local capture keeps working regardless
        cwd = str(Path(self.tmp.name) / "repo")
        self._hook_session_start("sess-ac19", cwd)
        self._hook_commit("sess-ac19", cwd, "AB-999", tool_use_id="toolu_ac19_a")
        self._hook_stop("sess-ac19", cwd)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM source_events WHERE event_type='git_commit_signal'"
        ).fetchone()[0], 1)

    # ------------------------------------------------------------------
    # 20. Backups and exports contain no secrets
    # ------------------------------------------------------------------

    def test_ac20_no_secrets_in_exports(self):
        secret_marker = "ATATT3xFfGF0T4Jw9xQpLm2Zk8vRt6YbNc1Ud0Ha"
        self._set_env("WL_AC20_TOKEN", secret_marker)
        cid = connections_mod.upsert_connection(
            self.conn, name="ac20-jira", adapter="jira-rest",
            base_url="http://127.0.0.1:1", username="j@x",
            credential_reference="env://WL_AC20_TOKEN",
        )
        wi.upsert_external(self.conn, cid, "AB-76", title="Upload recovery")

        rc, out = _run_cli(["backup", "--format", "json"])
        self.assertEqual(rc, 0)
        backup_path = Path(json.loads(out)["path"])

        # `export`'s own --format selects the export FILE format (json/
        # markdown/csv), not text-vs-json report rendering (cli.py's
        # `_setup_export` deliberately does not inherit the shared
        # --format flag) -- its output is always just the bare path, on
        # every format, never a JSON envelope.
        rc, out = _run_cli(["export", "--format", "json"])
        self.assertEqual(rc, 0)
        export_json_path = Path(out.strip())

        rc, out = _run_cli(["export", "--format", "markdown"])
        self.assertEqual(rc, 0)
        export_md_path = Path(out.strip())

        rc, out = _run_cli(["export", "--format", "csv"])
        self.assertEqual(rc, 0)
        csv_dir = Path(out.strip())

        artifacts = [backup_path, export_json_path, export_md_path]
        artifacts += list(csv_dir.glob("*.csv")) if csv_dir.is_dir() else [csv_dir]
        self.assertGreaterEqual(len(artifacts), 4)
        for p in artifacts:
            self.assertNotIn(secret_marker.encode(), Path(p).read_bytes(), p)

        export_text = export_json_path.read_text()
        self.assertIsNone(json.loads(export_text)["tables"]["connections"][0]["credential_reference"])

    # ------------------------------------------------------------------
    # 21. Backup and restore reproduce the same structured history
    # ------------------------------------------------------------------

    def test_ac21_backup_restore_roundtrip(self):
        from work_ledger import backup as backup_mod

        cl = entities.upsert_client(self.conn, "Acme")
        pr = entities.upsert_project(self.conn, cl, "Replay")
        wid = wi.upsert_external(self.conn, "ac21-jira", "AB-76", title="Upload recovery",
                                  client_id=cl, project_id=pr)
        remaining.create(self.conn, title="Retest on hardware", normalized_key="ac21-retest",
                          work_item_id=wid, client_id=cl, project_id=pr,
                          confidence="high", source="stage_a", occurred_at=timeutil.utc_now_iso())
        rc, out = _run_cli(["add", "client=Acme project=Replay ticket=AB-76 "
                            "Tested the upload recovery flow"])
        self.assertEqual(rc, 0)

        def dump():
            tables = ("clients", "projects", "work_items", "activities",
                      "remaining_items", "remaining_item_transitions")
            out = {}
            for t in tables:
                out[t] = [dict(r) for r in self.conn.execute(f"SELECT * FROM {t} ORDER BY id")]
            return out

        before = dump()

        rc, out = _run_cli(["backup", "--format", "json"])
        self.assertEqual(rc, 0)
        backup_path = json.loads(out)["path"]

        # mutate the live database so restore has something real to undo
        self.conn.execute("DELETE FROM remaining_item_transitions")
        self.conn.execute("DELETE FROM remaining_items")
        self.conn.execute("DELETE FROM activities")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM activities").fetchone()[0], 0)

        self.conn.close()
        backup_mod.restore(backup_path, confirm=True)
        self.conn = dbc.connect()

        self.assertEqual(dump(), before,
                          "restore must reproduce exactly the same structured history")

    # ------------------------------------------------------------------
    # 22. Daily reports default to summaries; evidence is available on request
    # ------------------------------------------------------------------

    def test_ac22_evidence_on_request(self):
        rc, out = _run_cli(["add", "client=Acme ticket=AB-76 Fixed the upload retry loop"])
        self.assertEqual(rc, 0)
        activity_id = out.strip()
        from work_ledger import ids
        self.conn.execute(
            "INSERT INTO activity_evidence(id,activity_id,evidence_type,ref,origin_device_id,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (ids.new_id(), activity_id, "file", "src/main.go", "dev1", timeutil.utc_now_iso()))

        rc, plain = _run_cli_json(["today", "--format", "json"])
        self.assertEqual(rc, 0)
        act = plain["clients"][0]["projects"][0]["work_items"][0]["activities"][0]
        self.assertNotIn("evidence", act)

        rc, evidenced = _run_cli_json(["today", "--evidence", "--format", "json"])
        self.assertEqual(rc, 0)
        act2 = evidenced["clients"][0]["projects"][0]["work_items"][0]["activities"][0]
        self.assertEqual(act2["evidence"][0]["ref"], "src/main.go")

    # ------------------------------------------------------------------
    # 23. Client/project reports include open work + previous 7 workdays
    # ------------------------------------------------------------------

    def test_ac23_status_window(self):
        from work_ledger import ids

        def insert_activity(work_item_id, client_id, started_at, summary):
            aid = ids.new_id()
            now = timeutil.utc_now_iso()
            self.conn.execute(
                "INSERT INTO activities(id,work_item_id,client_id,kind,summary,started_at,"
                "confidence,source,idempotency_key,source_hash,origin_device_id,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (aid, work_item_id, client_id, "code", summary, started_at, "high", "stage_a",
                 aid, "h", "dev1", now, now))
            return aid

        cl = entities.upsert_client(self.conn, "Acme")
        w_in = wi.upsert_external(self.conn, "ac23-jira", "AB-601", title="Six workdays back",
                                   client_id=cl)
        w_out = wi.upsert_external(self.conn, "ac23-jira", "AB-602", title="Nine workdays back",
                                    client_id=cl)
        remaining.create(self.conn, title="Retest on hardware", normalized_key="ac23-retest",
                          work_item_id=w_in, client_id=cl,
                          confidence="high", source="stage_a", occurred_at=timeutil.utc_now_iso())

        # "today" fixed to 2026-08-30 (America/Chicago, rollover 3): the
        # default 7-workday window is [today-6, today] inclusive, so an
        # activity dated exactly 6 workdays back is the window's own edge
        # (must appear) and one dated 9 workdays back is unambiguously
        # outside it (must not) -- content, not an echoed argument.
        insert_activity(w_in, cl, "2026-08-24T20:00:00.000Z", "worked six workdays back")
        insert_activity(w_out, cl, "2026-08-21T20:00:00.000Z", "worked nine workdays back")

        with mock.patch("work_ledger.timeutil.utc_now_iso", return_value="2026-08-30T20:00:00.000Z"):
            rc, rep = _run_cli_json(["status", "Acme", "--format", "json"])
        self.assertEqual(rc, 0)

        self.assertEqual(rep["window_workdays"], 7)
        self.assertEqual(rep["recent"]["kind"], "range")
        self.assertEqual(rep["recent"]["start_workday"], "2026-08-24")
        self.assertEqual(rep["recent"]["end_workday"], "2026-08-30")

        keys_in_window = {
            wi_row["work_item"]
            for c in rep["recent"]["clients"] for p in c["projects"] for wi_row in p["work_items"]
        }
        self.assertEqual(keys_in_window, {"AB-601"},
                          "the 6-workdays-back activity must appear, the 9-workdays-back one must not")
        self.assertEqual([i["title"] for i in rep["open_work"]], ["Retest on hardware"])

    # ------------------------------------------------------------------
    # 24. Jira is never modified by the plugin
    # ------------------------------------------------------------------

    def test_ac24_no_jira_writes(self):
        base = self._start_jira_server(mode="ok", issue=_issue("AB-76", status="In Progress"))
        cid = self._rest_connection("ac24-jira", base, token_env="WL_AC24_TOKEN")
        wid = wi.upsert_external(self.conn, cid, "AB-76", title="Upload recovery")
        remaining.create(self.conn, title="Retest", normalized_key="ac24-retest",
                          work_item_id=wid, confidence="high", source="stage_a",
                          occurred_at=timeutil.utc_now_iso())

        # a full session
        cwd = str(Path(self.tmp.name) / "repo")
        self._hook_session_start("sess-ac24", cwd)
        self._hook_commit("sess-ac24", cwd, "AB-76", tool_use_id="toolu_ac24_a")
        self._hook_stop("sess-ac24", cwd)
        # a refresh
        _run_cli(["jira", "refresh", "--connection", "ac24-jira", "--format", "json"])
        # a report
        _run_cli(["status", "AB-76", "--format", "json"])
        # a correction cycle (resolving a contradiction only ever touches our
        # own `contradictions` row, never Jira -- see cli._cmd_contradictions)
        _run_cli(["contradictions", "--format", "json"])
        rows = self.conn.execute(
            "SELECT id FROM contradictions WHERE status='open' LIMIT 1").fetchall()
        if rows:
            _run_cli(["contradictions", "--resolve", rows[0]["id"], "--note", "checked manually",
                      "--format", "json"])

        self.assertEqual(JiraHandler.mutating_calls, [],
                          "no POST/PUT/DELETE/PATCH request ever reached the Jira endpoint")

        # Plus the static grep from Task 38 (tests/test_security.py
        # test_no_jira_write_verbs_anywhere): no write verb or write-tool
        # name appears anywhere in the jira adapter package's source.
        for p in (SRC / "jira").rglob("*.py"):
            text = p.read_text()
            for verb in ('"POST"', "'POST'", '"PUT"', '"DELETE"', '"PATCH"',
                         "createJiraIssue", "transitionJiraIssue", "addCommentToJiraIssue",
                         "addWorklogToJiraIssue", "editJiraIssue", "createIssueLink"):
                self.assertNotIn(verb, text, f"{p} references {verb}")

if __name__ == "__main__":
    unittest.main()
