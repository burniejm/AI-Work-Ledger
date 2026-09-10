import os, sys, unittest
from unittest import mock
from work_ledger import credentials as cred


class TestReferences(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(cred.parse_reference("keychain://work-ledger/acme-jira"),
                         ("keychain", "work-ledger", "acme-jira"))
        self.assertEqual(cred.parse_reference("env://JIRA_TOKEN"), ("env", "", "JIRA_TOKEN"))
        with self.assertRaises(cred.InvalidReference):
            cred.parse_reference("https://example.com/token")


class TestEnvStore(unittest.TestCase):
    def test_reads_env(self):
        os.environ["WL_TEST_TOKEN"] = "abc123"
        try:
            self.assertEqual(cred.resolve("env://WL_TEST_TOKEN"), "abc123")
        finally:
            del os.environ["WL_TEST_TOKEN"]

    def test_missing_env_returns_none(self):
        self.assertIsNone(cred.resolve("env://WL_DEFINITELY_MISSING"))

    def test_set_is_unsupported(self):
        with self.assertRaises(cred.Unsupported):
            cred.EnvRefStore().set("env://X", "y")


class TestKeychainStore(unittest.TestCase):
    def test_get_builds_the_right_argv(self):
        store = cred.KeychainStore()
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="s3cr3t\n", stderr="")
            self.assertEqual(store.get("keychain://work-ledger/acme-jira"), "s3cr3t")
        argv = run.call_args[0][0]
        self.assertEqual(argv[:2], ["/usr/bin/security", "find-generic-password"])
        self.assertIn("work-ledger", argv)
        self.assertIn("acme-jira", argv)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_missing_item_returns_none(self):
        store = cred.KeychainStore()
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=44, stdout="", stderr="could not be found")
            self.assertIsNone(store.get("keychain://work-ledger/nope"))

    def test_set_omits_the_secret_from_argv_on_a_tty(self):
        store = cred.KeychainStore()
        with mock.patch("sys.stdin") as stdin, mock.patch("subprocess.run") as run:
            stdin.isatty.return_value = True
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            store.set("keychain://work-ledger/j", "s3cr3tvalue")
        self.assertNotIn("s3cr3tvalue", run.call_args[0][0])


class TestUnsupportedPlatforms(unittest.TestCase):
    def test_windows_and_linux_stores_report_unsupported(self):
        for cls in (cred.WindowsCredentialStore, cred.LinuxSecretServiceStore):
            s = cls()
            self.assertFalse(s.available())
            with self.assertRaises(cred.Unsupported):
                s.get("keychain://work-ledger/x")


if __name__ == "__main__":
    unittest.main()
