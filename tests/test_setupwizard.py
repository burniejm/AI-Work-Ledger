import json, os, subprocess, tempfile, unittest
from pathlib import Path
from work_ledger.db import connection as dbc, migrations as m
from work_ledger import setupwizard as sw, connections as C, paths

def git_repo(path):
    os.makedirs(path, exist_ok=True)
    for args in (["git","init","-q"], ["git","config","user.email","dev@example.com"],
                 ["git","config","user.name","Test User"]):
        subprocess.run(args, cwd=path, check=True, capture_output=True)
    return path

class TestSetup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WORK_LEDGER_HOME"] = os.path.join(self.tmp.name, "wl")
        # Hermetic-environment redirection:
        # without these two lines the wizard reads the developer's real
        # ~/.claude/projects transcripts and the real cwd's git config. Both
        # are redirected into the temp tree so the suite is hermetic.
        self.projects = os.path.join(self.tmp.name, "projects")
        os.makedirs(self.projects, exist_ok=True)
        os.environ["CLAUDE_PROJECTS_DIR"] = self.projects
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite")); m.apply_all(self.conn)
        self.repo = git_repo(os.path.join(self.tmp.name, "dev", "replay"))
        self._cwd = os.getcwd()
        os.chdir(self.repo)
        self.answers = {"timezone": "America/Chicago", "rollover_hour": 3,
                        "dev_roots": [os.path.join(self.tmp.name, "dev")],
                        "jira": {"mode": "skip"}}
    def tearDown(self):
        os.chdir(self._cwd)
        del os.environ["WORK_LEDGER_HOME"]; del os.environ["CLAUDE_PROJECTS_DIR"]
        self.tmp.cleanup()

    def test_runs_every_step_in_order(self):
        rep = sw.run(self.conn, answers=self.answers)
        self.assertEqual([s["name"] for s in rep["steps"]], list(sw.STEPS))

    def test_is_resumable(self):
        sw.run(self.conn, answers=self.answers, only="directories")
        state = json.loads((paths.state_dir() / "setup.json").read_text())
        self.assertEqual(state["completed"], ["directories"])
        rep = sw.run(self.conn, answers=self.answers)
        self.assertTrue(all(s["state"] in ("ok", "warn") for s in rep["steps"]))

    def test_rerun_does_not_duplicate_connections_identities_or_repositories(self):
        answers = {**self.answers, "jira": {"mode": "rest", "name": "j", "base_url": "http://127.0.0.1:9",
                                            "username": "j@x", "credential_reference": "env://WL_TEST_TOKEN"}}
        sw.run(self.conn, answers=answers)
        counts = lambda: tuple(self.conn.execute(f"SELECT count(*) FROM {t} WHERE deleted_at IS NULL").fetchone()[0]
                               for t in ("connections", "user_identities", "repositories"))
        before = counts()
        sw.run(self.conn, answers=answers)
        self.assertEqual(counts(), before)

    def test_reset_clears_progress_but_not_history(self):
        sw.run(self.conn, answers=self.answers)
        before = self.conn.execute("SELECT count(*) FROM repositories").fetchone()[0]
        sw.run(self.conn, answers=self.answers, reset=True)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM repositories").fetchone()[0], before)

    def test_repository_discovery_is_scoped_to_approved_roots(self):
        outside = git_repo(os.path.join(self.tmp.name, "elsewhere", "secret-repo"))
        found = sw.discover_repositories(roots=[os.path.join(self.tmp.name, "dev")], transcript_cwds=[])
        paths_found = {r["root_path"] for r in found}
        self.assertIn(os.path.realpath(self.repo), {os.path.realpath(p) for p in paths_found})
        self.assertNotIn(os.path.realpath(outside), {os.path.realpath(p) for p in paths_found})

    def test_git_identity_is_detected(self):
        sw.run(self.conn, answers=self.answers)
        vals = {r[0] for r in self.conn.execute("SELECT value FROM user_identities WHERE kind='git'")}
        self.assertIn("dev@example.com", vals)

    def test_jira_failure_does_not_fail_setup(self):
        answers = {**self.answers, "jira": {"mode": "rest", "name": "j", "base_url": "http://127.0.0.1:1",
                                            "username": "j@x", "credential_reference": "env://NOPE"}}
        rep = sw.run(self.conn, answers=answers)
        self.assertNotEqual(rep["overall"], "failed")
        jira_step = next(s for s in rep["steps"] if s["name"] == "jira_detect")
        self.assertIn(jira_step["state"], ("warn", "fail"))
        # Ruling 20: the test runs in a throwaway WORK_LEDGER_HOME with no plugin
        # installed, so the hooks step legitimately reports `warn` ("not yet
        # configured/installed", Ruling 19). What matters is that a Jira failure did
        # not degrade it to `fail`.
        self.assertIn(next(s for s in rep["steps"] if s["name"] == "hooks")["state"], ("ok", "warn"))

    def test_completion_report_matches_the_spec_shape(self):
        sw.run(self.conn, answers=self.answers)
        text = sw.completion_report(self.conn)
        for line in ("Work Ledger setup complete", "Database:", "Claude capture hooks:",
                     "Git identity:", "Repositories discovered:", "Reporting timezone:", "Workday rollover:"):
            self.assertIn(line, text)

    def test_no_secret_reaches_config_or_database(self):
        os.environ["WL_TEST_TOKEN"] = "ATATT3xFfGF0SUPERSECRET00000000000000000000"
        try:
            answers = {**self.answers, "jira": {"mode": "rest", "name": "j", "base_url": "http://127.0.0.1:9",
                                                "username": "j@x", "credential_reference": "env://WL_TEST_TOKEN"}}
            sw.run(self.conn, answers=answers)
            cfg = paths.config_path().read_text() if paths.config_path().exists() else ""
            self.assertNotIn("ATATT3xFfGF0SUPERSECRET", cfg)
            blob = "".join(str(r) for r in self.conn.execute("SELECT * FROM connections"))
            self.assertNotIn("ATATT3xFfGF0SUPERSECRET", blob)
        finally:
            del os.environ["WL_TEST_TOKEN"]

    # ---- regressions found in review -----------------------------------

    def _rest_answers(self, reference="env://WL_TEST_TOKEN"):
        return {**self.answers,
                "jira": {"mode": "rest", "name": "j", "base_url": "http://127.0.0.1:9",
                         "username": "j@x", "credential_reference": reference}}

    def test_rerun_preserves_a_safe_credential_reference(self):
        # A credential *reference* is not a secret. redact_obj's key rule fires
        # on "credential_reference" all the same, so the persisted answers used
        # to carry "[REDACTED:credential_reference]" and the next run wrote that
        # placeholder over a working connection.
        os.environ["WL_TEST_TOKEN"] = "ATATT3xFfGF0SUPERSECRET00000000000000000000"
        try:
            sw.run(self.conn, answers=self._rest_answers())
            stored = sw.State.load().data["answers"]["jira"]["credential_reference"]
            self.assertEqual(stored, "env://WL_TEST_TOKEN")
            sw.run(self.conn, answers={})
            row = self.conn.execute(
                "SELECT credential_reference, health_state FROM connections").fetchone()
            self.assertEqual(row["credential_reference"], "env://WL_TEST_TOKEN")
            self.assertNotEqual(row["health_state"], "missing")
        finally:
            del os.environ["WL_TEST_TOKEN"]

    def test_interactive_resume_offers_the_real_credential_reference(self):
        os.environ["WL_TEST_TOKEN"] = "ATATT3xFfGF0SUPERSECRET00000000000000000000"
        try:
            sw.run(self.conn, answers=self._rest_answers())
            asked = []
            def prompt(question):
                asked.append(question)
                return ""          # accept every default
            sw.run(self.conn, answers=None, prompt=prompt, only="jira_detect")
            reference_question = next(q for q in asked if "Credential reference" in q)
            self.assertIn("env://WL_TEST_TOKEN", reference_question)
            self.assertNotIn("REDACTED", reference_question)
        finally:
            del os.environ["WL_TEST_TOKEN"]

    def test_a_raw_token_never_reaches_the_state_file(self):
        answers = {**self.answers,
                   "jira": {"mode": "rest", "name": "j", "base_url": "http://127.0.0.1:9",
                            "username": "j@x", "credential_reference": "env://WL_MISSING",
                            "token": "ATATT3xFfGF0SUPERSECRET00000000000000000000"}}
        sw.run(self.conn, answers=answers)
        self.assertNotIn("ATATT3xFfGF0SUPERSECRET",
                         (paths.state_dir() / "setup.json").read_text())

    def test_answers_mode_stores_ticket_patterns_client_hint_and_extra_git_emails(self):
        answers = {**self.answers, "git_emails": ["alt@example.com"],
                   "jira": {"mode": "rest", "name": "j", "base_url": "http://127.0.0.1:9",
                            "username": "j@x", "credential_reference": "env://WL_MISSING",
                            "client_hint": "Acme", "ticket_patterns": ["AB-*", "SC-*"],
                            "project_filter": ["SK"]}}
        sw.run(self.conn, answers=answers)
        row = self.conn.execute(
            "SELECT client_hint, ticket_patterns_json, project_filter_json FROM connections"
        ).fetchone()
        self.assertEqual(row["client_hint"], "Acme")
        self.assertEqual(json.loads(row["ticket_patterns_json"]), ["AB-*", "SC-*"])
        self.assertEqual(json.loads(row["project_filter_json"]), ["SK"])
        vals = {r[0] for r in self.conn.execute(
            "SELECT value FROM user_identities WHERE kind='git'")}
        self.assertIn("alt@example.com", vals)

    def test_interactive_mode_collects_ticket_patterns_client_hint_and_git_emails(self):
        scripted = {
            "Reporting timezone": "America/Chicago",
            "Workday rollover hour": "3",
            "Development roots": os.path.join(self.tmp.name, "dev"),
            "Additional git author emails": "alt@example.com",
            "Jira setup": "rest",
            "Connection name": "j",
            "Jira base URL": "http://127.0.0.1:9",
            "Jira account email": "j@x",
            "Credential reference": "env://WL_MISSING",
            "API token": "",
            "Client this Jira serves": "Acme",
            "Ticket key patterns": "AB-*, SC-*",
            "Restrict to Jira project keys": "SK",
        }
        def prompt(question):
            for needle, reply in scripted.items():
                if needle in question:
                    return reply
            raise AssertionError(f"unscripted setup question: {question!r}")

        sw.run(self.conn, answers=None, prompt=prompt)
        row = self.conn.execute(
            "SELECT client_hint, ticket_patterns_json, project_filter_json FROM connections"
        ).fetchone()
        self.assertEqual(row["client_hint"], "Acme")
        self.assertEqual(json.loads(row["ticket_patterns_json"]), ["AB-*", "SC-*"])
        self.assertEqual(json.loads(row["project_filter_json"]), ["SK"])
        vals = {r[0] for r in self.conn.execute(
            "SELECT value FROM user_identities WHERE kind='git'")}
        self.assertIn("alt@example.com", vals)
        self.assertIn("dev@example.com", vals)

    def test_self_test_source_event_is_bounded_to_one_per_day(self):
        sw.run(self.conn, answers=self.answers)
        first = self.conn.execute("SELECT count(*) FROM source_events").fetchone()[0]
        rep = sw.run(self.conn, answers=self.answers)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM source_events").fetchone()[0], first)
        end_to_end = next(s for s in rep["steps"] if s["name"] == "end_to_end")
        self.assertEqual(end_to_end["state"], "ok")
