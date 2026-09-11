"""Time-on-ticket estimates, computed at query time (no stored state).

An activity's `started_at`/`ended_at` bounds come from real evidence
timestamps (Stage A/B set them from the session's events), so summing them
per work item -- with overlaps merged so two activities in the same hour
never double-count -- gives a defensible "session-active minutes" figure
for any window.

Honesty contract (README "Time on ticket"): this measures evidence-bounded
*session* time, not wall-clock effort. Human-only coding outside Claude
sessions is invisible here, the same denominator caveat the AI-usage score
carries. Estimates are recomputed from the ledger on every query and are
never stored, so there is nothing to migrate, pin, or correct beyond the
activity bounds themselves (already correctable via `worklog correct`).
"""
from datetime import timedelta

from work_ledger import config, timeutil

# An activity with no `ended_at` (single-event evidence) still cost real
# time; it counts as this many minutes rather than zero.
DEFAULT_MIN_ACTIVITY_MINUTES = 5


def _floor_minutes() -> int:
    value = config.get("time.min_activity_minutes", DEFAULT_MIN_ACTIVITY_MINUTES)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return DEFAULT_MIN_ACTIVITY_MINUTES
    return value


def merge_intervals(intervals):
    """Merge overlapping/touching `(start_iso, end_iso)` pairs; drops any
    pair whose end does not follow its start."""
    parsed = []
    for start, end in intervals:
        try:
            s = start if not isinstance(start, str) else timeutil.parse_iso(start)
            e = end if not isinstance(end, str) else timeutil.parse_iso(end)
        except (TypeError, ValueError):
            continue
        if e > s:
            parsed.append((s, e))
    parsed.sort()
    merged = []
    for s, e in parsed:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def total_minutes(merged) -> int:
    return int(sum((e - s).total_seconds() for s, e in merged) // 60)


def minutes_for_work_item(conn, work_item_id: str, *, start_utc: "str | None" = None,
                          end_utc: "str | None" = None) -> int:
    """Merged active minutes across the item's activities, clipped to
    [`start_utc`, `end_utc`) when given."""
    return total_minutes(merge_intervals(
        intervals_for_work_item(conn, work_item_id, start_utc=start_utc, end_utc=end_utc)))


def intervals_for_work_item(conn, work_item_id: str, *, start_utc: "str | None" = None,
                            end_utc: "str | None" = None):
    """The item's clipped activity intervals as `(datetime, datetime)` pairs,
    unmerged -- callers merge (`merge_intervals`) per item, or pool several
    items' intervals first so a client/day total never double-counts a
    stretch two tickets shared."""
    floor = timedelta(minutes=_floor_minutes())
    rows = conn.execute(
        "SELECT started_at, ended_at FROM activities"
        " WHERE work_item_id=? AND deleted_at IS NULL AND started_at IS NOT NULL",
        (work_item_id,)).fetchall()

    window_start = timeutil.parse_iso(start_utc) if start_utc else None
    window_end = timeutil.parse_iso(end_utc) if end_utc else None

    intervals = []
    for row in rows:
        try:
            s = timeutil.parse_iso(row["started_at"])
        except (TypeError, ValueError):
            continue
        e = None
        if row["ended_at"]:
            try:
                e = timeutil.parse_iso(row["ended_at"])
            except (TypeError, ValueError):
                e = None
        if e is None or e <= s:
            e = s + floor            # point-in-time or malformed: the floor
        if window_start is not None:
            s = max(s, window_start)
        if window_end is not None:
            e = min(e, window_end)
        if e > s:
            intervals.append((s, e))
    return intervals


def format_minutes(minutes: int) -> str:
    """`145 -> "2h 25m"`, `120 -> "2h"`, `45 -> "45m"`, `0 -> "0m"`."""
    hours, rest = divmod(max(int(minutes), 0), 60)
    if hours and rest:
        return f"{hours}h {rest}m"
    if hours:
        return f"{hours}h"
    return f"{rest}m"
