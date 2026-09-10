import unittest
from work_ledger import yamlite

SAMPLE = """\
# Work Ledger config
timezone: America/Chicago
rollover_hour: 3
stage_b:
  enabled: true
  model: sonnet
  timeout_seconds: 120
  note: null
connections:
  - name: acme-jira
    adapter: mcp
    client_hint: Acme
    ticket_patterns:
      - "AB-*"
      - "SC-*"
"""

class TestYamlite(unittest.TestCase):
    def test_loads(self):
        d = yamlite.loads(SAMPLE)
        self.assertEqual(d["timezone"], "America/Chicago")
        self.assertEqual(d["rollover_hour"], 3)
        self.assertIs(d["stage_b"]["enabled"], True)
        self.assertIsNone(d["stage_b"]["note"])
        self.assertEqual(d["connections"][0]["ticket_patterns"], ["AB-*", "SC-*"])

    def test_round_trip_is_stable(self):
        d = yamlite.loads(SAMPLE)
        self.assertEqual(yamlite.loads(yamlite.dumps(d)), d)

    def test_quotes_ambiguous_strings(self):
        out = yamlite.dumps({"a": "3", "b": "yes", "c": "has: colon", "d": "# hash"})
        reparsed = yamlite.loads(out)
        self.assertEqual(reparsed, {"a": "3", "b": "yes", "c": "has: colon", "d": "# hash"})

    def test_pyyaml_agrees_when_available(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        d = {"x": {"y": [1, "two", True, None]}, "s": "with # hash"}
        self.assertEqual(yaml.safe_load(yamlite.dumps(d)), d)

    def test_rejects_unsupported(self):
        with self.assertRaises(yamlite.YamlLiteError):
            yamlite.loads("a: &anchor 1\nb: *anchor\n")

    def test_apostrophes_and_embedded_quotes_round_trip(self):
        # CRITICAL 1: a lone quote character inside an unquoted plain
        # scalar must not be mistaken for the start of a quoted string.
        cases = {
            "note": "don't stop",
            "quip": "it's fine",
            "trailer": 'ends in a quote"',
            "balanced": 'she said "hi" once',
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                d = {key: value}
                out = yamlite.dumps(d)
                self.assertEqual(yamlite.loads(out), d)
        # dumps' own output must always be loadable, regardless of quoting.
        self.assertEqual(
            yamlite.loads(yamlite.dumps({"note": "don't stop"})),
            {"note": "don't stop"},
        )

    def test_rejects_block_scalars(self):
        # CRITICAL 2: | and > (with optional chomping/indentation
        # indicators) must raise, not silently return the indicator as a
        # one-character string while discarding the block body.
        for bad in [
            "key: |\n  line one\n  line two\n",
            "key: >\n  folded text\n",
            "key: |-\n  trimmed\n",
            "key: |+\n  kept\n",
            "key: >-\n  folded\n",
        ]:
            with self.subTest(bad=bad):
                with self.assertRaises(yamlite.YamlLiteError):
                    yamlite.loads(bad)

    def test_dumps_quotes_flow_collection_lookalikes(self):
        # CRITICAL 3: a plain string that happens to start with '[' or '{'
        # must be quoted by dumps, not crash it.
        d = {"a": "[not a list]", "b": "{x}", "c": "[DRAFT] x"}
        out = yamlite.dumps(d)
        self.assertEqual(yamlite.loads(out), d)

    def test_quoted_key_with_colon_and_escaped_quote(self):
        # Minor fix: _split_key_value must skip over a quoted span's
        # backslash-escaped quote rather than treating it as the close,
        # even when the quoted key itself contains a literal ':'.
        d = {'key: with "escaped" quote': "plain value"}
        out = yamlite.dumps(d)
        self.assertEqual(yamlite.loads(out), d)
