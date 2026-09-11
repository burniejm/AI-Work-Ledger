import json, os, subprocess, tempfile, unittest
from pathlib import Path

from work_ledger import githooks, ids, timeutil, work_items as wi, entities as ent
from work_ledger.db import connection as dbc, migrations as m


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)


def _make_repo(root):
    repo = os.path.join(root, "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "config", "user.name", "Dev User")
    return repo


class _GitHooksCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WORK_LEDGER_HOME"] = os.path.join(self.tmp.name, "wl")
        self.conn = dbc.connect()
        m.apply_all(self.conn)
        self.repo = _make_repo(self.tmp.name)

    def tearDown(self):
        del os.environ["WORK_LEDGER_HOME"]
        self.tmp.cleanup()


class TestHumanCommitsSchema(_GitHooksCase):
    def test_table_exists_with_unique_sha_per_repo(self):
        import sqlite3
        now = timeutil.utc_now_iso()
        self.conn.execute(
            "INSERT INTO human_commits(id,repo_root,sha,authored_at,author_email,subject,"
            "files_json,session_active,origin_device_id,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ids.new_id(), "/r", "abc", now, "dev@example.com", "AB-76 fix", "[]", 0, "d", now))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO human_commits(id,repo_root,sha,authored_at,author_email,subject,"
                "files_json,session_active,origin_device_id,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ids.new_id(), "/r", "abc", now, "dev@example.com", "dup", "[]", 0, "d", now))


class TestInstallRemove(_GitHooksCase):
    def _hook_path(self):
        return Path(self.repo) / ".git" / "hooks" / "post-commit"

    def test_install_creates_an_executable_hook_with_the_marker(self):
        githooks.install_hook(self.conn, self.repo)
        hook = self._hook_path()
        self.assertTrue(hook.is_file())
        self.assertTrue(os.access(hook, os.X_OK))
        content = hook.read_text()
        self.assertIn(githooks.HOOK_MARKER, content)
        self.assertTrue(content.startswith("#!/bin/sh"))

    def test_install_is_idempotent(self):
        githooks.install_hook(self.conn, self.repo)
        githooks.install_hook(self.conn, self.repo)
        content = self._hook_path().read_text()
        self.assertEqual(content.count(githooks.HOOK_MARKER), 1)

    def test_install_appends_to_an_existing_hook_without_clobbering(self):
        hook = self._hook_path()
        hook.write_text("#!/bin/sh\necho existing\n")
        hook.chmod(0o755)
        githooks.install_hook(self.conn, self.repo)
        content = hook.read_text()
        self.assertIn("echo existing", content)
        self.assertIn(githooks.HOOK_MARKER, content)

    def test_remove_deletes_only_our_line(self):
        hook = self._hook_path()
        hook.write_text("#!/bin/sh\necho existing\n")
        hook.chmod(0o755)
        githooks.install_hook(self.conn, self.repo)
        githooks.remove_hook(self.conn, self.repo)
        content = hook.read_text()
        self.assertIn("echo existing", content)
        self.assertNotIn(githooks.HOOK_MARKER, content)

    def test_installed_repos_are_tracked_and_untracked(self):
        githooks.install_hook(self.conn, self.repo)
        self.assertIn(os.path.realpath(self.repo), githooks.installed_repos(self.conn))
        githooks.remove_hook(self.conn, self.repo)
        self.assertEqual(githooks.installed_repos(self.conn), [])


class TestCapture(_GitHooksCase):
    def _commit(self, message, filename="f.txt"):
        Path(self.repo, filename).write_text(message)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", message)

    def _rows(self):
        return self.conn.execute(
            "SELECT * FROM human_commits ORDER BY created_at").fetchall()

    def test_capture_records_the_newest_commit_once(self):
        self._commit("AB-76 fix retry by hand")
        out = githooks.capture(self.conn, self.repo)
        self.assertTrue(out["captured"])
        out2 = githooks.capture(self.conn, self.repo)
        self.assertFalse(out2["captured"])
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subject"], "AB-76 fix retry by hand")
        self.assertEqual(rows[0]["session_active"], 0)

    def test_capture_marks_session_active_when_a_live_session_covers_the_repo(self):
        from work_ledger.capture import sessions as S
        S.upsert(self.conn, session_id="s1", cwd=self.repo,
                 occurred_at=timeutil.utc_now_iso())
        self._commit("AB-76 tweak while pairing")
        out = githooks.capture(self.conn, self.repo)
        self.assertTrue(out["captured"])
        self.assertEqual(self._rows()[0]["session_active"], 1)

    def test_capture_redacts_a_secret_bearing_subject(self):
        self._commit("oops token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
        githooks.capture(self.conn, self.repo)
        row = self._rows()[0]
        self.assertNotIn("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", row["subject"])
        self.assertIn("REDACTED", row["subject"])

    def test_capture_outside_a_repo_reports_not_captured(self):
        out = githooks.capture(self.conn, self.tmp.name)
        self.assertFalse(out["captured"])


import contextlib, io

from work_ledger import cli


class TestGithooksCli(_GitHooksCase):
    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_contract_parse_shape(self):
        args = cli.build_parser().parse_args(["githooks", "--format", "json"])
        self.assertEqual(args.command, "githooks")

    def test_install_status_remove_roundtrip(self):
        rc, out, _ = self._run(["githooks", "install", "--repo", self.repo])
        self.assertEqual(rc, 0)
        rc, out, _ = self._run(["githooks", "status", "--format", "json"])
        self.assertEqual(rc, 0)
        self.assertIn(os.path.realpath(self.repo), json.loads(out)["repos"])
        rc, _, _ = self._run(["githooks", "remove", "--repo", self.repo])
        self.assertEqual(rc, 0)
        rc, out, _ = self._run(["githooks", "status", "--format", "json"])
        self.assertEqual(json.loads(out)["repos"], [])

    def test_capture_never_exits_nonzero(self):
        # No repo argument, bogus repo, ledger trouble: rc must stay 0 --
        # this runs inside `git commit` and must never break one.
        rc, _, _ = self._run(["githooks", "capture"])
        self.assertEqual(rc, 0)
        rc, _, _ = self._run(["githooks", "capture", "--repo", "/nope"])
        self.assertEqual(rc, 0)

    def test_end_to_end_a_real_git_commit_lands_in_the_ledger(self):
        import subprocess as sp
        rc, _, _ = self._run(["githooks", "install", "--repo", self.repo])
        self.assertEqual(rc, 0)
        # Make the hook find OUR checkout's worklog first on PATH.
        bindir = os.path.join(self.tmp.name, "bin")
        os.makedirs(bindir)
        shim = Path(bindir, "worklog")
        repo_root = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
        shim.write_text("#!/bin/sh\n"
                        f"export WORK_LEDGER_HOME='{os.environ['WORK_LEDGER_HOME']}'\n"
                        f"export PYTHONPATH='{repo_root}/src'\n"
                        f"exec python3 -m work_ledger.cli \"$@\"\n")
        shim.chmod(0o755)
        env = dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"])
        Path(self.repo, "h.txt").write_text("by hand")
        sp.run(["git", "-C", self.repo, "add", "-A"], check=True, env=env,
               capture_output=True)
        sp.run(["git", "-C", self.repo, "commit", "-q", "-m", "AB-9 handwritten"],
               check=True, env=env, capture_output=True)
        row = self.conn.execute("SELECT subject FROM human_commits").fetchone()
        self.assertIsNotNone(row, "post-commit hook did not capture the commit")
        self.assertEqual(row["subject"], "AB-9 handwritten")


from work_ledger.capture import events as ev, sessions as S
from work_ledger.infer import ai_usage


class TestScoreIntegration(_GitHooksCase):
    def setUp(self):
        super().setUp()
        now = timeutil.utc_now_iso()
        self.conn.execute(
            "INSERT INTO connections(id,name,adapter,ticket_patterns_json,origin_device_id,"
            "created_at,updated_at) VALUES ('c1','j','jira-rest','[\"AB-*\"]','d',?,?)", (now, now))
        self.item = wi.upsert_external(self.conn, "c1", "AB-76", title="t")
        S.upsert(self.conn, session_id="s1", cwd="/w",
                 occurred_at="2026-09-01T12:00:00.000Z")
        srow = self.conn.execute(
            "SELECT id FROM sessions WHERE session_id='s1'").fetchone()["id"]
        aid = ids.new_id()
        self.conn.execute(
            "INSERT INTO activities(id,work_item_id,session_row_id,kind,summary,started_at,"
            "confidence,source,idempotency_key,source_hash,origin_device_id,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, self.item, srow, "code", "worked", now,
             "high", "stage_a", aid, "h", "d", now, now))
        for i in range(6):
            ev.append_source_event(
                self.conn, session_id="s1", event_type="tool_use",
                occurred_at="2026-09-01T12:05:00.000Z", payload={"tool_name": "Edit"},
                tool_name="Edit", idem=ev.idempotency_key("s1", "tool_use", f"e{i}"))

    def _human_commit(self, sha, subject, session_active=0):
        now = timeutil.utc_now_iso()
        self.conn.execute(
            "INSERT INTO human_commits(id,repo_root,sha,authored_at,author_email,subject,"
            "files_json,session_active,origin_device_id,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ids.new_id(), "/w", sha, now, "dev@example.com", subject, "[]",
             session_active, "d", now))

    def _score(self):
        ai_usage.recompute_for_work_item(self.conn, self.item)
        return self.conn.execute(
            "SELECT ai_usage_score, ai_usage_reason FROM work_items WHERE id=?",
            (self.item,)).fetchone()

    def test_hook_captured_commit_counts_human_side(self):
        self.assertEqual(self._score()["ai_usage_score"], 5)
        self._human_commit("h1", "AB-76 polish by hand")
        row = self._score()
        self.assertEqual(row["ai_usage_score"], 3)
        self.assertIn("1 human-only commit", row["ai_usage_reason"])

    def test_other_tickets_commits_do_not_count(self):
        self._human_commit("h1", "AB-99 unrelated work")
        self.assertEqual(self._score()["ai_usage_score"], 5)

    def test_session_active_hook_rows_are_ambiguous_not_human(self):
        self._human_commit("h1", "AB-76 made while pairing", session_active=1)
        self.assertEqual(self._score()["ai_usage_score"], 5)

    def test_sha_seen_by_reconcile_is_not_double_counted(self):
        # The same commit lands via reconcile into a read-only session AND
        # via the hook: one human commit, not two.
        S.upsert(self.conn, session_id="s2", cwd="/w",
                 occurred_at="2026-09-01T13:00:00.000Z")
        srow2 = self.conn.execute(
            "SELECT id FROM sessions WHERE session_id='s2'").fetchone()["id"]
        now = timeutil.utc_now_iso()
        aid = ids.new_id()
        self.conn.execute(
            "INSERT INTO activities(id,work_item_id,session_row_id,kind,summary,started_at,"
            "confidence,source,idempotency_key,source_hash,origin_device_id,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, self.item, srow2, "code", "reviewed", now,
             "high", "stage_a", aid, "h2", "d", now, now))
        ev.append_source_event(
            self.conn, session_id="s2", event_type="commit",
            occurred_at="2026-09-01T13:05:00.000Z",
            payload={"sha": "h1", "subject": "AB-76 polish by hand"},
            hook_event="git_reconcile", idem=ev.idempotency_key("s2", "commit", "h1"))
        self._human_commit("h1", "AB-76 polish by hand")
        row = self._score()
        self.assertIn("1 human-only commit", row["ai_usage_reason"])


