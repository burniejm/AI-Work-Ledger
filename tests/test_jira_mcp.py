import io
import contextlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from work_ledger.db import connection as dbc, migrations as m
from work_ledger.jira import mcp
from work_ledger import connections as C

ISSUE = {"id":"10042","key":"AB-76","fields":{"summary":"Upload recovery","updated":"2026-08-29T18:00:00.000+0000",
         "status":{"name":"Done","statusCategory":{"key":"done"}},"resolution":{"name":"Fixed"},
         "assignee":{"accountId":"acct-1","displayName":"Jim"},"project":{"key":"SK"}}}


class TestMcp(unittest.TestCase):
    def setUp(self):
        # WORK_LEDGER_HOME is set (and the ledger opened at its default path)
        # so nothing in this module can reach the real home directory:
        # `contradictions.evaluate` -> `config.get` would otherwise create
        # ~/.claude/work-ledger/config.yaml, and `JiraMcpAdapter.capabilities()`
        # falls back to `dbc.connect()` when it was built without a connection.
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("WORK_LEDGER_HOME")
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name
        self.conn = dbc.connect(); m.apply_all(self.conn)
        self.cid = C.upsert_connection(self.conn, name="acme-jira", adapter="mcp",
                                       mcp_server_name="atlassian", ticket_patterns=["AB-*"])
    def tearDown(self):
        self.conn.close()
        if self._old_home is None:
            os.environ.pop("WORK_LEDGER_HOME", None)
        else:
            os.environ["WORK_LEDGER_HOME"] = self._old_home
        self.tmp.cleanup()

    def test_discovery_reads_claude_json_without_credentials(self):
        p = Path(self.tmp.name) / "claude.json"
        p.write_text(json.dumps({"mcpServers": {"atlassian": {"command": "npx",
                     "env": {"ATLASSIAN_API_TOKEN": "ATATT3xFfGF0SUPERSECRETVALUE0000"}}}}))
        servers = mcp.discover_servers(claude_json_path=str(p), run_cli=False)
        self.assertEqual(servers[0]["name"], "atlassian")
        self.assertNotIn("ATATT3xFfGF0SUPERSECRETVALUE0000", json.dumps(servers))

    def test_capability_mapping_from_tool_names(self):
        supported, tools = mcp.map_capabilities(
            ["mcp__atlassian__getJiraIssue", "mcp__atlassian__atlassianUserInfo",
             "mcp__atlassian__searchJiraIssuesUsingJql"])
        self.assertTrue(supported["get_issue"]); self.assertTrue(supported["identify_current_user"])
        self.assertFalse(supported["get_user_activity"])
        self.assertEqual(tools["get_issue"], "mcp__atlassian__getJiraIssue")

    def test_adapter_calls_raise_model_mediation_required(self):
        row = C.list_connections(self.conn)[0]
        ad = mcp.JiraMcpAdapter(row)
        with self.assertRaises(mcp.ModelMediationRequired):
            ad.get_issue("AB-76")

    def test_plan_refresh_names_the_tool_and_keys(self):
        from work_ledger import work_items as wi
        wi.upsert_external(self.conn, self.cid, "AB-76", title="Upload recovery")
        C.record_capabilities(self.conn, self.cid, {"get_issue": True},
                              tool_names={"get_issue": "mcp__atlassian__getJiraIssue"})
        plan = mcp.plan_refresh(self.conn, limit=10)
        self.assertEqual(plan["connections"][0]["tool"], "mcp__atlassian__getJiraIssue")
        self.assertIn("AB-76", plan["connections"][0]["issue_keys"])

    def test_ingest_updates_work_items_and_clears_staleness(self):
        from work_ledger import work_items as wi
        wid = wi.upsert_external(self.conn, self.cid, "AB-76", title="Upload recovery")
        out = mcp.ingest(self.conn, "acme-jira", {"issues": [ISSUE]})
        self.assertEqual(out["updated"], 1)
        row = self.conn.execute("SELECT jira_status, jira_status_category, jira_stale FROM work_items WHERE id=?",
                                (wid,)).fetchone()
        self.assertEqual((row["jira_status"], row["jira_status_category"], row["jira_stale"]), ("Done", "done", 0))

    def test_ingest_rejects_malformed_entries(self):
        out = mcp.ingest(self.conn, "acme-jira",
                         [ISSUE, "a string", {"no_key": 1}, {"key": "'; DROP TABLE work_items; --"}])
        self.assertEqual(out["updated"], 1)
        self.assertEqual(len(out["rejected"]), 3)
        self.assertIsNotNone(self.conn.execute(
            "SELECT name FROM sqlite_master WHERE name='work_items'").fetchone())

    def test_ingest_is_idempotent(self):
        mcp.ingest(self.conn, "acme-jira", {"issues": [ISSUE]})
        before = self.conn.execute("SELECT count(*) FROM work_items").fetchone()[0]
        mcp.ingest(self.conn, "acme-jira", {"issues": [ISSUE]})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_items").fetchone()[0], before)

    def test_factory_builds_the_right_adapter_without_a_cycle(self):
        # Ruling 13: build_adapter lives in jira/factory.py; connections.py stays clean.
        import ast, inspect
        from work_ledger.jira import factory
        from work_ledger.jira import rest
        row = C.list_connections(self.conn)[0]
        self.assertIsInstance(factory.build_adapter(self.conn, row), mcp.JiraMcpAdapter)
        rid = C.upsert_connection(self.conn, name="r", adapter="jira-rest",
                                  base_url="http://127.0.0.1:9", username="j@x",
                                  credential_reference="env://WL_TEST_TOKEN")
        rrow = [r for r in C.list_connections(self.conn) if r["id"] == rid][0]
        self.assertIsInstance(factory.build_adapter(self.conn, rrow), rest.JiraRestAdapter)
        tree = ast.parse(inspect.getsource(factory))
        mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertFalse(any("connections" in mod for mod in mods))

    def test_capability_dicts_use_string_keys_end_to_end(self):
        # Ruling 27
        from work_ledger.jira import adapter as a
        self.assertEqual(set(mcp.CAPABILITY_TOOL_HINTS), a.CAPABILITY_VALUES)
        self.assertTrue(all(isinstance(k, str) for k in mcp.CAPABILITY_TOOL_HINTS))
        supported, tools = mcp.map_capabilities(["mcp__atlassian__getJiraIssue"])
        self.assertTrue(all(isinstance(k, str) for k in supported))
        self.assertTrue(all(isinstance(k, str) for k in tools))
        C.record_capabilities(self.conn, self.cid, supported, tool_names=tools)
        row = C.list_connections(self.conn)[0]
        self.assertTrue(mcp.JiraMcpAdapter(row).capabilities()["get_issue"])

    def test_ingest_opens_contradictions(self):
        from work_ledger import work_items as wi, remaining as r
        wid = wi.upsert_external(self.conn, self.cid, "AB-76", title="Upload recovery")
        r.create(self.conn, title="Retest on hardware", normalized_key="hardware-retest", work_item_id=wid,
                 confidence="high", source="stage_a", occurred_at="2026-08-29T12:00:00.000Z")
        out = mcp.ingest(self.conn, "acme-jira", {"issues": [ISSUE]})
        self.assertGreaterEqual(out["contradictions_opened"], 1)


