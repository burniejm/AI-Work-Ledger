import unittest
from work_ledger import textnorm as tn

class TestTextNorm(unittest.TestCase):
    def test_normalization_is_order_and_inflection_insensitive(self):
        # Ruling 3: "retesting" must stem to "retest" for these two to agree.
        self.assertEqual(tn.normalize_key("Retest the upload recovery"),
                         tn.normalize_key("upload recovery retesting"))
        self.assertEqual(tn.normalize_key("Retest the upload recovery"),
                         "recovery-retest-upload")

    def test_retest_family_is_stemmed(self):
        self.assertEqual(tn.STEM_MAP["retesting"], "retest")
        self.assertEqual(tn.STEM_MAP["retested"], "retest")
        self.assertEqual(tn.normalize_key("retested upload"), tn.normalize_key("retest upload"))

    def test_stopwords_dropped(self):
        self.assertEqual(tn.normalize_key("Need to fix the upload"), tn.normalize_key("fixing upload"))
        self.assertEqual(tn.normalize_key("Need to fix the upload"), "fix-upload")

    def test_normalize_title_is_the_same_pipeline(self):
        self.assertIs(tn.normalize_title, tn.normalize_key)
        self.assertEqual(tn.token_set("Diagnose Ubuntu boot image on AMD hardware"),
                         frozenset({"diagnose", "ubuntu", "boot", "image", "amd", "hardware"}))

    def test_key_is_truncated_to_120_chars(self):
        self.assertLessEqual(len(tn.normalize_key(" ".join(f"token{i}" for i in range(60)))), 120)
