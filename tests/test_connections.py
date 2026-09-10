import os, tempfile, unittest
from work_ledger.db import connection as dbc, migrations as m
from work_ledger import connections as C

class TestConnections(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite")); m.apply_all(self.conn)
    def tearDown(self):
        self.tmp.cleanup()

    def test_upsert_is_idempotent(self):
        a = C.upsert_connection(self.conn, name="acme-jira", adapter="mcp", client_hint="Acme",
                                ticket_patterns=["AB-*"])
        b = C.upsert_connection(self.conn, name="acme-jira", adapter="mcp", client_hint="Acme",
                                ticket_patterns=["AB-*", "SC-*"])
        self.assertEqual(a, b)
        self.assertEqual(len(C.list_connections(self.conn)), 1)
        row = C.list_connections(self.conn)[0]
        self.assertIn("SC-*", row["ticket_patterns_json"])

    def test_multiple_sites_are_independent(self):
        C.upsert_connection(self.conn, name="acme-jira", adapter="mcp")
        C.upsert_connection(self.conn, name="globex-jira", adapter="jira-rest",
                            base_url="https://globex.atlassian.net")
        self.assertEqual(len(C.list_connections(self.conn)), 2)

    def test_capabilities_are_upserted_not_duplicated(self):
        cid = C.upsert_connection(self.conn, name="j", adapter="mcp")
        C.record_capabilities(self.conn, cid, {"get_issue": True, "search_issues": False})
        C.record_capabilities(self.conn, cid, {"get_issue": True, "search_issues": True})
        rows = list(self.conn.execute("SELECT capability, supported FROM connection_capabilities ORDER BY capability"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(dict((r["capability"], r["supported"]) for r in rows), {"get_issue": 1, "search_issues": 1})

    def test_remove_clears_the_reference_but_not_the_secret_by_default(self):
        cid = C.upsert_connection(self.conn, name="j", adapter="jira-rest",
                                  credential_reference="keychain://work-ledger/j")
        out = C.remove(self.conn, "j")
        self.assertFalse(out["credential_deleted"])
        row = self.conn.execute("SELECT credential_reference, deleted_at FROM connections WHERE id=?", (cid,)).fetchone()
        self.assertIsNone(row["credential_reference"])
        self.assertIsNotNone(row["deleted_at"])

    def test_credential_deletion_requires_explicit_confirmation(self):
        C.upsert_connection(self.conn, name="j2", adapter="jira-rest", credential_reference="keychain://work-ledger/j2")
        with self.assertRaises(ValueError):
            C.remove(self.conn, "j2", delete_credential=True, confirmed=False)

    def test_upsert_rejects_a_raw_secret_as_credential_reference(self):
        # Important #1: a pasted raw token must never reach SQLite.
        with self.assertRaises(ValueError) as ctx:
            C.upsert_connection(self.conn, name="j", adapter="jira-rest",
                                credential_reference="hunter2-super-secret-token")
        self.assertNotIn("hunter2-super-secret-token", str(ctx.exception))
        self.assertEqual(len(C.list_connections(self.conn, include_disabled=True)), 0)

    def test_upsert_rejects_a_credential_reference_with_wrong_scheme(self):
        with self.assertRaises(ValueError):
            C.upsert_connection(self.conn, name="j", adapter="jira-rest",
                                credential_reference="https://example.com/token")

    def test_upsert_rejects_a_syntactically_valid_reference_that_looks_like_a_secret(self):
        # Second layer of the check (redact.looks_like_secret): a value that
        # parses as keychain://service/account syntactically, but whose
        # account portion is itself secret-shaped once whitespace breaks the
        # bare-reference exemption.
        with self.assertRaises(ValueError):
            C.upsert_connection(self.conn, name="j", adapter="jira-rest",
                                credential_reference="keychain://svc/api_key: leaked-token-xyz")

    def test_upsert_accepts_keychain_and_env_references(self):
        C.upsert_connection(self.conn, name="j", adapter="jira-rest",
                            credential_reference="keychain://work-ledger/j")
        C.upsert_connection(self.conn, name="j2", adapter="jira-rest",
                            credential_reference="env://JIRA_TOKEN")

    def test_upsert_rejects_base_url_with_embedded_userinfo(self):
        with self.assertRaises(ValueError):
            C.upsert_connection(self.conn, name="j", adapter="jira-rest",
                                base_url="https://user:hunter2@globex.atlassian.net")
        self.assertEqual(len(C.list_connections(self.conn, include_disabled=True)), 0)

    def test_upsert_accepts_an_ordinary_base_url(self):
        C.upsert_connection(self.conn, name="j", adapter="jira-rest",
                            base_url="https://globex.atlassian.net")

    def test_repair_rejects_a_raw_secret_credential_reference(self):
        C.upsert_connection(self.conn, name="j", adapter="jira-rest",
                            credential_reference="keychain://work-ledger/j")
        with self.assertRaises(ValueError) as ctx:
            C.repair(self.conn, "j", credential_reference="a-raw-token-value-123456")
        self.assertNotIn("a-raw-token-value-123456", str(ctx.exception))
        row = self.conn.execute(
            "SELECT credential_reference FROM connections WHERE name='j'"
        ).fetchone()
        self.assertEqual(row["credential_reference"], "keychain://work-ledger/j")

    def test_connections_module_does_not_import_jira_adapters(self):
        # Ruling 13: the connections <-> jira cycle is broken; build_adapter lives in
        # jira/factory.py (Task 34). Only *module-level* (module-load-time) imports
        # count toward the cycle -- Task 35's `test_connection`/`repair` take a
        # lazily-imported default (`from work_ledger.jira.factory import
        # build_adapter` inside `_default_builder`'s function body), which never
        # runs at module-import time and so never contributes to a load-time
        # cycle. Only `tree.body` (the module's own top-level statements) is
        # walked -- not `ast.walk`, which would also descend into every nested
        # function/class body and flag exactly the lazy import Ruling 13 asks for.
        import ast, inspect
        tree = ast.parse(inspect.getsource(C))
        imported = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for forbidden in ("work_ledger.jira.rest", "work_ledger.jira.mcp",
                          "work_ledger.jira.refresh", "work_ledger.jira.factory",
                          "jira.rest", "jira.mcp", "jira.refresh", "jira.factory",
                          ".jira.rest", ".jira.mcp", ".jira.refresh", ".jira.factory"):
            self.assertNotIn(forbidden, imported)
        self.assertFalse(hasattr(C, "build_adapter"))
