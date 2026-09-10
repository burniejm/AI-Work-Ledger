"""Deterministic extraction of remaining work, blockers, decisions, and
completion markers from a commit's subject and body.

Pure text scanning -- no I/O, no database access, no normalization. The
regexes here mirror the trailer conventions `infer.stage_a` already scans
for (`TODO(KEY): ...`, bare `TODO:`, `FIXME:`, `Remaining:`, `Blocked:`,
`Decision:`), plus `completion_markers`, which looks for a ticket key
following `fixes|closes|resolves` -- the signal `infer.stage_b` (Task 29)
and `contradictions` (Task 30) use to reconcile a commit against Jira's own
"done" state. Never scans file contents: a TODO in source code is not a
statement about this session's work.
"""
import re

__all__ = [
    "remaining_from_commit",
    "blockers_from_commit",
    "decisions_from_commit",
    "completion_markers",
]

_REMAINING_TRAILER_RE = re.compile(r"^[ \t]*Remaining[ \t]*:[ \t]*(.+?)[ \t]*$",
                                    re.MULTILINE | re.IGNORECASE)
_TODO_KEY_RE = re.compile(r"^[ \t]*TODO\(([^)]*)\)[ \t]*:[ \t]*(.+?)[ \t]*$", re.MULTILINE)
_TODO_BARE_RE = re.compile(r"^[ \t]*TODO[ \t]*:[ \t]*(.+?)[ \t]*$", re.MULTILINE)
_FIXME_RE = re.compile(r"^[ \t]*FIXME[ \t]*:[ \t]*(.+?)[ \t]*$", re.MULTILINE)
_BLOCKED_RE = re.compile(r"^[ \t]*Blocked[ \t]*:[ \t]*(.+?)[ \t]*$",
                          re.MULTILINE | re.IGNORECASE)
_DECISION_RE = re.compile(r"^[ \t]*Decision[ \t]*:[ \t]*(.+?)[ \t]*$",
                          re.MULTILINE | re.IGNORECASE)
_COMPLETION_RE = re.compile(
    r"\b(?:fixes|closes|resolves)\b[ \t]*:?[ \t]*([A-Z][A-Z0-9]*-\d+)", re.IGNORECASE)


def _text(subject: str, body: str) -> str:
    parts = [part for part in (subject, body) if part]
    return "\n".join(parts)


def remaining_from_commit(subject: str, body: str) -> list:
    """`TODO(KEY): ...` / bare `TODO:` / `FIXME:` / `Remaining:` lines from
    `subject` and `body`, as `{"title": ..., "detail": None}` dicts."""
    text = _text(subject, body)
    items = []
    for match in _REMAINING_TRAILER_RE.finditer(text):
        items.append({"title": match.group(1).strip(), "detail": None})
    for match in _TODO_KEY_RE.finditer(text):
        items.append({"title": match.group(2).strip(), "detail": None})
    for match in _TODO_BARE_RE.finditer(text):
        items.append({"title": match.group(1).strip(), "detail": None})
    for match in _FIXME_RE.finditer(text):
        items.append({"title": match.group(1).strip(), "detail": None})
    return items


def blockers_from_commit(body: str) -> list:
    """`Blocked:` lines from `body`, as `{"title": ..., "detail": None}` dicts."""
    items = []
    for match in _BLOCKED_RE.finditer(body or ""):
        items.append({"title": match.group(1).strip(), "detail": None})
    return items


def decisions_from_commit(body: str) -> list:
    """`Decision:` lines from `body`, as `{"statement": ..., "rationale": None}` dicts."""
    items = []
    for match in _DECISION_RE.finditer(body or ""):
        items.append({"statement": match.group(1).strip(), "rationale": None})
    return items


def completion_markers(subject: str, body: str) -> list:
    """Ticket keys following `fixes|closes|resolves` in `subject` or `body`,
    upper-cased, in the order they appear."""
    text = _text(subject, body)
    return [match.group(1).upper() for match in _COMPLETION_RE.finditer(text)]
