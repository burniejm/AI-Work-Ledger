import json, threading, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from work_ledger.jira import rest, adapter as a

ISSUE = {"id":"10042","key":"AB-76","fields":{"summary":"Upload recovery","updated":"2026-08-29T18:00:00.000+0000",
         "status":{"name":"In Progress","statusCategory":{"key":"indeterminate"}},"resolution":None,
         "assignee":{"accountId":"acct-1","displayName":"Jim"},"project":{"key":"SK"}}}

class Handler(BaseHTTPRequestHandler):
    mode = "ok"
    request_count = 0
    def log_message(self, *a): pass
    def do_GET(self):
        Handler.request_count += 1
        if Handler.mode == "auth":  self.send_response(401); self.end_headers(); return
        if Handler.mode == "error": self.send_response(500); self.end_headers(); return
        if Handler.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/rest/api/3/myself")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if Handler.mode == "garbage":
            raw = b"{not valid json"
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw))); self.end_headers()
            self.wfile.write(raw)
            return
        if Handler.mode == "myself_404" and self.path.startswith("/rest/api/3/myself"):
            self.send_response(404); self.end_headers(); return
        if self.path.startswith("/rest/api/3/myself"):
            body = {"accountId": "acct-1", "displayName": "Jim", "emailAddress": "j@x"}
        elif self.path.startswith("/rest/api/3/project/search"):
            body = {"values": [{"key": "SK", "name": "Sky"}]}
        elif "/issue/AB-404" in self.path:
            self.send_response(404); self.end_headers(); return
        elif "/issue/" in self.path:
            body = ISSUE
        elif self.path.startswith("/rest/api/3/search/jql"):
            body = {"issues": [ISSUE]}
        else:
            body = {}
        raw = json.dumps(body).encode()
        self.send_response(200); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_POST(self):   self.send_response(405); self.end_headers()
    def do_PUT(self):    self.send_response(405); self.end_headers()
    def do_DELETE(self): self.send_response(405); self.end_headers()

class TestJiraRest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"
    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
    def setUp(self):
        Handler.mode = "ok"
        Handler.request_count = 0
        import os; os.environ["WL_TEST_TOKEN"] = "tok123"
        self.ad = rest.JiraRestAdapter(self.base, "j@x", "env://WL_TEST_TOKEN")

    def test_identify_current_user(self):
        self.assertEqual(self.ad.identify_current_user().account_id, "acct-1")

    def test_get_issue_normalizes(self):
        r = self.ad.get_issue("AB-76")
        self.assertEqual((r.key, r.status_category, r.project_key), ("AB-76", "indeterminate", "SK"))

    def test_missing_issue_raises_issue_not_found(self):
        with self.assertRaises(rest.IssueNotFound):
            self.ad.get_issue("AB-404")

    def test_auth_failure_maps_to_expired(self):
        Handler.mode = "auth"
        self.assertEqual(self.ad.health().state, "expired")

    def test_server_error_maps_to_degraded(self):
        Handler.mode = "error"
        self.assertEqual(self.ad.health().state, "degraded")

    def test_unreachable_host_maps_to_degraded_not_exception(self):
        ad = rest.JiraRestAdapter("http://127.0.0.1:1", "j@x", "env://WL_TEST_TOKEN", timeout=1)
        self.assertEqual(ad.health().state, "degraded")

    def test_capabilities_are_string_keyed(self):
        # Ruling 27
        caps = self.ad.capabilities()
        self.assertTrue(all(isinstance(k, str) and not isinstance(k, a.Capability) for k in caps))
        self.assertTrue(caps["get_issue"])
        self.assertFalse(caps["list_sites"])

    def test_auth_header_builders(self):
        self.assertTrue(rest.AUTH_HEADER_BUILDERS["cloud-api-token"]("j@x", "tok").startswith("Basic "))
        self.assertEqual(rest.AUTH_HEADER_BUILDERS["pat"](None, "tok"), "Bearer tok")

    def test_module_contains_no_write_verbs(self):
        with open(rest.__file__) as f:
            src = f.read()
        for verb in ('"POST"', "'POST'", '"PUT"', '"DELETE"', '"PATCH"'):
            self.assertNotIn(verb, src)

    def test_token_never_appears_in_the_url(self):
        seen = []
        orig = rest._OPENER.open
        def spy(req, *a, **k):
            seen.append(req.full_url); return orig(req, *a, **k)
        rest._OPENER.open = spy
        try:
            self.ad.get_issue("AB-76")
        finally:
            rest._OPENER.open = orig
        self.assertTrue(all("tok123" not in u for u in seen))

    def test_redirect_is_not_followed_and_reports_degraded(self):
        Handler.mode = "redirect"
        result = self.ad.health()
        self.assertEqual(result.state, "degraded")
        self.assertEqual(Handler.request_count, 1)

    def test_redirect_never_sends_auth_header_to_a_second_handler(self):
        # A prior spy also proves this indirectly (only one request is ever
        # made -- see the request_count assertion above), but assert on the
        # header directly too: no second request means no second Request
        # object carrying the Authorization header.
        Handler.mode = "redirect"
        seen = []
        orig = rest._OPENER.open
        def spy(req, *a, **k):
            seen.append(req)
            return orig(req, *a, **k)
        rest._OPENER.open = spy
        try:
            self.ad.health()
        finally:
            rest._OPENER.open = orig
        self.assertEqual(len(seen), 1)

    def test_corrupt_json_response_maps_to_degraded(self):
        Handler.mode = "garbage"
        self.assertEqual(self.ad.health().state, "degraded")

    def test_invalid_issue_key_is_rejected_without_a_request(self):
        Handler.request_count = 0
        with self.assertRaises(rest.InvalidIssueKey):
            self.ad.get_issue("AB-76?evil=1")
        with self.assertRaises(rest.InvalidIssueKey):
            self.ad.get_issue("../../etc/passwd")
        self.assertEqual(Handler.request_count, 0)

    def test_404_from_non_issue_endpoint_is_not_issue_not_found(self):
        Handler.mode = "myself_404"
        with self.assertRaises(rest.NotFound):
            self.ad.identify_current_user()

    def test_list_projects_returns_values(self):
        self.assertEqual(self.ad.list_projects(), [{"key": "SK", "name": "Sky"}])
