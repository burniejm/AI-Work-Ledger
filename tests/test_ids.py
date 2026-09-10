import unittest
from work_ledger import ids

class TestIds(unittest.TestCase):
    def test_version_and_variant(self):
        u = ids.uuid7()
        self.assertEqual(u[14], "7")                       # version nibble
        self.assertIn(u[19], "89ab")                       # RFC 4122 variant
        self.assertEqual(len(u), 36)

    def test_monotonic_by_string_sort(self):
        a = ids.uuid7(when_ms=1_700_000_000_000)
        b = ids.uuid7(when_ms=1_700_000_001_000)
        self.assertLess(a, b)

    def test_timestamp_round_trip(self):
        self.assertEqual(ids.timestamp_ms_of(ids.uuid7(when_ms=1_700_000_000_123)), 1_700_000_000_123)

    def test_unique(self):
        self.assertEqual(len({ids.uuid7() for _ in range(10_000)}), 10_000)
