"""Shared helper for turning activity summaries into a display list.

Every report that renders a work item's/day's activities as a terse
human-readable line (`query.week`, `query.timesheet`) needs the same
"drop blanks, keep first-seen order, no duplicates" rule; it lives here
once so the three call sites can't drift apart.
"""

__all__ = ["dedupe"]


def dedupe(summaries) -> list:
    """Keep first-seen order, drop falsy/duplicate entries."""
    result: list = []
    for summary in summaries:
        if summary and summary not in result:
            result.append(summary)
    return result