class TestUninstallCleansHooks(_GitHooksCase):
    def test_uninstall_with_data_removes_installed_hook_lines(self):
        from work_ledger import uninstall
        githooks.install_hook(self.conn, self.repo)
        hook = Path(os.path.realpath(self.repo)) / ".git" / "hooks" / "post-commit"
        self.assertIn(githooks.HOOK_MARKER, hook.read_text())
        self.conn.close()
        out = uninstall.run(data=True, confirmation="delete my work ledger")
        self.assertIsNone(out["refused"])
        self.assertEqual(out["deleted"]["githooks"],
                         [{"repo": str(Path(os.path.realpath(self.repo))), "removed": True}])
        self.assertNotIn(githooks.HOOK_MARKER, hook.read_text())


class TestWizardStep(_GitHooksCase):
    def _approve_repo(self):
        ent.upsert_repository(self.conn, os.path.realpath(self.repo),
                              name="repo", approved=True)

    def test_step_is_opt_in_and_skips_by_default(self):
        from work_ledger import setupwizard as sw
        self._approve_repo()
        report = sw.run(self.conn, answers={}, only="githooks")
        step = [r for r in report["steps"] if r["name"] == "githooks"][0]
        self.assertNotEqual(step["state"], "fail")
        hook = Path(os.path.realpath(self.repo)) / ".git" / "hooks" / "post-commit"
        self.assertFalse(hook.exists() and githooks.HOOK_MARKER in hook.read_text())

    def test_step_installs_for_approved_repos_when_asked(self):
        from work_ledger import setupwizard as sw
        self._approve_repo()
        report = sw.run(self.conn, answers={"githooks": {"install": True}},
                        only="githooks")
        step = [r for r in report["steps"] if r["name"] == "githooks"][0]
        self.assertEqual(step["state"], "ok")
        hook = Path(os.path.realpath(self.repo)) / ".git" / "hooks" / "post-commit"
        self.assertIn(githooks.HOOK_MARKER, hook.read_text())


if __name__ == "__main__":
    unittest.main()
