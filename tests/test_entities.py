import os, tempfile, unittest
from work_ledger.db import connection as dbc, migrations as m
from work_ledger import entities as e

class TestEntities(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite"))
        m.apply_all(self.conn)
    def tearDown(self):
        self.tmp.cleanup()

    def test_device_is_created_once(self):
        self.assertEqual(e.current_device(self.conn), e.current_device(self.conn))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM devices").fetchone()[0], 1)

    def test_slugify(self):
        self.assertEqual(e.slugify("Tech Genie"), "tech-genie")
        self.assertEqual(e.slugify("Acme"), "acme")

    def test_upsert_client_is_idempotent(self):
        a = e.upsert_client(self.conn, "Acme")
        b = e.upsert_client(self.conn, "acme")   # same slug
        self.assertEqual(a, b)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM clients").fetchone()[0], 1)

    def test_alias_resolution(self):
        cid = e.upsert_client(self.conn, "Portal")
        e.add_alias(self.conn, "client", "TG", "client", cid, source="correction")
        e.add_alias(self.conn, "client", "tg", "client", cid, source="correction")  # idempotent
        self.assertEqual(self.conn.execute("SELECT count(*) FROM aliases").fetchone()[0], 1)
        self.assertEqual(e.get_client_by_name(self.conn, "TG")["id"], cid)

    def test_resolve_scope_prefers_longest_prefix(self):
        c1 = e.upsert_client(self.conn, "Globex"); p1 = e.upsert_project(self.conn, c1, "Portal")
        c2 = e.upsert_client(self.conn, "Acme"); p2 = e.upsert_project(self.conn, c2, "Replay")
        r = e.upsert_repository(self.conn, "/Users/x/mono")
        e.map_repository(self.conn, r, client_id=c1, project_id=p1, path_prefix="", confidence="medium", source="inference")
        e.map_repository(self.conn, r, client_id=c2, project_id=p2, path_prefix="apps/replay",
                         confidence="high", source="setup")
        cid, pid, conf, _ = e.resolve_scope_for_path(self.conn, "/Users/x/mono/apps/replay/src/main.go")
        self.assertEqual((cid, pid, conf), (c2, p2, "high"))
        cid, pid, conf, _ = e.resolve_scope_for_path(self.conn, "/Users/x/mono/other/file.go")
        self.assertEqual((cid, pid), (c1, p1))

    def test_unknown_path_returns_none_without_raising(self):
        self.assertEqual(e.resolve_scope_for_path(self.conn, "/tmp/nowhere")[:2], (None, None))

    def test_soft_delete_rejects_unknown_table(self):
        with self.assertRaises(ValueError):
            e.soft_delete(self.conn, "sqlite_master; DROP TABLE clients", "x")

    def test_upsert_repository_bare_recall_preserves_custom_name_and_disapproval(self):
        r1 = e.upsert_repository(self.conn, "/Users/x/repo", name="Custom Name", approved=False)
        r2 = e.upsert_repository(self.conn, "/Users/x/repo")   # bare re-call: no name/approved given
        self.assertEqual(r1, r2)
        row = self.conn.execute("SELECT name, approved FROM repositories WHERE id=?", (r1,)).fetchone()
        self.assertEqual(row["name"], "Custom Name")
        self.assertEqual(row["approved"], 0)

    def test_upsert_repository_explicit_disapproval_survives_bare_recall(self):
        r1 = e.upsert_repository(self.conn, "/Users/x/repo2")
        e.upsert_repository(self.conn, "/Users/x/repo2", approved=False)
        e.upsert_repository(self.conn, "/Users/x/repo2")       # bare re-call
        row = self.conn.execute("SELECT approved FROM repositories WHERE id=?", (r1,)).fetchone()
        self.assertEqual(row["approved"], 0)

    def test_upsert_repository_explicit_values_do_update(self):
        r1 = e.upsert_repository(self.conn, "/Users/x/repo3", name="Old Name", approved=False)
        e.upsert_repository(self.conn, "/Users/x/repo3", name="New Name", default_branch="main",
                             remotes=["origin"], approved=True)
        row = self.conn.execute(
            "SELECT name, default_branch, remotes_json, approved FROM repositories WHERE id=?", (r1,)
        ).fetchone()
        self.assertEqual(row["name"], "New Name")
        self.assertEqual(row["default_branch"], "main")
        self.assertEqual(row["remotes_json"], '["origin"]')
        self.assertEqual(row["approved"], 1)

    def test_device_name_config_overrides_the_hostname(self):
        # Ruling 29: config `device_name` is consumed here and nowhere else.
        import os, tempfile as tf
        from work_ledger import config, paths
        with tf.TemporaryDirectory() as home:
            os.environ["WORK_LEDGER_HOME"] = home
            try:
                paths.ensure_dirs()
                paths.config_path().write_text("device_name: jim-laptop\n")
                did = e.current_device(self.conn)
                row = self.conn.execute("SELECT hostname FROM devices WHERE id=?", (did,)).fetchone()
                self.assertEqual(row["hostname"], "jim-laptop")
                self.assertEqual(self.conn.execute("SELECT count(*) FROM devices").fetchone()[0], 1)
            finally:
                del os.environ["WORK_LEDGER_HOME"]
