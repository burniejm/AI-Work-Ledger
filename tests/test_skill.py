"""Structural tests for skills/worklog/SKILL.md (Task 37).

Everything here is static: the file is parsed and asserted against, never
handed to a model. A live `claude --plugin-dir . -p ...` invocation is a
manual verification step (docs/INSTALL-NOTES.md), not a unit test -- the
suite has no network and makes no model calls (Global Constraints).
"""
import re
import unittest
from pathlib import Path

from work_ledger import cli

SKILL = Path(__file__).resolve().parents[1] / "skills" / "worklog" / "SKILL.md"


class TestSkill(unittest.TestCase):
    def setUp(self):
        self.text = SKILL.read_text()
        self.fm = self.text.split("---")[1]

    def test_frontmatter_fields(self):
        for key in ("name:", "description:", "argument-hint:", "allowed-tools:", "user-invocable:"):
            self.assertIn(key, self.fm)
        self.assertIn("name: worklog", self.fm)

    def test_description_mentions_the_documented_triggers(self):
        for phrase in ("what did I work on yesterday", "what remains on", "what is blocked",
                       "record that I spent", "move yesterday's activity"):
            self.assertIn(phrase, self.fm)

    def test_every_canonical_command_is_documented(self):
        for cmd in ("setup","today","day","range","status","remaining","blocked","stale","evidence",
                    "add","correct","inferences","connections","backup","export","import","doctor"):
            self.assertRegex(self.text, rf"worklog {cmd}\b")

    def test_inline_bash_has_a_failure_guard(self):
        matches = re.findall(r"!`([^`]+)`", self.text)
        self.assertTrue(matches, "no inline bash preamble found")
        for m in matches:
            self.assertTrue(m.strip().endswith("|| true"), f"unguarded inline bash: {m}")

    def test_forbids_sql_and_jira_writes(self):
        low = self.text.lower()
        self.assertIn("never", low)
        for forbidden in ("sqlite3", "sql"):
            self.assertRegex(low, rf"never[^.\n]*{forbidden}")
        self.assertRegex(low, r"never[^.\n]*(transition|comment|worklog|create|update)[^.\n]*jira|"
                              r"never[^.\n]*jira[^.\n]*(write|transition|comment|create|update)")

    def test_documents_the_mcp_refresh_loop(self):
        for token in ("plan-refresh", "jira ingest", "mcp__"):
            self.assertIn(token, self.text)

    def test_allowed_tools_permit_the_readonly_mcp_tools_and_no_write_tools(self):
        # Ruling 11: the refresh loop in body requirement 4 cannot run unless the
        # mcp__* read tools are allowed; allowed-tools has no wildcard over MCP names,
        # so each is listed exactly.
        allowed = next(l for l in self.fm.splitlines() if l.startswith("allowed-tools:"))
        for tool in ("mcp__atlassian__getJiraIssue",
                     "mcp__atlassian__searchJiraIssuesUsingJql",
                     "mcp__atlassian__getVisibleJiraProjects",
                     "mcp__atlassian__atlassianUserInfo",
                     "mcp__atlassian__getAccessibleAtlassianResources",
                     "mcp__atlassian__getJiraIssueRemoteIssueLinks"):
            self.assertIn(tool, allowed)
        for write_tool in ("createJiraIssue", "editJiraIssue", "transitionJiraIssue",
                           "addCommentToJiraIssue", "addWorklogToJiraIssue", "createIssueLink"):
            self.assertNotIn(write_tool, allowed)
        self.assertNotIn("mcp__atlassian__*", allowed)      # wildcards are not supported

    def test_bash_patterns_match_the_documented_invocation_form(self):
        # Ruling 11: the body invokes "${CLAUDE_PLUGIN_ROOT}"/bin/worklog; allowed-tools
        # must carry a matching pattern, not only the bare-name and glob forms.
        allowed = next(l for l in self.fm.splitlines() if l.startswith("allowed-tools:"))
        self.assertIn('Bash("${CLAUDE_PLUGIN_ROOT}"/bin/worklog:*)', allowed)
        self.assertIn("Bash(*/bin/worklog:*)", allowed)
        self.assertIn("Bash(worklog:*)", allowed)
        self.assertIn('"${CLAUDE_PLUGIN_ROOT}"/bin/worklog', self.text)

    def test_documents_the_mcp_server_name_limitation(self):
        low = self.text.lower()
        self.assertIn("server name", low)
        self.assertRegex(low, r"allowed-tools[^.\n]*(wildcard|exact)")

    def test_every_documented_subcommand_exists_in_the_cli_dispatch_table(self):
        """Consistency with the CLI dispatch table: every `worklog <word>` the
        skill tells the model to run must be a registered subcommand, so the
        skill can never document a command the CLI does not have."""
        registered = set(cli.registered_names())
        documented = set(re.findall(r"worklog ([a-z][a-z-]*)\b", self.text))
        # `worklog` also appears as a bare noun ("the worklog skill") and in
        # paths; only tokens that look like subcommands are checked, and the
        # known non-command words are excluded explicitly.
        prose = {"skill", "context", "subcommand", "subcommands", "commands", "command"}
        unknown = sorted(documented - registered - prose)
        self.assertEqual(unknown, [], f"SKILL.md documents non-existent subcommands: {unknown}")