class TestMcpHardening(unittest.TestCase):
    """Behaviours the ten-test core above does not pin down: the Ruling 15
    redaction sink, candidate filtering, health, and the `jira` subcommand."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("WORK_LEDGER_HOME")
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name
        self.conn = dbc.connect(); m.apply_all(self.conn)
        self.cid = C.upsert_connection(self.conn, name="acme-jira", adapter="mcp",
                                       mcp_server_name="atlassian", ticket_patterns=["AB-*"])

    def tearDown(self):
        self.conn.close()
        if self._old_home is None:
            os.environ.pop("WORK_LEDGER_HOME", None)
        else:
            os.environ["WORK_LEDGER_HOME"] = self._old_home
        self.tmp.cleanup()

    def test_ingest_redacts_secrets_before_they_reach_the_database(self):
        # Ruling 15: mcp.ingest is a required redaction sink -- stdin is untrusted.
        secret = "ATATT3xFfGF0SUPERSECRETVALUE0000"
        payload = {"issues": [{"id": "1", "key": "AB-99",
                               "fields": {"summary": f"Rotate {secret} now",
                                          "status": {"name": "To Do",
                                                     "statusCategory": {"key": "new"}}}}]}
        out = mcp.ingest(self.conn, "acme-jira", payload)
        self.assertEqual(out["updated"], 1)
        title = self.conn.execute(
            "SELECT title FROM work_items WHERE external_key='AB-99'").fetchone()["title"]
        self.assertNotIn(secret, title)
        self.assertIn("[REDACTED:atlassian_token]", title)

    def test_ingest_reports_keys_this_connection_does_not_claim(self):
        out = mcp.ingest(self.conn, "acme-jira",
                         [{"key": "OTHER-1", "fields": {"summary": "not ours"}}])
        self.assertEqual(out["updated"], 0)
        self.assertEqual(out["unknown_keys"], ["OTHER-1"])
        self.assertIsNone(self.conn.execute(
            "SELECT id FROM work_items WHERE external_key='OTHER-1'").fetchone())

    def test_jira_candidates_matches_by_server_name_or_tool_name(self):
        servers = [
            {"name": "atlassian", "source": "claude.json", "tools": []},
            {"name": "notes", "source": "cli", "tools": ["mcp__notes__jira_get_issue"]},
            {"name": "weather", "source": "cli", "tools": ["mcp__weather__forecast"]},
        ]
        self.assertEqual([s["name"] for s in mcp.jira_candidates(servers)], ["atlassian", "notes"])

    def test_health_is_degraded_until_a_model_mediated_refresh_lands(self):
        row = C.list_connections(self.conn)[0]
        result = mcp.JiraMcpAdapter(row, conn=self.conn).health()
        self.assertEqual(result.state, "degraded")
        self.assertEqual(result.detail, "awaiting model-mediated refresh")
        C.record_capabilities(self.conn, self.cid, {"get_issue": True},
                              tool_names={"get_issue": "mcp__atlassian__getJiraIssue"})
        row = C.list_connections(self.conn)[0]
        self.assertEqual(mcp.JiraMcpAdapter(row, conn=self.conn).health().state, "healthy")

    def test_discover_servers_never_runs_the_cli_when_disabled(self):
        p = Path(self.tmp.name) / "claude.json"
        p.write_text("{ not json at all")
        # A corrupt/absent config is not an error -- discovery degrades to empty.
        self.assertEqual(mcp.discover_servers(claude_json_path=str(p), run_cli=False), [])

    def test_cli_jira_plan_refresh_and_ingest_round_trip(self):
        from work_ledger import cli, work_items as wi
        wi.upsert_external(self.conn, self.cid, "AB-76", title="Upload recovery")
        C.record_capabilities(self.conn, self.cid, {"get_issue": True},
                              tool_names={"get_issue": "mcp__atlassian__getJiraIssue"})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["jira", "plan-refresh", "--format", "json"])
        self.assertEqual(rc, 0)
        plan = json.loads(buf.getvalue())
        self.assertEqual(plan["connections"][0]["issue_keys"], ["AB-76"])

        buf = io.StringIO()
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps({"issues": [ISSUE]}))
        try:
            with contextlib.redirect_stdout(buf):
                rc = cli.main(["jira", "ingest", "--connection", "acme-jira",
                               "--format", "json"])
        finally:
            sys.stdin = old_stdin
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue())["updated"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT jira_stale FROM work_items WHERE external_key='AB-76'").fetchone()[0], 0)

    def test_ticket_patterns_match_case_insensitively(self):
        # A connection configured as ["ab-*"] accumulates AB-76 work items
        # through Stage A / manual (both match case-insensitively); ingest
        # must claim the same key rather than report it unknown.
        C.upsert_connection(self.conn, name="lower", adapter="mcp",
                            mcp_server_name="atlassian", ticket_patterns=["ab-*"])
        out = mcp.ingest(self.conn, "lower", {"issues": [ISSUE]})
        self.assertEqual(out["updated"], 1)
        self.assertEqual(out["unknown_keys"], [])

    def test_a_connection_with_no_ticket_patterns_claims_nothing(self):
        # Same as manual.matching_connection_id / stage_a.ticket_patterns:
        # both iterate the list and never match when it is empty.
        C.upsert_connection(self.conn, name="unscoped", adapter="mcp",
                            mcp_server_name="atlassian")
        out = mcp.ingest(self.conn, "unscoped", {"issues": [ISSUE]})
        self.assertEqual(out["updated"], 0)
        self.assertEqual(out["unknown_keys"], ["AB-76"])

    def test_every_stored_payload_string_is_length_bounded(self):
        long = "x" * 5000
        payload = {"issues": [{"id": long, "key": "AB-77", "fields": {
            "summary": long,
            "status": {"name": long, "statusCategory": {"key": long}},
            "resolution": {"name": long},
            "assignee": {"accountId": long}}}]}
        self.assertEqual(mcp.ingest(self.conn, "acme-jira", payload)["updated"], 1)
        row = self.conn.execute(
            "SELECT title, external_id, jira_status, jira_status_category, jira_resolution, "
            "jira_assignee FROM work_items WHERE external_key='AB-77'").fetchone()
        for column in row.keys():
            self.assertEqual(len(row[column]), 512, column)

    def test_ingest_refuses_a_disabled_connection(self):
        self.conn.execute("UPDATE connections SET enabled=0 WHERE id=?", (self.cid,))
        with self.assertRaises(ValueError):
            mcp.ingest(self.conn, "acme-jira", {"issues": [ISSUE]})

    def test_a_payload_without_updated_never_erases_a_known_timestamp(self):
        mcp.ingest(self.conn, "acme-jira", {"issues": [ISSUE]})
        before = self.conn.execute(
            "SELECT jira_updated_at FROM work_items WHERE external_key='AB-76'").fetchone()[0]
        self.assertTrue(before.endswith("Z"))
        mcp.ingest(self.conn, "acme-jira",
                   [{"key": "AB-76", "fields": {"summary": "Upload recovery",
                                                "updated": "not a timestamp"}}])
        self.assertEqual(self.conn.execute(
            "SELECT jira_updated_at FROM work_items WHERE external_key='AB-76'").fetchone()[0],
            before)

    def test_refresh_limit_falls_back_when_config_is_not_a_count(self):
        from work_ledger import config as config_mod
        cfg = config_mod.load()
        cfg["jira"]["max_refresh_per_turn"] = "twenty"
        config_mod.save(cfg)
        self.assertEqual(mcp.default_refresh_limit(), 20)

