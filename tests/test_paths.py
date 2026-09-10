import os, tempfile, unittest
from pathlib import Path
from work_ledger import paths

class TestPaths(unittest.TestCase):
    def test_home_honours_env(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["WORK_LEDGER_HOME"] = d
            try:
                self.assertEqual(paths.home(), Path(d))
                self.assertEqual(paths.db_path(), Path(d) / "work-ledger.sqlite")
            finally:
                del os.environ["WORK_LEDGER_HOME"]

    def test_ensure_dirs_creates_0700(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["WORK_LEDGER_HOME"] = str(Path(d) / "wl")
            try:
                paths.ensure_dirs()
                for sub in ("backups", "logs", "state", "exports"):
                    p = paths.home() / sub
                    self.assertTrue(p.is_dir())
                    self.assertEqual(oct(p.stat().st_mode & 0o777), "0o700")
            finally:
                del os.environ["WORK_LEDGER_HOME"]
