import os, subprocess, tempfile, unittest
from work_ledger import config, gitevidence as g, paths

def sh(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)

class TestGitEvidence(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("WORK_LEDGER_HOME")
        os.environ["WORK_LEDGER_HOME"] = self.home.name
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        sh(self.repo, "git", "init", "-q", "-b", "main")
        sh(self.repo, "git", "config", "user.email", "dev@example.com")
        sh(self.repo, "git", "config", "user.name", "Test User")
        open(os.path.join(self.repo, "a.txt"), "w").write("hello")
        sh(self.repo, "git", "add", "a.txt")
        sh(self.repo, "git", "commit", "-q", "-m", "AB-76 add the upload retry")
    def tearDown(self):
        self.tmp.cleanup()
        if self._old_home is None:
            os.environ.pop("WORK_LEDGER_HOME", None)
        else:
            os.environ["WORK_LEDGER_HOME"] = self._old_home
        self.home.cleanup()

    def test_repo_root_and_branch(self):
        self.assertEqual(os.path.realpath(g.repo_root(self.repo)), os.path.realpath(self.repo))
        self.assertEqual(g.current_branch(self.repo), "main")

    def test_identities(self):
        self.assertEqual(g.identities(self.repo)["email"], "dev@example.com")

    def test_commits_since_returns_subject_and_files(self):
        rows = g.commits_since(self.repo, "1970-01-01T00:00:00Z")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subject"], "AB-76 add the upload retry")
        self.assertEqual(rows[0]["files"], ["a.txt"])
        self.assertEqual(rows[0]["author_email"], "dev@example.com")

    def test_author_filter_excludes_others(self):
        self.assertEqual(g.commits_since(self.repo, "1970-01-01T00:00:00Z", authors=["someone@else.com"]), [])

    def test_mutating_commands_are_refused(self):
        for bad in ("commit", "push", "reset", "checkout"):
            with self.assertRaises(ValueError):
                g.run_git(self.repo, [bad, "-m", "nope"])

    def test_non_repo_returns_none_not_exception(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(g.repo_root(d))
            self.assertIsNone(g.current_branch(d))

    def test_remote_url_credentials_are_redacted(self):
        sh(self.repo, "git", "remote", "add", "origin", "https://user:s3cr3tpass@example.com/x.git")
        self.assertNotIn("s3cr3tpass", str(g.remotes(self.repo)))

    def test_timeout_is_reported_not_raised(self):
        rc, out, err = g.run_git(self.repo, ["log"], timeout=0.000001)
        self.assertEqual(rc, -1)
        self.assertIn("timeout", err.lower())

    # --- CONTROLLER RULING: allowlist bypass probes (IMPORTANT 1) ---

    def test_leading_global_option_bypass_is_refused(self):
        # A leading global option (`-c ...`) pushes the real subcommand off
        # position 0; a position-0 denylist never sees "commit" at all.
        with self.assertRaises(ValueError):
            g.run_git(self.repo, ["-c", "user.email=x", "commit", "--allow-empty", "-m", "x"])

    def test_config_write_form_is_refused(self):
        # `git config` is a read command in its get form but a write command
        # when given a value -- the allowlist must reject the write form.
        with self.assertRaises(ValueError):
            g.run_git(self.repo, ["config", "user.email", "x"])

    def test_config_read_form_still_works(self):
        rc, out, _err = g.run_git(self.repo, ["config", "user.email"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "dev@example.com")

    def test_symbolic_ref_write_form_is_refused(self):
        with self.assertRaises(ValueError):
            g.run_git(self.repo, ["symbolic-ref", "HEAD", "refs/heads/elsewhere"])

    def test_symbolic_ref_read_form_still_works(self):
        rc, out, _err = g.run_git(self.repo, ["symbolic-ref", "--quiet", "HEAD"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "refs/heads/main")

    def test_subcommand_not_on_allowlist_is_refused(self):
        with self.assertRaises(ValueError):
            g.run_git(self.repo, ["fetch"])

    # --- CONTROLLER RULING: config wiring (IMPORTANT 2) ---

    def test_run_git_default_timeout_comes_from_config(self):
        paths.ensure_dirs()
        paths.config_path().write_text("git:\n  subprocess_timeout_seconds: 0\n")
        rc, _out, err = g.run_git(self.repo, ["log"])
        self.assertEqual(rc, -1)
        self.assertIn("timeout", err.lower())

    def test_commits_since_default_limit_comes_from_config(self):
        sh(self.repo, "bash", "-c", "echo more > b.txt")
        sh(self.repo, "git", "add", "b.txt")
        sh(self.repo, "git", "commit", "-q", "-m", "second commit")
        paths.ensure_dirs()
        paths.config_path().write_text("git:\n  max_commits_per_reconcile: 1\n")
        rows = g.commits_since(self.repo, "1970-01-01T00:00:00Z")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subject"], "second commit")

    # --- MINOR: run_git prepends its own fixed safe options only ---

    def test_non_ascii_filenames_survive_name_only(self):
        fname = "café.txt"
        with open(os.path.join(self.repo, fname), "w") as fh:
            fh.write("x")
        sh(self.repo, "git", "add", fname)
        sh(self.repo, "git", "commit", "-q", "-m", "add unicode file")
        rows = g.commits_since(self.repo, "1970-01-01T00:00:00Z")
        self.assertIn(fname, rows[0]["files"])

    # --- CONTROLLER RULING round 2: allowlist still leaked (IMPORTANT) ---

    def test_branch_create_is_refused(self):
        with self.assertRaises(ValueError):
            g.run_git(self.repo, ["branch", "newbranch"])
        self.assertNotIn(
            "newbranch",
            subprocess.run(["git", "branch"], cwd=self.repo, capture_output=True, text=True).stdout,
        )

    def test_branch_delete_is_refused(self):
        sh(self.repo, "git", "branch", "throwaway")
        with self.assertRaises(ValueError):
            g.run_git(self.repo, ["branch", "-D", "throwaway"])

    def test_branch_rename_is_refused(self):
        with self.assertRaises(ValueError):
            g.run_git(self.repo, ["branch", "-m", "main", "renamed"])
        self.assertEqual(g.current_branch(self.repo), "main")

    def test_remote_add_is_refused(self):
        with self.assertRaises(ValueError):
            g.run_git(self.repo, ["remote", "add", "origin", "https://example.com/x.git"])
        self.assertEqual(g.remotes(self.repo), [])

    def test_remote_remove_is_refused(self):
        sh(self.repo, "git", "remote", "add", "origin", "https://example.com/x.git")
        with self.assertRaises(ValueError):
            g.run_git(self.repo, ["remote", "remove", "origin"])
        self.assertEqual(len(g.remotes(self.repo)), 1)

    def test_remotes_still_works_after_remote_read_form_restriction(self):
        sh(self.repo, "git", "remote", "add", "origin", "https://example.com/x.git")
        rows = g.remotes(self.repo)
        self.assertEqual(rows, [{"name": "origin", "url": "https://example.com/x.git"}])

    def test_output_flag_is_refused(self):
        target = os.path.join(self.tmp.name, "..", "clobber-me.txt")
        with self.assertRaises(ValueError):
            g.run_git(self.repo, ["log", f"--output={target}", "-1"])

    def test_normal_log_still_works_after_output_flag_restriction(self):
        rc, out, _err = g.run_git(self.repo, ["log", "-1", "--pretty=%s"])
        self.assertEqual(rc, 0)
        self.assertIn("AB-76 add the upload retry", out)

    def test_diff_show_status_ls_files_are_not_on_the_allowlist(self):
        # Important #3 (final review): these four subcommands had zero
        # production callers and `diff --no-index` is an arbitrary-file-read
        # primitive with no read-form check guarding it -- dropped entirely
        # rather than patched.
        for args in (
            ["diff", "--stat"],
            ["diff", "--no-index", "/etc/hosts", "/dev/null"],
            ["show", "HEAD"],
            ["status", "--short"],
            ["ls-files"],
        ):
            with self.assertRaises(ValueError):
                g.run_git(self.repo, args)

    def test_diff_no_index_cannot_read_an_arbitrary_file(self):
        outside = os.path.join(self.tmp.name, "..", "outside-the-repo.txt")
        with open(outside, "w") as fh:
            fh.write("should never be readable via gitevidence")
        try:
            with self.assertRaises(ValueError):
                g.run_git(self.repo, ["diff", "--no-index", outside, "/dev/null"])
        finally:
            os.remove(outside)
