import unittest
from work_ledger.jira import adapter as a

CLOUD_ISSUE = {
  "id": "10042", "key": "AB-76", "self": "https://x.atlassian.net/rest/api/3/issue/10042",
  "fields": {"summary": "Upload recovery", "updated": "2026-08-29T18:00:00.000+0000",
             "status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
             "resolution": None, "assignee": {"accountId": "acct-1", "displayName": "Jim"},
             "project": {"key": "SK"}}}

class TestAdapter(unittest.TestCase):
    def test_capability_values_match_the_spec(self):
        self.assertEqual({c.value for c in a.Capability},
            {"identify_current_user","list_sites","list_projects","get_issue","search_issues",
             "get_issue_history","get_user_activity"})
        self.assertEqual(a.CAPABILITY_VALUES, {c.value for c in a.Capability})

    def test_capability_dicts_are_keyed_by_plain_strings(self):
        # Ruling 27: Capability(str, Enum) hashes by member, so a member-keyed dict is
        # unreachable by "get_issue". Every boundary uses the .value string.
        self.assertNotEqual(hash(a.Capability.GET_ISSUE), hash("get_issue"))
        self.assertEqual(a.Capability.GET_ISSUE.value, "get_issue")
        import inspect, typing
        sig = inspect.signature(a.JiraAdapter.capabilities)
        self.assertIn("dict[str, bool]", str(sig.return_annotation).replace("'", ""))

    def test_issue_normalization(self):
        r = a.IssueResult.from_cloud_payload(CLOUD_ISSUE)
        self.assertEqual((r.key, r.status, r.status_category, r.assignee_account_id, r.project_key),
                         ("AB-76", "In Progress", "indeterminate", "acct-1", "SK"))

    def test_issue_normalization_tolerates_missing_fields(self):
        r = a.IssueResult.from_cloud_payload({"key": "AB-1", "fields": {}})
        self.assertEqual(r.key, "AB-1")
        self.assertIsNone(r.status); self.assertIsNone(r.assignee_account_id)

    def test_adapter_abc_exposes_no_write_methods(self):
        forbidden = {"create_issue","update_issue","transition_issue","add_comment","add_worklog","delete_issue","post","put","delete"}
        self.assertEqual(forbidden & set(dir(a.JiraAdapter)), set())
