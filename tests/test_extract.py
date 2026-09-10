import unittest
from work_ledger.infer import extract as x

BODY = ("Refactored the retry loop.\n\n"
        "TODO(AB-76): retest on replay hardware\n"
        "Blocked: waiting for the replacement AMD box\n"
        "Decision: keep the exponential backoff at 5 attempts\n"
        "Fixes AB-77\n")

class TestExtract(unittest.TestCase):
    def test_remaining(self):
        got = x.remaining_from_commit("AB-76 fix upload retry", BODY)
        self.assertEqual(got[0]["title"], "retest on replay hardware")

    def test_blockers(self):
        self.assertEqual(x.blockers_from_commit(BODY)[0]["title"], "waiting for the replacement AMD box")

    def test_decisions(self):
        self.assertIn("exponential backoff", x.decisions_from_commit(BODY)[0]["statement"])

    def test_completion_markers(self):
        self.assertEqual(x.completion_markers("AB-76 fix upload retry", BODY), ["AB-77"])

    def test_nothing_extracted_from_plain_commits(self):
        self.assertEqual(x.remaining_from_commit("tidy up", "just a small cleanup"), [])
