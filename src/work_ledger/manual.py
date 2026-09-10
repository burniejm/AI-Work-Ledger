"""Manual work capture: `worklog add` (Spec Section 7, Ruling 18, Ruling 15).

Parsing (`parse_add`) is pure: no database access, no I/O beyond reading
`config` for the `when` phrase (America/Chicago default). Recording
(`record`) is the module's redaction sink (Ruling 15: a required choke
point, not an exception to the three-choke-point rule) -- every string this
module persists (the activity summary, and a local work item's title when
one is created) is redacted first.

Ruling 18 governs how a free-text ticket key resolves to a work item:
reuse an existing live row by case-insensitive `external_key` first,
resolve `connection_id` only when exactly one enabled connection's
`ticket_patterns` matches, then create via `work_items.upsert_external`
(whose partial unique index `uq_work_items_orphan_key` makes a connection-
less orphan row unique on `external_key` alone, so repeating the same
ticket mention can never create a second work item).
"""
import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from work_ledger import config, entities, ids, redact, timeutil, work_items
from work_ledger.db import connection

__all__ = [
    "ManualStatement",
    "DURATION_RE",
    "KEYVALUE_RE",
    "parse_add",
    "record",
    "resolve_ticket_work_item",
    "matching_connection_id",
]

# activities.kind CHECK constraint values (004_interpretation.sql). Kept here
# as a literal tuple rather than imported: no module currently owns this list.
_ALLOWED_KINDS = ("code", "test", "investigation", "decision", "review", "manual", "other")

# Leading "key=value" tokens consumed before the description begins.
KEYVALUE_RE = re.compile(
    r"^(?P<key>client|project|ticket|when|duration|kind)=(?P<value>\S+)$",
    re.IGNORECASE,
)

# Explicit duration phrases: "(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours)" -> minutes,
# "(\d+)\s*(m|min|mins|minute|minutes)" -> minutes. "an hour"/"half an hour" are
# handled separately (_HALF_HOUR_RE / _AN_HOUR_RE) since they carry no digits.
DURATION_RE = re.compile(
    r"(?P<hnum>\d+(?:\.\d+)?)\s*(?:hours|hour|hrs|hr|h)\b"
    r"|(?P<mnum>\d+)\s*(?:minutes|minute|mins|min|m)\b",
    re.IGNORECASE,
)
_HALF_HOUR_RE = re.compile(r"\bhalf an hour\b", re.IGNORECASE)
_AN_HOUR_RE = re.compile(r"\ban hour\b", re.IGNORECASE)

# Free-text ticket detection. re.IGNORECASE so "ab-76" and "AB-76" are both
# picked up as a mention of the same ticket -- Ruling 18's reuse lookup is
# case-insensitive, and a statement's own casing must not gate whether the
# ticket is even recognized.
_TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b", re.IGNORECASE)


@dataclass
class ManualStatement:
    client: "str | None"
    project: "str | None"
    ticket: "str | None"
    when: "str | None"          # raw phrase, unparsed
    duration_minutes: "int | None"
    kind: str
    description: str


def _duration_minutes_from_text(text: "str | None") -> "int | None":
    if not text:
        return None
    if _HALF_HOUR_RE.search(text):
        return 30
    if _AN_HOUR_RE.search(text):
        return 60
    m = DURATION_RE.search(text)
    if m is None:
        return None
    if m.group("hnum") is not None:
        return round(float(m.group("hnum")) * 60)
    return int(m.group("mnum"))


