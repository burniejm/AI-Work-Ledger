import unittest
from work_ledger.infer import confidence as c


class TestConfidence(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual(c.bucket(70), "high")
        self.assertEqual(c.bucket(69), "medium")
        self.assertEqual(c.bucket(40), "medium")
        self.assertEqual(c.bucket(39), "low")

    def test_strong_deterministic_evidence_is_high(self):
        b, s, reason = c.classify({"ticket_in_branch": 1, "repo_mapping_persisted": 1})
        self.assertEqual(b, "high")
        self.assertEqual(s, 75)
        self.assertIn("branch", reason)

    def test_weak_evidence_is_low_and_goes_to_the_inbox(self):
        b, s, _ = c.classify({"ticket_in_tool_io": 1, "repo_name_similarity": 1})
        self.assertEqual(b, "low")
        self.assertEqual(c.in_inbox(b), 1)

    def test_manual_always_wins(self):
        b, s, _ = c.classify({}, manual=True)
        self.assertEqual((b, s), ("high", 100))

    def test_competing_candidates_are_contradictory(self):
        b, _, reason = c.classify({"ticket_in_branch": 1}, competing=[("AB-76", 35), ("VV-12", 30)])
        self.assertEqual(b, "contradictory")
        self.assertIn("AB-76", reason); self.assertIn("VV-12", reason)

    def test_far_apart_candidates_are_not_contradictory(self):
        b, _, _ = c.classify({"ticket_in_branch": 1, "repo_mapping_persisted": 1},
                             competing=[("AB-76", 75), ("VV-12", 8)])
        self.assertEqual(b, "high")

    def test_stage_b_only_is_capped_at_medium(self):
        b, _, reason = c.classify({"manual_statement": 0, "repo_mapping_persisted": 1, "ticket_in_branch": 1},
                                  stage_b_only=True)
        self.assertEqual(b, "medium")
        self.assertIn("capped", reason)

    def test_stage_b_failure_leaves_attribution_confidence_unchanged(self):
        # Ruling 6: the "never exceeds medium" cap applies to Stage B narrative output
        # and to Stage-A-only summaries, NOT to attribution confidence. A direct signal
        # (ticket key in the branch) plus a persisted repo mapping is 35 + 40 = 75 -> high,
        # with or without Stage B.
        with_b = c.classify({"ticket_in_branch": 1, "repo_mapping_persisted": 1}, stage_b_ran=True)
        without_b = c.classify({"ticket_in_branch": 1, "repo_mapping_persisted": 1}, stage_b_ran=False)
        self.assertEqual(with_b[:2], ("high", 75))
        self.assertEqual(without_b[:2], ("high", 75))          # unchanged
        self.assertIn("Stage B did not run", without_b[2])
        self.assertIn("ticket_in_branch", c.DIRECT_SIGNALS)

    def test_stage_b_failure_caps_the_narrative_not_the_attribution(self):
        # Ruling 6: what Stage B's absence caps is the prose and the Stage-B-derived fields.
        policy = c.narrative_policy(stage_b_ran=False)
        self.assertEqual(policy["summary"], "templated")
        self.assertFalse(policy["allow_stage_b_fields"])
        self.assertTrue(c.narrative_policy(stage_b_ran=True)["allow_stage_b_fields"])

    def test_score_is_capped_at_100(self):
        self.assertEqual(c.score({"manual_statement": 1, "ticket_in_branch": 1}), 100)
