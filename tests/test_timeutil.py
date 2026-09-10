import unittest
from work_ledger import timeutil as t

TZ, ROLL = "America/Chicago", 3

class TestWorkday(unittest.TestCase):
    def test_morning_is_same_day(self):
        # 2026-08-30 08:00 CDT == 13:00Z
        self.assertEqual(t.workday_of("2026-08-30T13:00:00.000Z", TZ, ROLL), "2026-08-30")

    def test_after_midnight_before_rollover_is_previous_day(self):
        # 2026-08-31 02:30 CDT == 07:30Z
        self.assertEqual(t.workday_of("2026-08-31T07:30:00.000Z", TZ, ROLL), "2026-08-30")

    def test_at_rollover_is_new_day(self):
        # 2026-08-31 03:00 CDT == 08:00Z
        self.assertEqual(t.workday_of("2026-08-31T08:00:00.000Z", TZ, ROLL), "2026-08-31")

    def test_bounds_cover_exactly_the_workday(self):
        start, end = t.workday_bounds("2026-08-30", TZ, ROLL)
        self.assertEqual(t.workday_of(start, TZ, ROLL), "2026-08-30")
        for ts in ("2026-08-30T13:00:00.000Z", "2026-08-31T07:59:59.000Z"):
            self.assertTrue(start <= ts < end)
        self.assertFalse(start <= "2026-08-31T08:00:00.000Z" < end)

    def test_dst_spring_forward_day_has_no_gap(self):
        # 2026-03-08 is the US DST transition; the workday must still be 24h of wall time
        start, end = t.workday_bounds("2026-03-08", TZ, ROLL)
        self.assertTrue(start < end)
        self.assertEqual(t.workday_of("2026-03-08T14:00:00.000Z", TZ, ROLL), "2026-03-08")

    def test_parse_day_expr(self):
        self.assertEqual(t.parse_day_expr("2026-08-29", TZ, ROLL), "2026-08-29")
        with self.assertRaises(ValueError):
            t.parse_day_expr("sometime last spring", TZ, ROLL)