def _format_utc(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _normalize_when_text(raw: str) -> str:
    """Underscores are treated as spaces so the single whitespace-free token
    that `key=value` parsing requires can still carry a multi-word phrase,
    e.g. `when=last_night` -> "last night", `when=on_aug_29` -> "on aug 29".
    Shared by `_resolve_when` (to recognize the phrase) and `parse_add` (to
    render it back into the description when it turns out unparseable)."""
    return raw.strip().replace("_", " ")


def _resolve_when(raw: str, *, now: "datetime | None" = None) -> "str | None":
    """Resolve a raw `when` phrase to a UTC ISO timestamp, or None when it
    cannot be parsed (the caller then leaves occurred_at at now and appends
    the raw phrase to the description rather than guessing).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    text = _normalize_when_text(raw)
    low = text.lower()

    tz = config.get("timezone", "America/Chicago")
    rollover_hour = config.get("rollover_hour", 3)
    nominal_start_hour = config.get("nominal_start_hour", 8)
    zone = ZoneInfo(tz)
    local_now = now.astimezone(zone)

    if low == "last night":
        target = (local_now - timedelta(days=1)).replace(
            hour=21, minute=0, second=0, microsecond=0)
        return _format_utc(target)
    if low == "this morning":
        target = local_now.replace(hour=9, minute=0, second=0, microsecond=0)
        return _format_utc(target)

    day_text = text[3:].strip() if low.startswith("on ") else text
    try:
        workday = timeutil.parse_day_expr(day_text, tz, rollover_hour, now=now)
    except ValueError:
        return None

    year, month, day = (int(p) for p in workday.split("-"))
    target = datetime(year, month, day, nominal_start_hour, 0, 0, tzinfo=zone)
    return _format_utc(target)


def parse_add(text: str) -> ManualStatement:
    """Parse free-form `worklog add` text into a `ManualStatement`.

    Leading `key=value` tokens are consumed for client/project/ticket/when/
    duration/kind; the first token that doesn't match stops consumption and
    everything from there on (or the whole string, if nothing was consumed)
    is the description.
    """
    fields = {"client": None, "project": None, "ticket": None,
              "when": None, "duration": None, "kind": None}

    pos = 0
    for token_match in re.finditer(r"\S+", text):
        kv = KEYVALUE_RE.match(token_match.group(0))
        if kv is None:
            break
        fields[kv.group("key").lower()] = kv.group("value")
        pos = token_match.end()
    remainder = text[pos:].lstrip(" \t\r\n")

    ticket = fields["ticket"]
    if ticket is None:
        ticket_match = _TICKET_RE.search(remainder)
        if ticket_match is not None:
            ticket = ticket_match.group(0)

    if fields["duration"] is not None:
        duration_minutes = _duration_minutes_from_text(fields["duration"])
    else:
        duration_minutes = _duration_minutes_from_text(remainder)

    kind = fields["kind"]
    if kind is None:
        kind = "manual"
    else:
        kind = kind.lower()
        if kind not in _ALLOWED_KINDS:
            raise ValueError(f"invalid kind: {fields['kind']!r}")

    when_raw = fields["when"]
    description = remainder
    if when_raw is not None and _resolve_when(when_raw) is None:
        # Unparseable: don't guess -- keep the raw phrase visible in the
        # description, space-restored (not the underscore-joined key=value
        # token) since that's what a person actually typed.
        when_display = _normalize_when_text(when_raw)
        description = f"{remainder} ({when_display})" if remainder else f"({when_display})"

    return ManualStatement(
        client=fields["client"],
        project=fields["project"],
        ticket=ticket,
        when=when_raw,
        duration_minutes=duration_minutes,
        kind=kind,
        description=description,
    )


def matching_connection_id(conn, ticket_key: str) -> "str | None":
    """Return a connection id only when exactly one live, enabled
    connection's `ticket_patterns` matches `ticket_key`. None for zero
    matches and None for ambiguity (two or more matches)."""
    rows = conn.execute(
        "SELECT id, ticket_patterns_json FROM connections "
        "WHERE enabled=1 AND deleted_at IS NULL"
    ).fetchall()

    matches = []
    key_upper = ticket_key.upper()
    for row in rows:
        patterns = json.loads(row["ticket_patterns_json"] or "[]")
        for pattern in patterns:
            if fnmatch.fnmatchcase(key_upper, pattern.upper()):
                matches.append(row["id"])
                break

    if len(matches) == 1:
        return matches[0]
    return None


def resolve_ticket_work_item(conn, ticket_key: str, *, client_id=None,
                              project_id=None) -> "tuple[str, bool]":
    """Ruling 18: reuse-or-create a work item for a free-text ticket key.

    Returns (work_item_id, created).
    """
    rows = conn.execute(
        "SELECT id, connection_id FROM work_items WHERE upper(external_key) = upper(?) "
        "AND deleted_at IS NULL ORDER BY updated_at DESC",
        (ticket_key,),
    ).fetchall()

    if rows:
        if len(rows) == 1:
            return rows[0]["id"], False
        preferred_connection_id = matching_connection_id(conn, ticket_key)
        chosen = None
        if preferred_connection_id is not None:
            chosen = next(
                (r for r in rows if r["connection_id"] == preferred_connection_id), None)
        if chosen is None:
            # rows is already ordered by updated_at DESC, so the first
            # non-null connection_id here is also the most recently updated.
            chosen = next((r for r in rows if r["connection_id"] is not None), None)
        if chosen is None:
            chosen = rows[0]
        return chosen["id"], False

    connection_id = matching_connection_id(conn, ticket_key)
    work_item_id = work_items.upsert_external(
        conn, connection_id, ticket_key, title=ticket_key,
        client_id=client_id, project_id=project_id,
    )
    return work_item_id, True


def _resolve_client_id(conn, name: "str | None") -> "str | None":
    if name is None:
        return None
    existing = entities.get_client_by_name(conn, name)
    if existing is not None:
        return existing["id"]
    return entities.upsert_client(conn, name)


def record(conn, statement: ManualStatement, *, occurred_at: "str | None" = None) -> str:
    """Record a manual statement as a new activity. Always confidence='high',
    source='manual', in_inbox=0. Returns the new activities.id.

    Manual statements are user intent, not replayed evidence: repeating the
    same statement creates a second activity (no idempotency dedupe across
    statements) -- `idempotency_key` is unique-per-call by construction
    (sha256("manual|" + the activity's own uuid7)).
    """
    device_id = entities.current_device(conn)

    # Ruling 15: manual.record is a required redaction sink -- every string
    # persisted below (the activity summary, and a fresh local item's title)
    # is redacted, not just the designated three choke points.
    redacted_summary, _fired = redact.redact(statement.description)

    client_id = _resolve_client_id(conn, statement.client)
    project_id = None
    if statement.project and client_id is not None:
        project_id = entities.upsert_project(conn, client_id, statement.project)

    if statement.ticket:
        work_item_id, _created = resolve_ticket_work_item(
            conn, statement.ticket, client_id=client_id, project_id=project_id)
    else:
        work_item_id, _created = work_items.get_or_create_local(
            conn, redacted_summary, client_id=client_id, project_id=project_id)

    resolved_at = occurred_at
    if resolved_at is None:
        resolved_at = timeutil.utc_now_iso()
        if statement.when:
            when_resolved = _resolve_when(statement.when)
            if when_resolved is not None:
                resolved_at = when_resolved

    activity_id = ids.new_id()
    idempotency_key = hashlib.sha256(f"manual|{activity_id}".encode()).hexdigest()
    source_hash = hashlib.sha256(redacted_summary.encode()).hexdigest()
    now = timeutil.utc_now_iso()
    with connection.transaction(conn):
        conn.execute(
            "INSERT INTO activities(id, client_id, project_id, work_item_id, kind, summary, "
            "started_at, stated_duration_minutes, confidence, source, in_inbox, "
            "idempotency_key, source_hash, origin_device_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (activity_id, client_id, project_id, work_item_id, statement.kind, redacted_summary,
             resolved_at, statement.duration_minutes, "high", "manual", 0,
             idempotency_key, source_hash, device_id, now, now),
        )
    return activity_id
