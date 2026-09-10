import json, os, tempfile, threading, time, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock
from work_ledger.db import connection as dbc, migrations as m
from work_ledger.jira import refresh
from work_ledger.jira.adapter import IssueResult
from work_ledger import connections as C, work_items as wi, remaining as r

ISSUE = {"id":"1","key":"AB-76","fields":{"summary":"Upload recovery","updated":"2026-08-29T18:00:00.000+0000",
         "status":{"name":"Done","statusCategory":{"key":"done"}},"resolution":{"name":"Fixed"},
         "assignee":{"accountId":"acct-1","displayName":"Jim"},"project":{"key":"SK"}}}

class H(BaseHTTPRequestHandler):
    mode = "ok"
    def log_message(self, *a): pass
    def do_GET(self):
        if H.mode == "down": self.send_response(503); self.end_headers(); return
        if H.mode == "auth": self.send_response(401); self.end_headers(); return
        body = {"accountId":"acct-1","displayName":"Jim"} if "myself" in self.path else ISSUE
        raw = json.dumps(body).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

class TestRefresh(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"
    @classmethod
    def tearDownClass(cls): cls.srv.shutdown()

    def setUp(self):
        H.mode = "ok"
        os.environ["WL_TEST_TOKEN"] = "tok"
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite")); m.apply_all(self.conn)
        self.cid = C.upsert_connection(self.conn, name="j", adapter="jira-rest", base_url=self.base,
                                       username="j@x", auth_method="cloud-api-token",
                                       credential_reference="env://WL_TEST_TOKEN", ticket_patterns=["AB-*"])
        self.wid = wi.upsert_external(self.conn, self.cid, "AB-76", title="Upload recovery")
    def tearDown(self): self.tmp.cleanup()

    def _row(self):
        return C.list_connections(self.conn)[0]

    def test_refresh_updates_and_clears_staleness(self):
        out = refresh.refresh_rest(self.conn, self._row(), limit=10, now="2026-08-30T12:00:00.000Z")
        self.assertEqual(out["updated"], 1)
        row = self.conn.execute("SELECT jira_status, jira_stale FROM work_items WHERE id=?", (self.wid,)).fetchone()
        self.assertEqual((row["jira_status"], row["jira_stale"]), ("Done", 0))

    def test_outage_degrades_without_losing_history(self):
        H.mode = "down"
        before = self.conn.execute("SELECT count(*) FROM work_items").fetchone()[0]
        out = refresh.refresh_rest(self.conn, self._row(), limit=10, now="2026-08-30T12:00:00.000Z")
        self.assertEqual(out["health"], "degraded")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_items").fetchone()[0], before)
        self.assertEqual(self.conn.execute("SELECT jira_stale FROM work_items WHERE id=?", (self.wid,)).fetchone()[0], 1)

    def test_expired_credentials_mark_expired_and_keep_history(self):
        H.mode = "auth"
        refresh.refresh_rest(self.conn, self._row(), limit=10, now="2026-08-30T12:00:00.000Z")
        self.assertEqual(C.list_connections(self.conn)[0]["health_state"], "expired")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_items").fetchone()[0], 1)

    def test_refresh_opens_contradictions(self):
        r.create(self.conn, title="Retest", normalized_key="retest", work_item_id=self.wid,
                 confidence="high", source="stage_a", occurred_at="2026-08-29T12:00:00.000Z")
        refresh.refresh_rest(self.conn, self._row(), limit=10, now="2026-08-30T12:00:00.000Z")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM contradictions WHERE rule='jira_done_open_remaining' AND status='open'"
        ).fetchone()[0], 1)

    def test_mcp_connections_are_skipped_by_hook_refresh(self):
        mcid = C.upsert_connection(self.conn, name="m", adapter="mcp", mcp_server_name="atlassian")
        wi.upsert_external(self.conn, mcid, "VV-1", title="Other")
        out = refresh.refresh_all_rest(self.conn, limit=10)
        self.assertIn("m", out["skipped_mcp"])
        self.assertEqual(self.conn.execute(
            "SELECT jira_stale FROM work_items WHERE external_key='VV-1'").fetchone()[0], 1)

    def test_identity_attribution_requires_a_configured_identity(self):
        from work_ledger.jira.adapter import IssueResult
        issue = IssueResult.from_cloud_payload(ISSUE)
        self.assertFalse(refresh.attribute_identity(self.conn, self.cid, issue))
        self.conn.execute("INSERT INTO user_identities(id,kind,connection_id,value,origin_device_id,created_at,updated_at)"
                          " VALUES ('u1','jira',?, 'acct-1','d','t','t')", (self.cid,))
        self.assertTrue(refresh.attribute_identity(self.conn, self.cid, issue))

    def test_connection_test_reports_each_check(self):
        out = C.test_connection(self.conn, "j")
        names = [c["name"] for c in out["checks"]]
        self.assertIn("authentication", names)
        self.assertIn("current_user", names)
        self.assertIn("issue_retrieval", names)
        self.assertEqual(out["state"], "healthy")

    def test_repair_updates_health_without_deleting_history(self):
        H.mode = "auth"
        C.test_connection(self.conn, "j")
        H.mode = "ok"
        out = C.repair(self.conn, "j")
        self.assertEqual(out["state"], "healthy")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_items").fetchone()[0], 1)

    def test_refresh_rest_stops_at_deadline_and_reports_partial(self):
        # CRITICAL 1 (code review): refresh_rest must not blow past a
        # caller-supplied wall-clock deadline just because more stale items
        # remain -- it checks `deadline` before each item fetch and stops
        # cleanly, the same pattern as capture.sessions.reconcile_transcript.
        wid2 = wi.upsert_external(self.conn, self.cid, "AB-77", title="Second")

        class SlowAdapter:
            calls = []
            def get_issue(self, key):
                time.sleep(0.05)
                SlowAdapter.calls.append(key)
                return IssueResult.from_cloud_payload(ISSUE)

        SlowAdapter.calls = []
        with mock.patch("work_ledger.jira.refresh.factory.build_adapter",
                        return_value=SlowAdapter()):
            deadline = time.monotonic() + 0.03
            out = refresh.refresh_rest(self.conn, self._row(), limit=10,
                                       now="2026-08-30T12:00:00.000Z", deadline=deadline)

        self.assertTrue(out.get("partial"))
        self.assertEqual(out["updated"], 1)
        self.assertEqual(len(SlowAdapter.calls), 1)

    def test_refresh_all_rest_propagates_partial_from_a_cut_short_connection(self):
        wi.upsert_external(self.conn, self.cid, "AB-77", title="Second")

        class SlowAdapter:
            calls = []
            def get_issue(self, key):
                time.sleep(0.05)
                SlowAdapter.calls.append(key)
                return IssueResult.from_cloud_payload(ISSUE)

        SlowAdapter.calls = []
        with mock.patch("work_ledger.jira.refresh.factory.build_adapter",
                        return_value=SlowAdapter()):
            deadline = time.monotonic() + 0.03
            out = refresh.refresh_all_rest(self.conn, limit=10, deadline=deadline)

        self.assertTrue(out.get("partial"))
        self.assertEqual(out["updated"], 1)

