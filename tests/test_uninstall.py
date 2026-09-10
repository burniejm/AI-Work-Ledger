# tests/test_uninstall.py   (Ruling 12)
import contextlib, io, json, os, sys, tempfile, unittest
from unittest import mock
from work_ledger.db import connection as dbc, migrations as m
from work_ledger import cli, uninstall, paths

class TestUninstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WORK_LEDGER_HOME"] = os.path.join(self.tmp.name, "wl")
        paths.ensure_dirs()
        self.conn = dbc.connect(str(paths.db_path())); m.apply_all(self.conn)
        self.conn.execute("INSERT INTO connections(id,name,adapter,credential_reference,origin_device_id,"
                          "created_at,updated_at) VALUES ('c','j','jira-rest',"
                          "'keychain://work-ledger/j','d','t','t')")
        self.conn.close()
    def tearDown(self):
        del os.environ["WORK_LEDGER_HOME"]; self.tmp.cleanup()

    def test_inventory_reports_without_deleting(self):
        inv = uninstall.inventory()
        self.assertTrue(inv["database"]["exists"])
        self.assertGreater(inv["database"]["bytes"], 0)
        self.assertIn("activities", inv["counts"])
        self.assertEqual(inv["credential_references"], ["keychain://work-ledger/j"])
        self.assertTrue(paths.db_path().exists())          # nothing was removed

    def test_run_without_data_flag_deletes_nothing(self):
        out = uninstall.run()
        self.assertFalse(out["deleted"]["home"])
        self.assertTrue(paths.home().exists())

    def test_wrong_confirmation_refuses_and_deletes_nothing(self):
        out = uninstall.run(data=True, confirmation="yes")
        self.assertIsNotNone(out["refused"])
        self.assertFalse(out["deleted"]["home"])
        self.assertTrue(paths.db_path().exists())

    def test_typed_confirmation_removes_the_data_directory(self):
        out = uninstall.run(data=True, confirmation=uninstall.CONFIRMATION_PHRASE)
        self.assertIsNone(out["refused"])
        self.assertTrue(out["deleted"]["home"])
        self.assertFalse(paths.home().exists())

    def test_credential_deletion_is_never_implicit(self):
        # --delete-credentials without --data and the phrase must be a no-op.
        out = uninstall.run(delete_credentials=True)
        self.assertEqual(out["deleted"]["credentials"], [])
        self.assertTrue(paths.db_path().exists())


class TestUninstallKeychainDeletion(unittest.TestCase):
    """Code review IMPORTANT 1: a keychain deletion must only be reported
    as removed when `security` actually exits 0.

    Minor #7 (final review): `_delete_keychain_entry` now pins the binary
    to `credentials._SECURITY_BIN` ("/usr/bin/security") instead of a bare
    `"security"` resolved off PATH -- a PATH-prepended stub script can no
    longer intercept the call, so these stub `subprocess.run` itself
    (mirroring `tests/test_credentials.py`'s `TestKeychainStore`, which
    does the same for the sibling call sites)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WORK_LEDGER_HOME"] = os.path.join(self.tmp.name, "wl")
        paths.ensure_dirs()
        conn = dbc.connect(str(paths.db_path())); m.apply_all(conn)
        conn.execute("INSERT INTO connections(id,name,adapter,credential_reference,origin_device_id,"
                     "created_at,updated_at) VALUES ('c','j','jira-rest',"
                     "'keychain://work-ledger/j','d','t','t')")
        conn.close()

    def tearDown(self):
        del os.environ["WORK_LEDGER_HOME"]; self.tmp.cleanup()

    def test_successful_deletion_is_reported_as_removed(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            out = uninstall.run(data=True, delete_credentials=True,
                                confirmation=uninstall.CONFIRMATION_PHRASE)

        creds = out["deleted"]["credentials"]
        self.assertEqual(len(creds), 1)
        self.assertEqual(creds[0]["reference"], "keychain://work-ledger/j")
        self.assertTrue(creds[0]["removed"])
        self.assertEqual(run.call_args[0][0][0], "/usr/bin/security")

    def test_failed_deletion_is_reported_as_not_removed(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=44, stdout="", stderr="could not be found")
            out = uninstall.run(data=True, delete_credentials=True,
                                confirmation=uninstall.CONFIRMATION_PHRASE)

        creds = out["deleted"]["credentials"]
        self.assertEqual(len(creds), 1)
        self.assertFalse(creds[0]["removed"])
        self.assertTrue(creds[0]["detail"])

    def test_missing_security_binary_is_reported_as_not_removed(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
            out = uninstall.run(data=True, delete_credentials=True,
                                confirmation=uninstall.CONFIRMATION_PHRASE)
        creds = out["deleted"]["credentials"]
        self.assertEqual(len(creds), 1)
        self.assertFalse(creds[0]["removed"])

    def test_delete_uses_the_same_pinned_security_binary_as_credentials_py(self):
        from work_ledger import credentials
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            uninstall.run(data=True, delete_credentials=True,
                          confirmation=uninstall.CONFIRMATION_PHRASE)
        self.assertEqual(run.call_args[0][0][0], credentials._SECURITY_BIN)


class TestUninstallCliJsonOutput(unittest.TestCase):
    """Code review IMPORTANT 2: `uninstall --data --format json` must emit
    exactly the JSON payload on stdout -- no human-readable inventory text
    or confirmation prompt mixed in."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WORK_LEDGER_HOME"] = os.path.join(self.tmp.name, "wl")
        paths.ensure_dirs()
        conn = dbc.connect(str(paths.db_path())); m.apply_all(conn)
        conn.close()

    def tearDown(self):
        del os.environ["WORK_LEDGER_HOME"]; self.tmp.cleanup()

    def test_data_format_json_emits_only_json_on_stdout(self):
        buf = io.StringIO()
        with mock.patch("builtins.input", return_value=uninstall.CONFIRMATION_PHRASE):
            with contextlib.redirect_stdout(buf):
                rc = cli.main(["uninstall", "--data", "--format", "json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())  # raises if stdout isn't pure JSON
        self.assertIsNone(payload["refused"])
        self.assertTrue(payload["deleted"]["home"])

    def test_no_data_format_json_also_emits_only_json_on_stdout(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["uninstall", "--format", "json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertFalse(payload["deleted"]["home"])
        self.assertTrue(paths.db_path().exists())
