import json, os, unittest
from pathlib import Path
from work_ledger import transcript as tr

FIX = Path(__file__).parent / "fixtures" / "transcript" / "sample.jsonl"

class TestTranscript(unittest.TestCase):
    def test_encode_cwd(self):
        self.assertEqual(tr.encode_cwd("/Users/devuser/Documents/code/workledger"),
                         "-Users-devuser-Documents-code-workledger")

    def test_bad_lines_are_skipped_not_raised(self):
        recs = list(tr.iter_records(FIX))
        self.assertTrue(all(isinstance(r, dict) for r in recs))
        self.assertGreaterEqual(len(recs), 8)

    def test_unknown_type_is_ignored_without_error(self):
        summaries = [tr.summarize_record(r) for r in tr.iter_records(FIX)]
        self.assertIn(None, summaries)                       # unknown/irrelevant types return None
        self.assertTrue(any(s for s in summaries if s and s["tool_uses"]))

    def test_string_content_is_handled(self):
        texts = [s["text"] for s in (tr.summarize_record(r) for r in tr.iter_records(FIX)) if s and s["text"]]
        self.assertTrue(any("AB-76" in t or t for t in texts))

    def test_tool_use_extraction(self):
        uses = [u for s in (tr.summarize_record(r) for r in tr.iter_records(FIX)) if s for u in s["tool_uses"]]
        self.assertEqual(uses[0]["name"], "Bash")
        self.assertIn("git commit", uses[0]["input"]["command"])

    def test_sidechain_flag_is_preserved(self):
        self.assertTrue(any(s["is_sidechain"] for s in (tr.summarize_record(r) for r in tr.iter_records(FIX)) if s))

    def test_after_uuid_resumes(self):
        recs = [r for r in tr.iter_records(FIX) if r.get("uuid")]
        mid = recs[1]["uuid"]
        after = [r for r in tr.iter_records(FIX, after_uuid=mid) if r.get("uuid")]
        self.assertLess(len(after), len(recs))

    def test_missing_file_returns_empty(self):
        self.assertEqual(list(tr.iter_records(Path("/nonexistent/x.jsonl"))), [])


if __name__ == "__main__":
    unittest.main()
