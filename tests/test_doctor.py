import os, sqlite3, tempfile, unittest
from unittest import mock
from work_ledger.db import connection as dbc, migrations as m
from work_ledger import doctor

class TestDoctor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name
    def tearDown(self):
        del os.environ["WORK_LEDGER_HOME"]; self.tmp.cleanup()

    def test_reports_every_named_check(self):
        rep = doctor.run()
        self.assertEqual([c["name"] for c in rep["checks"]], doctor.CHECKS)
        self.assertIn(rep["overall"], ("healthy", "degraded", "unhealthy"))

    def test_detects_synced_filesystem(self):
        state, detail = doctor.check_synced_filesystem("/Users/x/Dropbox/work-ledger")
        self.assertEqual(state, "fail")
        self.assertIn("Dropbox", detail)
        self.assertEqual(doctor.check_synced_filesystem("/Users/x/.claude/work-ledger")[0], "ok")

    def test_integrity_and_schema_checks_pass_on_a_fresh_db(self):
        conn = dbc.connect(os.path.join(self.tmp.name, "work-ledger.sqlite")); m.apply_all(conn)
        rep = doctor.run(conn)
        by = {c["name"]: c for c in rep["checks"]}
        self.assertEqual(by["integrity"]["state"], "ok")
        self.assertEqual(by["schema_version"]["state"], "ok")
        self.assertEqual(by["wal_mode"]["state"], "ok")

    def test_failed_checkpoints_are_surfaced(self):
        conn = dbc.connect(os.path.join(self.tmp.name, "work-ledger.sqlite")); m.apply_all(conn)
        conn.execute("INSERT INTO capture_checkpoints(id,session_id,hook_event,checkpoint_key,status,detail,"
                     "started_at,origin_device_id,created_at) VALUES "
                     "('x','s','Stop','ck-1','failed','database busy','2026-08-30T00:00:00.000Z','d','2026-08-30T00:00:00.000Z')")
        by = {c["name"]: c for c in doctor.run(conn)["checks"]}
        self.assertEqual(by["checkpoint_failures"]["state"], "warn")
        self.assertIn("database busy", by["checkpoint_failures"]["detail"])

    def test_never_prints_a_secret(self):
        conn = dbc.connect(os.path.join(self.tmp.name, "work-ledger.sqlite")); m.apply_all(conn)
        conn.execute("INSERT INTO connections(id,name,adapter,credential_reference,origin_device_id,created_at,updated_at)"
                     " VALUES ('c','j','jira-rest','keychain://work-ledger/j','d','t','t')")
        text = repr(doctor.run(conn))
        self.assertNotIn("ATATT", text)

    def test_absent_subsystems_warn_and_never_fail(self):
        # Ruling 19: on a fresh install nothing is configured yet; doctor degrades, never fails.
        conn = dbc.connect(os.path.join(self.tmp.name, "work-ledger.sqlite")); m.apply_all(conn)
        rep = doctor.run(conn)
        by = {c["name"]: c for c in rep["checks"]}
        for name in ("hooks_installed", "transcripts_readable", "git_identity",
                     "connections", "stage_b", "subagent_capture"):
            self.assertIn(by[name]["state"], ("ok", "warn"), f"{name} must not fail when absent")
            if by[name]["state"] == "warn":
                self.assertTrue(by[name]["detail"])
        self.assertNotEqual(rep["overall"], "unhealthy")
        self.assertIn(doctor.NOT_YET_CONFIGURED, " ".join(c["detail"] or "" for c in rep["checks"]))


class TestDoctorFsDetection(unittest.TestCase):
    """Code review IMPORTANT 3: network-fs detection must not silently
    claim `ok` on a platform with no detection strategy, and must still
    catch a real network fs type when the platform's tool reports one."""

    def test_warns_when_platform_offers_no_detection_strategy(self):
        with mock.patch.object(doctor.sys, "platform", "win32"):
            state, detail = doctor.check_synced_filesystem("/some/path")
        self.assertEqual(state, "warn")
        self.assertIn("network-fs detection unavailable", detail)

    def test_ok_on_darwin_for_an_unresolvable_fake_path(self):
        # macOS always has a detection strategy (stat -f); a path that
        # can't be stat'd (doesn't exist) is inconclusive, not a finding.
        with mock.patch.object(doctor.sys, "platform", "darwin"):
            state, _ = doctor.check_synced_filesystem("/Users/x/.claude/work-ledger")
        self.assertEqual(state, "ok")

    def test_fails_when_macos_stat_reports_a_network_fs_type(self):
        fake_proc = mock.Mock(returncode=0, stdout="nfs\n")
        with mock.patch.object(doctor.sys, "platform", "darwin"), \
             mock.patch.object(doctor.subprocess, "run", return_value=fake_proc):
            state, detail = doctor.check_synced_filesystem("/Volumes/remote")
        self.assertEqual(state, "fail")
        self.assertIn("nfs", detail)

    def test_fails_when_linux_proc_mounts_reports_a_network_fs_type(self):
        mounts = "\n".join([
            "/dev/sda1 / ext4 rw 0 0",
            "server:/export /mnt/data nfs4 rw 0 0",
        ])
        with mock.patch.object(doctor.sys, "platform", "linux"), \
             mock.patch("builtins.open", mock.mock_open(read_data=mounts)), \
             mock.patch.object(doctor.os.path, "realpath", return_value="/mnt/data/work-ledger"):
            state, detail = doctor.check_synced_filesystem("/mnt/data/work-ledger")
        self.assertEqual(state, "fail")
        self.assertIn("nfs4", detail)


class TestDoctorConnectionLeak(unittest.TestCase):
    """Code review minor: `run()` must close the connection it opened for
    itself even when `migrations.ensure_current` raises."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name
    def tearDown(self):
        del os.environ["WORK_LEDGER_HOME"]; self.tmp.cleanup()

    def test_own_connection_is_closed_when_migrations_fail(self):
        opened = {}
        real_connect = dbc.connect

        def spy_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            opened["conn"] = conn
            return conn

        with mock.patch.object(doctor.dbc, "connect", side_effect=spy_connect), \
             mock.patch.object(doctor.migrations, "ensure_current", side_effect=RuntimeError("boom")):
            doctor.run()

        self.assertIn("conn", opened)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened["conn"].execute("SELECT 1")
