"""Stage B: the background LLM summarizer (Task 29; SPEC §6/§8, DESIGN §9.2).

Stage B is the *optional* half of interpretation. Stage A has already written
everything that can be counted; Stage B asks a local `claude` process to read
a bounded, redacted bundle of the session's `source_events` and return one
strictly-shaped JSON object describing the activities it sees. Every part of
this module is written so the answer "Stage B did not happen" is a completely
ordinary outcome:

* **It never blocks a hook.** The Stop hook calls `spawn(session_id)` as its
  very last statement; `spawn` detaches a `worklog stage-b` process and
  returns (Ruling 8). Nothing polls it, nothing reads its result.
* **It never raises at its caller.** `run` catches everything, writes a
  `capture_checkpoints(hook_event='stage_b', …)` row, and returns
  `{"ran": False, "reason": …, "status": …}`. Stage A's templated summary and
  its attribution confidence stand untouched (DESIGN §6 override 3, Ruling 6).
  The status distinguishes the two shapes of non-run: a **guard** declining
  (nested, no `claude`, disabled, rate limited, empty bundle) is `'skipped'`,
  because those are the designed-for outcomes and the rate limiter alone
  produces one on most Stop hooks; only real breakage (timeout, non-zero exit,
  unparseable output, schema violation, a merge that raised) is `'failed'`.
  `doctor` warns on the second and not the first.
* **It never exceeds `medium` confidence on its own** (Global Constraints;
  DESIGN §6 override 2) and it never rewrites a Stage A activity row.
* **It is a mandatory redaction choke point.** `build_bundle` runs
  `redact.redact_obj` over the whole bundle unconditionally, after the
  payloads were already redacted on ingest. Two passes is the point: the
  bundle is the one artifact that leaves this process for a model.

Recursion guard (DESIGN gap G8)
-------------------------------
`WORK_LEDGER_NESTED=1` is THE recursion guard. `invoke_claude` sets it in the
`claude` child's environment, every hook shim short-circuits on it, so the
model session Stage B starts cannot capture itself into an endless loop. The
isolation flags (`--setting-sources ""`, `--plugin-dir`) were verified during
implementation against Claude Code 2.1.252 and are NOT adopted: on that
version `--setting-sources ""` suppresses hooks that come from user/project/
local settings but leaves a *plugin's* hooks firing, which is exactly the case
work-ledger ships as. The measurements are written up in
`docs/INSTALL-NOTES.md`. The sentinel alone is the guard.

`spawn` also sets `WORK_LEDGER_NESTED=1` in the `worklog stage-b` child, as
the task brief specifies -- but that child *is* the Stage B runner, and
`should_run`'s first guard rejects a nested environment, so the two
requirements would cancel each other out and leave the Stop-hook path dead.
The resolution: `spawn` additionally sets `WORK_LEDGER_STAGE_B_SPAWNED=1`,
and the `worklog stage-b` CLI entry point consumes the sentinel *only* when
that companion marker is present (see `cli._cmd_stage_b`). A human running
`WORK_LEDGER_NESTED=1 worklog stage-b` by hand, or a hook that somehow
reaches this module inside a Stage-B-started session, still gets refused.
`invoke_claude` re-sets the sentinel for the model child regardless, so the
guard that actually matters is never weakened.

Controller ruling (carried from Task 28)
----------------------------------------
`infer.dedupe.resolve` is the single write path for a remaining item
discovered by inference, and Task 28 landed it with no production caller.
This module is that caller, on both paths:

* Stage B ran -> `merge` routes the model's `remaining`/`blockers` through
  `dedupe.resolve` with `source='stage_b'`.
* Stage B produced nothing usable (disabled, `claude` missing, rate-limited,
  timed out, garbage, schema violation) -> `run` routes *Stage A's* extracted
  `ActivityDraft.remaining` through `dedupe.resolve` with `source='stage_a'`,
  so the Stage-A-only mode still persists remaining work.

Both passes are idempotent. `dedupe.resolve` reinforces rather than
duplicates, and the Stage-A-only pass is additionally gated on an
append-only `capture_checkpoints` row keyed by a hash of the extracted
titles, so re-running over unchanged evidence is a true no-op rather than a
mention-count inflator.

Transaction discipline
----------------------
`db.connection.transaction` is not reentrant. `entities.current_device`,
`dedupe.resolve`, `remaining.create`/`reinforce`/`transition` and
`checkpoints.checkpoint` each open their own transaction, so every one of
them is called *outside* the transaction this module opens for its own
`activities`/`decisions` writes.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from work_ledger import config, entities, ids, paths, redact, timeutil
from work_ledger.capture import checkpoints
from work_ledger.db import connection
from work_ledger.infer import confidence, dedupe

__all__ = [
    "SCHEMA_VERSION",
    "StageBSchemaError",
    "build_bundle",
    "build_prompt",
    "validate_output",
    "enabled",
    "should_run",
    "invoke_claude",
    "merge",
    "run",
    "spawn",
]

SCHEMA_VERSION = 1

#: Env sentinel that every hook shim short-circuits on. THE recursion guard.
NESTED_ENV = "WORK_LEDGER_NESTED"
#: Companion marker `spawn` sets so the CLI can tell "I am the Stage B runner
#: that `spawn` started" from "a hook is running inside a Stage B session".
SPAWNED_ENV = "WORK_LEDGER_STAGE_B_SPAWNED"

# DESIGN §9.2 output schema. `manual` is deliberately absent from the kinds a
# model may claim -- `manual` means the user said it (SPEC §7).
KINDS = ("code", "test", "investigation", "decision", "review", "other")
CONFIDENCES = ("high", "medium", "low")
REMAINING_STATES = ("open", "blocked", "waiting")

# String caps. `summary <= 400` is verbatim from DESIGN §9.2; the rest bound
# the free-text fields at the same 512-char ceiling the evidence tables use.
_MAX_SUMMARY = 400
_MAX_OUTCOME = 400
_MAX_HINT = 200
_MAX_TITLE = 512
_MAX_DETAIL = 1024
_MAX_ACTIVITIES = 20
_MAX_LIST_ITEMS = 20

# Events worth keeping when the bundle has to shrink: the ones a summary can
# actually be built from. Everything else is "low value" and is dropped
# oldest-first (DESIGN §9.2).
HIGH_VALUE_EVENT_TYPES = frozenset({
    "commit", "git_commit_signal", "test_run", "files_touched", "manual", "prompt",
})

# A remaining item's `state` from the model maps onto the `kind` column
# `dedupe.resolve` understands (`remaining_items.kind` is
# remaining|blocker|waiting).
_STATE_TO_KIND = {"open": "remaining", "blocked": "blocker", "waiting": "waiting"}

# The model's own confidence word, scored so `confidence.bucket` can bucket it
# with the same thresholds every other conclusion uses. `classify` is not the
# call site here: it sums *counted* attribution signals (DESIGN §6) and Stage B
# contributes no counted signal of its own -- what it contributes is a claim,
# which is what these scores represent. The medium cap below is DESIGN §6
# override 2, worded to match `classify`'s own text.
_MODEL_CONFIDENCE_SCORE = {"high": 70, "medium": 50, "low": 20}


class StageBSchemaError(Exception):
    """The model's output was not a valid DESIGN §9.2 document."""


# --------------------------------------------------------------------------
# Bundle
# --------------------------------------------------------------------------

def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def _entry_bytes(entry) -> int:
    """One entry's serialized size, with json's *default* separators (wider
    than the canonical ones used for hashing) so the `max_bytes` promise holds
    however the bundle is later serialized."""
    return len(json.dumps(entry, ensure_ascii=True).encode("utf-8"))


def _measured_bytes(bundle) -> int:
    """The size a caller sees from a plain `json.dumps(bundle)`."""
    return len(json.dumps(bundle, ensure_ascii=True).encode("utf-8"))


def _list_bytes(sizes_total: int, count: int) -> int:
    """`json.dumps` of a list is `"["` + entries joined by `", "` + `"]"`, so
    the whole is 2 framing bytes plus the entries plus 2 bytes per separator.
    Exact, which is what lets the shrink loop subtract instead of re-encoding
    the entire bundle on every drop (the difference between O(n) and O(n^2)
    over a long session)."""
    if count <= 0:
        return 2
    return 2 + sizes_total + 2 * (count - 1)


def build_bundle(conn, session_id: str, *, max_bytes: int) -> "tuple[list[dict], str]":
    """`(events, bundle_hash)` for one session, redacted and size-bounded.

    The payloads were already redacted by `capture.events.append_source_event`
    on the way in; they are redacted again here because this is the required
    Stage B choke point (Global Constraints) -- the bundle is the only
    artifact in the system that is handed to a model.

    Ordered oldest-first. When the bundle does not fit `max_bytes`, the oldest
    *low-value* event is dropped first (anything outside
    `HIGH_VALUE_EVENT_TYPES`), then the oldest event of any kind, until it
    fits. `bundle_hash` is `sha256` of the canonical JSON of what survived, so
    a re-run over unchanged evidence produces the same idempotency keys.
    """
    rows = conn.execute(
        "SELECT id, event_type, tool_name, git_branch, cwd, occurred_at, payload_json "
        "FROM source_events WHERE session_id=? ORDER BY occurred_at, id",
        (session_id,),
    ).fetchall()

    bundle = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            payload = {}
        entry = {
            "id": row["id"],
            "event_type": row["event_type"],
            "tool_name": row["tool_name"],
            "git_branch": row["git_branch"],
            "cwd": row["cwd"],
            "occurred_at": row["occurred_at"],
            "payload": payload,
        }
        bundle.append(entry)

    # Mandatory choke point -- unconditional, not an optimization.
    bundle, _fired = redact.redact_obj(bundle)

    # Shrink to fit in one linear pass. The drop ORDER is fixed up front --
    # every low-value event oldest-first, then every remaining event
    # oldest-first -- and sizes are subtracted incrementally, so this is O(n)
    # rather than a re-encode of the whole bundle per dropped event.
    max_bytes = int(max_bytes)
    sizes = [_entry_bytes(entry) for entry in bundle]
    total = sum(sizes)
    count = len(bundle)

    if _list_bytes(total, count) > max_bytes:
        low_value = [i for i, e in enumerate(bundle)
                     if e["event_type"] not in HIGH_VALUE_EVENT_TYPES]
        high_value = [i for i, e in enumerate(bundle)
                      if e["event_type"] in HIGH_VALUE_EVENT_TYPES]
        dropped = set()
        for index in low_value + high_value:
            if _list_bytes(total, count) <= max_bytes:
                break
            dropped.add(index)
            total -= sizes[index]
            count -= 1
        bundle = [entry for i, entry in enumerate(bundle) if i not in dropped]

    return bundle, hashlib.sha256(_canonical(bundle)).hexdigest()


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------

_SCHEMA_BLOCK = """{
  "schema_version": 1,
  "activities": [
    {
      "kind": "code|test|investigation|decision|review|other",
      "summary": "string, <= 400 chars",
      "outcome": "string|null",
      "ticket_hint": "AB-76|null",
      "client_hint": "string|null",
      "project_hint": "string|null",
      "confidence": "high|medium|low",
      "ai_usage_score": "1-5|null -- how much of this activity's code the AI wrote: 1 = human coded it all (AI advised), 3 = genuinely mixed, 5 = essentially one-shot AI. Omit or null when the session does not show you enough to judge.",
      "evidence_event_ids": ["source_event id", "..."],
      "remaining": [{"title": "string", "detail": "string|null", "state": "open|blocked|waiting"}],
      "blockers": [{"title": "string", "detail": "string|null"}],
      "decisions": [{"statement": "string", "rationale": "string|null"}]
    }
  ]
}"""


def build_prompt(bundle: "list[dict]", *, context: dict) -> str:
    """The single `-p` prompt handed to `claude`.

    Carries the DESIGN §9.2 schema verbatim and the instruction to emit only
    the JSON object. Nothing in here asks the model to read files, run tools,
    or look anything up: the bundle is the whole world it is allowed to
    reason over.
    """
    context = context or {}
    lines = [
        "You are summarizing one Claude Code session for a private work ledger.",
        "",
        "Session context:",
        f"  session_id: {context.get('session_id')}",
        f"  cwd: {context.get('cwd')}",
        f"  git_branch: {context.get('git_branch')}",
        f"  events: {len(bundle)}",
        "",
        "Rules:",
        "  - Use ONLY the events below. Do not read files, run commands, or guess.",
        "  - Every activity must cite the ids of the events that support it in",
        "    evidence_event_ids. Cite nothing you did not see.",
        "  - One activity per distinct piece of work. Prefer fewer, accurate entries.",
        "  - summary is at most 400 characters and describes what was DONE.",
        "  - remaining/blockers are work that is still outstanding, not work finished.",
        "  - If you are unsure, say so with confidence: \"low\" rather than inventing detail.",
        "",
        "Respond with ONLY a single JSON object matching this schema exactly.",
        "No prose, no markdown fence, no commentary before or after it.",
        "Close every bracket: the last character you write must be the object's",
        "own closing brace.",
        "",
        _SCHEMA_BLOCK,
        "",
        "Events (JSON, oldest first):",
        json.dumps(bundle, sort_keys=True, ensure_ascii=False),
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Output validation
# --------------------------------------------------------------------------

def _balanced_from(text: str, start: int) -> "str | None":
    """The balanced `{...}` beginning at `start`, or None if it never closes.

    Tracks string literals and escapes so a brace inside a JSON string never
    opens or closes the object.
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def _candidate_objects(text: str):
    """Every balanced `{...}` in `text`, in order, outermost-first.

    Found the hard way in live verification against the real CLI: models put
    braces in prose before the answer ("returns a { ... } shaped object") and
    sometimes emit a small unrelated object first. Scanning only the first `{`
    reported `unbalanced JSON object` and took Stage B offline for the whole
    session. After a candidate is yielded the scan resumes past its end, so a
    nested object inside the answer is never offered as a rival to it.
    """
    start = text.find("{")
    while start >= 0:
        candidate = _balanced_from(text, start)
        if candidate is None:
            start = text.find("{", start + 1)
            continue
        yield candidate
        start = text.find("{", start + len(candidate))


def _close_truncated(text: str, start: int) -> "str | None":
    """`text[start:]` with the closing brackets it is missing, or None.

    Measured behaviour, not a hypothetical: on Claude Code 2.1.252 a Stage B
    answer of a few hundred characters arrives reliably missing the root
    object's final `}` -- every structure inside it closes, the document
    itself does not. Verified that the CLI is not at fault (a short answer
    round-trips through the `--output-format json` envelope byte-for-byte), so
    the last brace is simply not written.

    The repair is deliberately the smallest one that can help: append the
    closers the *already-parsed* structure implies, in the order the open
    brackets stack. Nothing is invented -- no key, no value, no string
    content. If the text ends inside a string literal it is left alone, since
    closing that would fabricate a truncated summary and the schema's length
    caps would happily accept it. Whatever comes back is then validated in
    full by `validate_output`, so a repair that produces a wrong-shaped
    document still fails, loudly, the same as before.
    """
    stack = []
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]":
            if not stack or stack[-1] != char:
                return None            # genuinely malformed, not truncated
            stack.pop()

    if in_string or not stack:
        return None
    return text[start:] + "".join(reversed(stack))


def _first_balanced_object(text: str) -> str:
    """The balanced `{...}` that is the model's answer.

    Prefers the first candidate that parses to an object carrying a
    `schema_version` key -- the document's own marker. Failing that, tries a
    bounded repair of a document truncated before its closing brackets. Only
    then falls back to the first candidate that parses at all, so the error a
    genuinely malformed answer produces still describes the answer rather than
    some brace in the preamble.
    """
    if not isinstance(text, str):
        raise StageBSchemaError("model output was not text")

    def _versioned(candidate):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            return False
        return isinstance(parsed, dict) and "schema_version" in parsed

    fallback = None
    for candidate in _candidate_objects(text):
        if _versioned(candidate):
            return candidate
        if fallback is None:
            try:
                json.loads(candidate)
            except ValueError:
                continue
            fallback = candidate

    start = text.find("{")
    if start >= 0:
        repaired = _close_truncated(text, start)
        if repaired is not None and _versioned(repaired):
            return repaired

    if fallback is not None:
        return fallback
    if "{" in text:
        raise StageBSchemaError("no parseable JSON object in model output")
    raise StageBSchemaError("no JSON object in model output")


def _require_str(value, field, cap):
    """A required narrative field: wrong type or empty is a real schema
    violation (the model gave structurally bad output -- reject the whole
    batch, same as a bad `kind`/`confidence`/evidence id). Running over
    `cap` is not: `summary`/`outcome`/`title`/`detail`/`statement`/
    `rationale` are free text with no structural meaning past their length,
    so truncating one is the same safe, silent cap `query.week` and
    `query.timesheet` already apply to a ticket's summary text -- not a
    reason to discard every other activity, remaining item, blocker, and
    decision in an otherwise-valid batch just because one caption ran a
    few characters long.
    """
    if not isinstance(value, str):
        raise StageBSchemaError(f"{field} must be a string, got {type(value).__name__}")
    text = value.strip()
    if not text:
        raise StageBSchemaError(f"{field} must not be empty")
    if len(text) > cap:
        text = text[:cap].rstrip()
    return text


def _optional_str(value, field, cap):
    if value is None:
        return None
    return _require_str(value, field, cap)


def _require_list(value, field):
    if value is None:
        return []
    if not isinstance(value, list):
        raise StageBSchemaError(f"{field} must be a list, got {type(value).__name__}")
    if len(value) > _MAX_LIST_ITEMS:
        raise StageBSchemaError(f"{field} has {len(value)} entries (max {_MAX_LIST_ITEMS})")
    return value


def _require_obj(value, field):
    if not isinstance(value, dict):
        raise StageBSchemaError(f"{field} must be an object, got {type(value).__name__}")
    return value


def validate_output(text: str, *, known_event_ids=None) -> dict:
    """Parse and strictly validate the model's answer, or raise.

    Extracts the first balanced JSON object (models wrap answers in prose and
    fences however hard you ask them not to), then checks types, enum
    membership, string caps, and -- when `known_event_ids` is supplied --
    that every `evidence_event_ids` entry names a real event from the bundle.
    Unknown keys are dropped rather than rejected, so a later model version
    adding a field does not take Stage B offline. Anything else raises
    `StageBSchemaError`, which `run` turns into a soft failure.

    `known_event_ids` is keyword-only with a default so the documented
    `validate_output(text) -> dict` call still works; without it the ids are
    only type-checked, because this function has no database of its own.
    """
    document = _require_obj(json.loads(_first_balanced_object(text)), "root")

    version = document.get("schema_version")
    if version != SCHEMA_VERSION:
        raise StageBSchemaError(
            f"schema_version must be {SCHEMA_VERSION}, got {version!r}")

    raw_activities = document.get("activities")
    if not isinstance(raw_activities, list):
        raise StageBSchemaError("activities must be a list")
    if len(raw_activities) > _MAX_ACTIVITIES:
        raise StageBSchemaError(
            f"activities has {len(raw_activities)} entries (max {_MAX_ACTIVITIES})")

    activities = []
    for index, raw in enumerate(raw_activities):
        where = f"activities[{index}]"
        raw = _require_obj(raw, where)

        kind = raw.get("kind")
        if kind not in KINDS:
            raise StageBSchemaError(f"{where}.kind must be one of {KINDS}, got {kind!r}")
        conf = raw.get("confidence")
        if conf not in CONFIDENCES:
            raise StageBSchemaError(
                f"{where}.confidence must be one of {CONFIDENCES}, got {conf!r}")

        evidence = _require_list(raw.get("evidence_event_ids"), f"{where}.evidence_event_ids")
        for position, event_id in enumerate(evidence):
            if not isinstance(event_id, str) or not event_id.strip():
                raise StageBSchemaError(
                    f"{where}.evidence_event_ids[{position}] must be a non-empty string")
            if known_event_ids is not None and event_id not in known_event_ids:
                raise StageBSchemaError(
                    f"{where}.evidence_event_ids[{position}] names no event in the bundle")

        remaining = []
        for position, item in enumerate(_require_list(raw.get("remaining"),
                                                      f"{where}.remaining")):
            field = f"{where}.remaining[{position}]"
            item = _require_obj(item, field)
            state = item.get("state", "open")
            if state not in REMAINING_STATES:
                raise StageBSchemaError(
                    f"{field}.state must be one of {REMAINING_STATES}, got {state!r}")
            remaining.append({
                "title": _require_str(item.get("title"), f"{field}.title", _MAX_TITLE),
                "detail": _optional_str(item.get("detail"), f"{field}.detail", _MAX_DETAIL),
                "state": state,
            })

        blockers = []
        for position, item in enumerate(_require_list(raw.get("blockers"),
                                                      f"{where}.blockers")):
            field = f"{where}.blockers[{position}]"
            item = _require_obj(item, field)
            blockers.append({
                "title": _require_str(item.get("title"), f"{field}.title", _MAX_TITLE),
                "detail": _optional_str(item.get("detail"), f"{field}.detail", _MAX_DETAIL),
            })

        decisions = []
        for position, item in enumerate(_require_list(raw.get("decisions"),
                                                      f"{where}.decisions")):
            field = f"{where}.decisions[{position}]"
            item = _require_obj(item, field)
            decisions.append({
                "statement": _require_str(item.get("statement"), f"{field}.statement",
                                          _MAX_TITLE),
                "rationale": _optional_str(item.get("rationale"), f"{field}.rationale",
                                           _MAX_DETAIL),
            })

        ai_usage_score = raw.get("ai_usage_score")
        if ai_usage_score is not None:
            if isinstance(ai_usage_score, bool) or not isinstance(ai_usage_score, int) \
                    or not (1 <= ai_usage_score <= 5):
                raise StageBSchemaError(
                    f"{where}.ai_usage_score must be an integer 1-5 or null,"
                    f" got {ai_usage_score!r}")

        activities.append({
            "kind": kind,
            "ai_usage_score": ai_usage_score,
            "summary": _require_str(raw.get("summary"), f"{where}.summary", _MAX_SUMMARY),
            "outcome": _optional_str(raw.get("outcome"), f"{where}.outcome", _MAX_OUTCOME),
            "ticket_hint": _optional_str(raw.get("ticket_hint"), f"{where}.ticket_hint",
                                         _MAX_HINT),
            "client_hint": _optional_str(raw.get("client_hint"), f"{where}.client_hint",
                                         _MAX_HINT),
            "project_hint": _optional_str(raw.get("project_hint"), f"{where}.project_hint",
                                          _MAX_HINT),
            "confidence": conf,
            "evidence_event_ids": list(evidence),
            "remaining": remaining,
            "blockers": blockers,
            "decisions": decisions,
        })

    return {"schema_version": SCHEMA_VERSION, "activities": activities}


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------

def min_interval_minutes() -> int:
    try:
        return int(config.get("stage_b.min_interval_minutes", 20))
    except (TypeError, ValueError):
        return 20


def max_bundle_bytes() -> int:
    try:
        return int(config.get("stage_b.max_bundle_bytes", 60000))
    except (TypeError, ValueError):
        return 60000


def enabled() -> bool:
    """`stage_b.enabled`, read fresh. A YAML `no`/`false`/`0` written outside
    the yamlite subset can arrive as a string (see `config`'s docstring), so
    the truthiness test is explicit rather than `bool(value)`."""
    value = config.get("stage_b.enabled", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("no", "false", "0", "off", "")
    return bool(value)


def _last_ok_run(conn, session_id: str):
    # `id DESC` breaks a tie SQLite does not otherwise guarantee to resolve
    # by insertion order: `started_at`/`created_at` are millisecond-precision
    # text timestamps, and two checkpoints written within the same
    # millisecond (a realistic case on a fast interpreter) would otherwise
    # leave this query's result order unspecified. `id` is a UUIDv7 -- text
    # that sorts lexicographically in generation order -- so it is a sound
    # final tiebreaker.
    return conn.execute(
        "SELECT started_at FROM capture_checkpoints "
        "WHERE hook_event='stage_b' AND status='ok' AND session_id IS ? "
        "ORDER BY started_at DESC, created_at DESC, id DESC LIMIT 1",
        (session_id,),
    ).fetchone()


def _cheap_guards(conn, session_id: str, *, now: str, force: bool) -> "tuple[bool, str]":
    """Every guard that does not need the bundle, in DESIGN §9.2 order.

    Split out from `should_run` so `run` can evaluate these first and build
    the bundle exactly once per pass (it is then threaded into the emptiness
    check and into `merge`). `should_run` keeps its documented behaviour by
    calling this and then building the bundle itself.
    """
    if os.environ.get(NESTED_ENV):
        return False, f"nested: {NESTED_ENV} is set in the environment"

    if shutil.which("claude") is None:
        return False, "claude is not on PATH"

    if not enabled():
        return False, "stage_b.enabled is false"

    if not force:
        interval = min_interval_minutes()
        row = _last_ok_run(conn, session_id)
        if row is not None and row["started_at"]:
            try:
                elapsed = (timeutil.parse_iso(now)
                           - timeutil.parse_iso(row["started_at"])).total_seconds() / 60.0
            except ValueError:
                elapsed = None
            if elapsed is not None and elapsed < interval:
                return False, (f"rate limited: last ok run {elapsed:.1f} min ago, "
                               f"minimum interval is {interval} min")

    return True, "ok"


def should_run(conn, session_id: str, *, now: str, force: bool = False,
               bundle=None) -> "tuple[bool, str]":
    """`(ok, reason)` -- may Stage B run for this session right now?

    Guards in DESIGN §9.2 order: the `WORK_LEDGER_NESTED` sentinel, `claude`
    on `PATH`, `stage_b.enabled`, the per-session rate limit, a non-empty
    bundle. `force=True` bypasses **only** the rate limit: an explicit
    `worklog stage-b --force` / `worklog reprocess` is a deliberate user
    action and should not be silently swallowed by the window, but it is not
    a licence to recurse, to invent a missing binary, or to override a
    configuration the user turned off.

    `bundle` is an optional prebuilt bundle (keyword-only, defaults to
    building one) so a caller that already has it does not pay for a second
    build; every rejection reason is identical either way.
    """
    ok, reason = _cheap_guards(conn, session_id, now=now, force=force)
    if not ok:
        return False, reason

    if bundle is None:
        bundle, _hash = build_bundle(conn, session_id, max_bytes=max_bundle_bytes())
    if not bundle:
        return False, "bundle is empty: nothing captured for this session"

    return True, "ok"


# --------------------------------------------------------------------------
# Model invocation
# --------------------------------------------------------------------------

def _child_env() -> dict:
    """The environment for anything that starts a `claude` session. The
    sentinel is set unconditionally -- this is the recursion guard (G8)."""
    return {**os.environ, NESTED_ENV: "1"}


def invoke_claude(prompt: str, *, model: str, timeout: int) -> str:
    """Run one non-interactive `claude` and return its assistant text.

    `cwd` is a fresh empty temp directory so the model session has no
    project, no `CLAUDE.md`, and nothing to wander into; `WORK_LEDGER_NESTED`
    is set so every hook that session fires short-circuits. Raises on a
    non-zero exit, a timeout, or an error envelope -- `run` converts all of
    those into a soft failure.
    """
    with tempfile.TemporaryDirectory(prefix="work-ledger-stage-b-") as workdir:
        completed = subprocess.run(
            ["claude", "-p", prompt, "--model", str(model), "--output-format", "json"],
            env=_child_env(), cwd=workdir, capture_output=True, text=True,
            timeout=int(timeout),
        )

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()[:200]
        raise RuntimeError(f"claude exited {completed.returncode}: {stderr or 'no stderr'}")

    stdout = (completed.stdout or "").strip()
    if not stdout:
        raise RuntimeError("claude produced no output")

    try:
        envelope = json.loads(stdout)
    except ValueError:
        # `--output-format json` should always give an envelope; if this
        # version did not, hand the raw text to the validator rather than
        # failing on a formatting detail.
        return stdout

    if isinstance(envelope, dict):
        if envelope.get("is_error"):
            raise RuntimeError(f"claude reported an error: {str(envelope.get('result'))[:200]}")
        result = envelope.get("result")
        if isinstance(result, str):
            return result
    return stdout


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------

def _idempotency_key(session_id, bundle_hash, index, suffix=None) -> str:
    material = f"stage_b|{session_id}|{bundle_hash}|{SCHEMA_VERSION}|{index}"
    if suffix is not None:
        material = f"{material}|{suffix}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _confidence_for(model_confidence: str) -> "tuple[str, int, str]":
    """`(bucket, score, reason)` for a Stage-B-authored row, capped at medium.

    Global Constraints: "Stage B alone never exceeds medium confidence."
    DESIGN §6 override 2 is the same rule; the reason string mirrors
    `confidence.classify`'s wording so a reader sees one vocabulary.
    """
    value = _MODEL_CONFIDENCE_SCORE.get(model_confidence, 20)
    resolved = confidence.bucket(value)
    reason = f"Stage B model confidence {model_confidence!r}"
    if resolved == "high":
        resolved = "medium"
        reason += "; capped at medium (Stage B only, DESIGN §6 override 2)"
    return resolved, value, reason


def _clean(text):
    """Redact model prose before it is persisted.

    Defence in depth, not a fourth choke point: the bundle the model saw was
    already redacted twice, so this should never fire. It costs nothing and
    Global Constraints are unambiguous that a secret must never reach a
    summary.
    """
    if text is None:
        return None
    return redact.redact(text)[0]


def _stage_a_rows(conn, session_row_id):
    if session_row_id is None:
        return []
    return conn.execute(
        "SELECT id, client_id, project_id, work_item_id, confidence "
        "FROM activities WHERE session_row_id=? AND source='stage_a' AND deleted_at IS NULL "
        "ORDER BY started_at, id",
        (session_row_id,),
    ).fetchall()


def _work_item_for_key(conn, ticket_hint):
    """The existing work item a `ticket_hint` names, or None.

    A read, never a write: Stage B may *recognize* a ticket the ledger
    already knows, but a work item conjured out of model text would be an
    entity nobody counted (SPEC §4). `work_items.upsert_external` stays
    Stage A's and Jira's to call.
    """
    if not ticket_hint:
        return None
    return conn.execute(
        "SELECT id, client_id, project_id FROM work_items "
        "WHERE external_key=? COLLATE NOCASE AND deleted_at IS NULL ORDER BY created_at LIMIT 1",
        (ticket_hint.strip().upper(),),
    ).fetchone()


def _external_key(conn, work_item_id) -> "str | None":
    if work_item_id is None:
        return None
    row = conn.execute(
        "SELECT external_key FROM work_items WHERE id=?", (work_item_id,)).fetchone()
    if row is None or not row["external_key"]:
        return None
    return row["external_key"].upper()


def _attribution(conn, activity, stage_a_rows):
    """`(client_id, project_id, work_item_id, anchor_row, note)` for one entry.

    Stage B *enriches* an attribution, it never overwrites one (DESIGN §9.2:
    "it may not change a high-confidence Stage A attribution"). So the order
    is: adopt the Stage A activity this entry corresponds to, if there is
    one; otherwise recognize the ticket hint against a work item that already
    exists; otherwise leave the attribution empty and let the row sit in the
    inference inbox. `anchor_row` is the Stage A activity whose attribution
    was adopted -- it is returned so the caller can be explicit about never
    writing to it.

    `note` records a ticket hint that was *not* honoured -- the model naming
    a ticket the counted evidence disagrees with, or one the ledger has never
    heard of. Silently dropping that is how an attribution disagreement
    becomes invisible, so the caller appends it to `confidence_reason`.
    """
    hint = (activity.get("ticket_hint") or "").strip()
    wanted = hint.upper()

    if wanted:
        for row in stage_a_rows:
            if _external_key(conn, row["work_item_id"]) == wanted:
                return row["client_id"], row["project_id"], row["work_item_id"], row, None

    if len(stage_a_rows) == 1:
        row = stage_a_rows[0]
        note = None
        if wanted:
            counted = _external_key(conn, row["work_item_id"])
            if counted != wanted:
                note = (f"discarded Stage B ticket hint {hint!r}: Stage A attributed this "
                        f"session to {counted or 'no ticket'}")
        return row["client_id"], row["project_id"], row["work_item_id"], row, note

    work_item = _work_item_for_key(conn, wanted)
    if work_item is not None:
        return work_item["client_id"], work_item["project_id"], work_item["id"], None, None

    note = (f"discarded Stage B ticket hint {hint!r}: names no work item the ledger knows"
            if wanted else None)
    return None, None, None, None, note


def _bounds(bundle_by_id, evidence_ids, session_row):
    """`(started_at, ended_at)` from the cited events, falling back to the
    session's start and no end when the model cited nothing -- matching
    Stage A's own contract (`infer.stage_a`'s `ended_at` is `None` absent
    real evidence) and the floor `query.timespent` already applies to any
    activity with no end evidence, rather than inventing one here.

    This used to fall back to `session_row["ended_at"] or
    session_row["last_seen_at"]`: correct for a short-lived session, but
    `last_seen_at` keeps advancing for as long as a session stays open or
    gets reopened, with no bound tying it to when *this* bundle's events
    actually happened. A session idle-finalized weeks after it started
    turned one activity's "ended_at" into that whole multi-week span,
    inflating its reported time-on-ticket by orders of magnitude the next
    time a report's window happened to clip into it.
    """
    times = sorted(bundle_by_id[i]["occurred_at"] for i in evidence_ids
                   if i in bundle_by_id and bundle_by_id[i].get("occurred_at"))
    if times:
        return times[0], times[-1]
    started = session_row["started_at"] if session_row is not None else None
    return started or timeutil.utc_now_iso(), None


# `source_events.event_type` -> the `activity_evidence.evidence_type` the
# schema allows (commit|file|tool_call|excerpt|jira|transcript|manual|test).
_EVIDENCE_TYPE = {
    "commit": "commit",
    "git_commit_signal": "commit",
    "files_touched": "file",
    "test_run": "test",
    "tool_use": "tool_call",
    "tool_failure": "tool_call",
    "manual": "manual",
    "jira_snapshot": "jira",
}


def _write_evidence(conn, activity_id, entry, bundle_by_id, device_id, now):
    """Link the events the model actually cited to the activity it wrote.

    `validate_output` has already proved every id in `evidence_event_ids`
    names a real event in the bundle, and throwing that away would leave a
    Stage B activity with no way back to what it was derived from -- the
    opposite of SPEC §14.2's "evidence is a pointer set". Append-only, like
    Stage A's evidence writes: a re-run adds links it does not already have
    and rewrites nothing. `excerpt` is deliberately not copied -- the pointer
    is the evidence, the payload already lives in `source_events`.
    """
    written = 0
    for event_id in entry["evidence_event_ids"]:
        event = bundle_by_id.get(event_id)
        if event is None:
            continue
        existing = conn.execute(
            "SELECT id FROM activity_evidence WHERE activity_id=? AND source_event_id=?",
            (activity_id, event_id),
        ).fetchone()
        if existing is not None:
            continue
        conn.execute(
            "INSERT INTO activity_evidence(id, activity_id, source_event_id, evidence_type, "
            "ref, excerpt, occurred_at, origin_device_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (ids.new_id(), activity_id, event_id,
             _EVIDENCE_TYPE.get(event["event_type"], "excerpt"), None, None,
             event.get("occurred_at"), device_id, now),
        )
        written += 1
    return written


def merge(conn, session_id: str, parsed: dict, bundle_hash: str, *, bundle=None) -> dict:
    """Write Stage B's activities, remaining items, blockers and decisions.

    Returns `{"activities": [ids], "created": n, "updated": n,
    "remaining": n, "decisions": n}`. Idempotent: every row is keyed by
    `sha256("stage_b|<session>|<bundle_hash>|<schema_version>|<index>")`, so
    re-running over the same bundle rewrites nothing and re-running over a
    changed bundle creates new rows rather than mutating old ones.

    Stage A rows are never written to. The Stage B row *adopts* a Stage A
    activity's client/project/work item when there is one to adopt -- that is
    what "enriches, never overwrites" means here: the model's narrative gets
    stored alongside the counted attribution instead of on top of it.
    """
    result = {"activities": [], "created": 0, "updated": 0, "remaining": 0,
              "remaining_actions": {}, "decisions": 0, "evidence": 0}

    session_row = conn.execute(
        "SELECT id, started_at, ended_at, last_seen_at FROM sessions WHERE session_id=?",
        (session_id,),
    ).fetchone()
    session_row_id = session_row["id"] if session_row is not None else None

    if bundle is None:
        bundle, _hash = build_bundle(conn, session_id, max_bytes=max_bundle_bytes())
    bundle_by_id = {entry["id"]: entry for entry in bundle}

    stage_a_rows = _stage_a_rows(conn, session_row_id)
    policy = confidence.narrative_policy(stage_b_ran=True)
    if not policy["allow_stage_b_fields"]:  # pragma: no cover - defensive
        return result

    # Opens (and commits) its own transaction: must complete before ours.
    device_id = entities.current_device(conn)
    now = timeutil.utc_now_iso()

    # (activity_id, entry) pairs whose remaining/decisions are written after
    # the transaction below closes -- `dedupe.resolve` opens its own.
    followups = []

    with connection.transaction(conn):
        for index, entry in enumerate(parsed.get("activities") or []):
            client_id, project_id, work_item_id, _anchor, hint_note = _attribution(
                conn, entry, stage_a_rows)
            resolved, score, reason = _confidence_for(entry["confidence"])
            if hint_note:
                reason = f"{reason}; {hint_note}"
            started_at, ended_at = _bounds(bundle_by_id, entry["evidence_event_ids"],
                                           session_row)
            summary = _clean(entry["summary"])
            outcome = _clean(entry["outcome"])
            key = _idempotency_key(session_id, bundle_hash, index)
            source_hash = hashlib.sha256(_canonical({
                "summary": summary, "outcome": outcome, "kind": entry["kind"],
                "confidence": resolved, "started_at": started_at, "ended_at": ended_at,
                "client_id": client_id, "project_id": project_id,
                "work_item_id": work_item_id,
            })).hexdigest()

            existing = conn.execute(
                "SELECT id, source, source_hash FROM activities WHERE idempotency_key=?",
                (key,),
            ).fetchone()

            if existing is None:
                activity_id = ids.new_id()
                conn.execute(
                    "INSERT INTO activities(id, session_row_id, client_id, project_id, "
                    "work_item_id, kind, summary, outcome, started_at, ended_at, confidence, "
                    "confidence_score, confidence_reason, source, in_inbox, idempotency_key, "
                    "source_hash, origin_device_id, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (activity_id, session_row_id, client_id, project_id, work_item_id,
                     entry["kind"], summary, outcome, started_at, ended_at, resolved,
                     score, reason, "stage_b", confidence.in_inbox(resolved), key,
                     source_hash, device_id, now, now),
                )
                result["created"] += 1
            else:
                activity_id = existing["id"]
                # Two guards, both deliberate:
                #
                # 1. `source == 'stage_b'` -- a user correction owns the row
                #    from then on. `corrections.move_activity` re-attributes
                #    without changing `source`, so this guard alone does not
                #    catch a move, which is why (2) exists.
                # 2. client_id / project_id / work_item_id are NOT in the SET
                #    list. Stage A omits them for the same reason: attribution
                #    is settled when the row is created, and a re-run that
                #    happened to resolve a different work item must never
                #    silently relocate an activity a human has already moved,
                #    reported on, or invoiced against. Stage B rewrites its own
                #    narrative here, nothing else. A genuinely changed
                #    attribution belongs to a new bundle hash (a new row) or to
                #    an explicit correction.
                if existing["source"] == "stage_b" and existing["source_hash"] != source_hash:
                    conn.execute(
                        "UPDATE activities SET kind=?, summary=?, outcome=?, started_at=?, "
                        "ended_at=?, confidence=?, confidence_score=?, confidence_reason=?, "
                        "in_inbox=?, source_hash=?, updated_at=? WHERE id=?",
                        (entry["kind"], summary, outcome, started_at, ended_at, resolved,
                         score, reason, confidence.in_inbox(resolved), source_hash, now,
                         activity_id),
                    )
                    result["updated"] += 1

            for position, decision in enumerate(entry["decisions"]):
                decision_key = _idempotency_key(session_id, bundle_hash, index,
                                                f"decision|{position}")
                if conn.execute("SELECT 1 FROM decisions WHERE idempotency_key=?",
                                (decision_key,)).fetchone() is not None:
                    continue
                conn.execute(
                    "INSERT INTO decisions(id, work_item_id, activity_id, client_id, project_id, "
                    "statement, rationale, decided_at, confidence, source, idempotency_key, "
                    "origin_device_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ids.new_id(), work_item_id, activity_id, client_id, project_id,
                     _clean(decision["statement"]), _clean(decision["rationale"]),
                     ended_at or started_at, resolved, "stage_b", decision_key,
                     device_id, now, now),
                )
                result["decisions"] += 1

            result["evidence"] += _write_evidence(conn, activity_id, entry, bundle_by_id,
                                                  device_id, now)
            result["activities"].append(activity_id)
            followups.append((activity_id, client_id, project_id, work_item_id,
                              resolved, started_at, ended_at, entry))

    # Outside the transaction: `dedupe.resolve` opens its own (and calls
    # `entities.current_device`, which opens one too).
    for (activity_id, client_id, project_id, work_item_id, resolved,
         started_at, ended_at, entry) in followups:
        occurred_at = ended_at or started_at
        items = [(item["title"], item["detail"],
                  _STATE_TO_KIND.get(item["state"], "remaining"), item["state"])
                 for item in entry["remaining"]]
        # A reported blocker is a blocked item, not an open one -- carrying the
        # state through means `remaining.create` writes the right initial
        # transition instead of opening it and immediately needing a correction.
        items += [(item["title"], item["detail"], "blocker", "blocked")
                  for item in entry["blockers"]]
        for title, detail, kind, state in items:
            _item_id, action = dedupe.resolve(
                conn, title=_clean(title), work_item_id=work_item_id, client_id=client_id,
                project_id=project_id, occurred_at=occurred_at, confidence=resolved,
                source="stage_b", detail=_clean(detail), kind=kind, state=state,
                activity_id=activity_id,
            )
            # What `dedupe.resolve` actually did, not how many times it was
            # called: "3 remaining" that were all reinforcements of one item is
            # a different report from three new ones.
            result["remaining_actions"][action] = result["remaining_actions"].get(action, 0) + 1
            result["remaining"] += 1

    _refine_ai_usage(conn, parsed, stage_a_rows)

    return result


def _refine_ai_usage(conn, parsed: dict, stage_a_rows) -> None:
    """Apply the model's per-activity `ai_usage_score` suggestions.

    The model refines, never decides: each suggestion is clamped to one step
    from the deterministic baseline (`work_items.ai_usage_score`, freshly
    recomputed here so merged evidence is included), a refinement is stored
    at `medium` confidence at most (Ruling 6's split: deterministic counting
    may be high, a model opinion may not), and a score a live correction
    pins is never touched (`recompute_for_work_item` skips it, and so does
    the write below). With no baseline (score NULL: no countable evidence)
    the suggestion is dropped rather than trusted on its own.
    """
    from work_ledger.infer import ai_usage as _ai_usage

    suggestions = {}
    for entry in parsed.get("activities") or []:
        model_score = entry.get("ai_usage_score")
        if model_score is None:
            continue
        _client, _project, work_item_id, _anchor, _note = _attribution(
            conn, entry, stage_a_rows)
        if work_item_id is None:
            continue
        # Two activities on one ticket: keep the strongest claim of AI work.
        current = suggestions.get(work_item_id)
        suggestions[work_item_id] = model_score if current is None else max(current, model_score)

    for work_item_id, model_score in sorted(suggestions.items()):
        outcome = _ai_usage.recompute_for_work_item(conn, work_item_id)
        if outcome.get("skipped"):
            continue
        baseline = outcome.get("score")
        if baseline is None:
            continue
        clamped = max(baseline - 1, min(baseline + 1, model_score))
        if clamped == baseline:
            continue
        row = conn.execute(
            "SELECT ai_usage_reason FROM work_items WHERE id=? AND deleted_at IS NULL",
            (work_item_id,)).fetchone()
        if row is None:
            continue
        reason = f"{row['ai_usage_reason']}; Stage B adjusted {baseline} -> {clamped}"
        with connection.transaction(conn):
            conn.execute(
                "UPDATE work_items SET ai_usage_score=?, ai_usage_confidence='medium',"
                " ai_usage_reason=?, updated_at=? WHERE id=?",
                (clamped, reason, timeutil.utc_now_iso(), work_item_id))


# --------------------------------------------------------------------------
# The Stage-A-only remaining pass (Controller ruling, Task 28)
# --------------------------------------------------------------------------

_STAGE_A_REMAINING_EVENT = "stage_a_remaining"


def persist_stage_a_remaining(conn, session_id: str, *, now: str) -> dict:
    """Route Stage A's extracted remaining/blockers through `dedupe.resolve`.

    Called on every path where Stage B produced nothing usable, so the
    Stage-A-only mode still persists remaining work instead of extracting it
    and dropping it on the floor.

    Runs at most once per distinct set of extracted titles: the pass is
    fenced by an append-only `capture_checkpoints` row whose key is a hash of
    what was extracted, and `capture_checkpoints` has a unique index on
    `checkpoint_key`. Re-running over unchanged evidence is therefore a true
    no-op rather than a mention-count inflator, while genuinely new evidence
    produces a new key and a new pass.
    """
    result = {"resolved": 0, "skipped": False, "actions": {}}
    try:
        from work_ledger.infer import stage_a
        drafts = stage_a.split_activities(conn, session_id)
    except Exception:
        # Stage A being unavailable or unhappy must never turn a soft Stage B
        # failure into a hard one.
        return {"resolved": 0, "skipped": True, "actions": {}}

    pending = []
    for draft in drafts:
        for item in draft.remaining or []:
            title = (item.get("title") or "").strip()
            if not title:
                continue
            pending.append((draft, item, title))

    if not pending:
        return result

    fingerprint = hashlib.sha256(_canonical(sorted(
        (draft.work_item_id or "", item.get("kind") or "remaining", title)
        for draft, item, title in pending
    ))).hexdigest()
    key = f"{_STAGE_A_REMAINING_EVENT}:{session_id}:{fingerprint}"

    if conn.execute("SELECT 1 FROM capture_checkpoints WHERE checkpoint_key=?",
                    (key,)).fetchone() is not None:
        result["skipped"] = True
        return result

    # Claim the fence BEFORE doing the work, not after. `checkpoint_key` is
    # unique, so this is the atomic claim; writing it afterwards would mean a
    # crash (or a `DatabaseBusy`) part-way through the loop leaves no fence,
    # and the next pass re-resolves the items it already resolved -- inflating
    # `mention_count` exactly as the fence exists to prevent.
    checkpoints.checkpoint(
        conn, session_id=session_id, hook_event=_STAGE_A_REMAINING_EVENT, key=key,
        status="ok", detail=f"claimed {len(pending)} Stage A remaining item(s)",
        started_at=now,
    )

    for draft, item, title in pending:
        _item_id, action = dedupe.resolve(
            conn, title=title, work_item_id=draft.work_item_id, client_id=draft.client_id,
            project_id=draft.project_id, occurred_at=item.get("occurred_at") or now,
            confidence="medium", source="stage_a", detail=item.get("detail"),
            kind=item.get("kind") or "remaining",
            state="blocked" if (item.get("kind") == "blocker") else "open",
            source_event_id=item.get("source_event_id"),
        )
        result["actions"][action] = result["actions"].get(action, 0) + 1
        result["resolved"] += 1

    return result


# --------------------------------------------------------------------------
# The flow
# --------------------------------------------------------------------------

def _record(conn, session_id, *, status, detail, started_at):
    """Best-effort checkpoint. Stage B failing must never raise at its
    caller, and neither may the record of it failing.

    The detail is redacted first: it can carry a subprocess's stderr or an
    exception message, and Global Constraints put checkpoints squarely inside
    "never in SQLite ... never in logs ... or diagnostic output".
    """
    try:
        checkpoints.checkpoint(
            conn, session_id=session_id, hook_event="stage_b",
            key=f"stage_b:{session_id}:{started_at}", status=status,
            detail=redact.redact(detail or "")[0][:512], started_at=started_at,
        )
    except Exception:
        pass


def run(conn, session_id: str, *, force: bool = False, now: "str | None" = None) -> dict:
    """The whole Stage B flow for one session. Never raises.

    `now` defaults to `timeutil.utc_now_iso()` and is threaded into both
    `should_run` **and** the `capture_checkpoints` row this writes, so a test
    can pin both sides of the rate-limit window (Ruling 30(e)).

    Two kinds of non-run, and the checkpoint says which (Controller ruling,
    Task 29 review IMPORTANT 1):

    * a **guard** declined -- nested sentinel, `claude` absent,
      `stage_b.enabled: false`, rate limited, empty bundle -> `status='skipped'`.
      These are the ordinary, expected, working-as-designed outcomes; the
      rate limiter alone produces one on most Stop hooks. Recording them as
      `failed` made `worklog doctor` warn about a healthy system.
    * something actually **broke** -- a timeout, a non-zero exit, unparseable
      output, a schema violation, a merge that raised, or a guard evaluation
      that itself threw -> `status='failed'`.

    Either way Stage A's remaining work is routed through `dedupe.resolve`
    and the return is `{"ran": False, "reason": …}`. Stage A's activities and
    their attribution confidence are left exactly as they were (DESIGN §6
    override 3).
    """
    now = now or timeutil.utc_now_iso()

    def _not_run(reason, status):
        _record(conn, session_id, status=status, detail=reason, started_at=now)
        try:
            persist_stage_a_remaining(conn, session_id, now=now)
        except Exception:
            pass
        return {"ran": False, "reason": reason, "status": status}

    def _skip(reason):
        return _not_run(reason, "skipped")

    def _soft_fail(reason):
        return _not_run(reason, "failed")

    # Cheap guards first, then exactly one bundle build for the whole pass --
    # threaded into the emptiness check, the prompt, the validator, and merge.
    try:
        ok, reason = _cheap_guards(conn, session_id, now=now, force=force)
        if not ok:
            return _skip(reason)
        bundle, bundle_hash = build_bundle(conn, session_id, max_bytes=max_bundle_bytes())
    except Exception as exc:
        return _soft_fail(f"guard check failed: {type(exc).__name__}: {exc}")

    if not bundle:
        return _skip("bundle is empty: nothing captured for this session")

    try:
        session_row = conn.execute(
            "SELECT cwd, git_branch FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        prompt = build_prompt(bundle, context={
            "session_id": session_id,
            "cwd": session_row["cwd"] if session_row is not None else None,
            "git_branch": session_row["git_branch"] if session_row is not None else None,
        })
        text = invoke_claude(prompt,
                             model=config.get("stage_b.model", "sonnet"),
                             timeout=config.get("stage_b.timeout_seconds", 120))
        parsed = validate_output(text, known_event_ids={e["id"] for e in bundle})
        merged = merge(conn, session_id, parsed, bundle_hash, bundle=bundle)
    except subprocess.TimeoutExpired:
        return _soft_fail(f"claude timed out after {config.get('stage_b.timeout_seconds', 120)}s")
    except StageBSchemaError as exc:
        return _soft_fail(f"schema violation: {exc}")
    except Exception as exc:
        return _soft_fail(f"{type(exc).__name__}: {exc}")

    _record(conn, session_id, status="ok",
            detail=(f"bundle={len(bundle)} events, created={merged['created']}, "
                    f"updated={merged['updated']}, remaining={merged['remaining']}, "
                    f"decisions={merged['decisions']}, evidence={merged['evidence']}"),
            started_at=now)
    return {"ran": True, "reason": "ok", "status": "ok", "bundle_hash": bundle_hash,
            "bundle_events": len(bundle), **merged}


# --------------------------------------------------------------------------
# Detached spawn (the Stop hook's only entry point)
# --------------------------------------------------------------------------

def worklog_bin() -> Path:
    """The repo's `bin/worklog`, resolved from this file's own location:
    `<root>/src/work_ledger/infer/stage_b.py`."""
    return Path(__file__).resolve().parents[3] / "bin" / "worklog"


def spawn(session_id: str) -> None:
    """Start `worklog stage-b --session <id>` detached and return immediately.

    Ruling 8: the Stop hook never waits on, polls, or reads the result of
    Stage B. `start_new_session=True` puts the child in its own process group
    so Claude Code exiting does not take it with it; stdin is `/dev/null` and
    both output streams append to `logs/stage_b.log`, because a background
    job that inherits a terminal is a background job that eventually blocks
    on one.

    Never raises: a Stop hook that cannot spawn must still return 0.
    """
    if not session_id:
        return
    try:
        paths.ensure_dirs()
        log_path = paths.log_dir() / "stage_b.log"
        env = {**os.environ, NESTED_ENV: "1", SPAWNED_ENV: "1"}
        with open(log_path, "a") as log:
            subprocess.Popen(
                [sys.executable, str(worklog_bin()), "stage-b", "--session", session_id],
                start_new_session=True, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, env=env,
            )
    except Exception as exc:
        # Still never raises -- but a Stage B that silently never starts is
        # indistinguishable from one that ran and found nothing, so the reason
        # goes to the plugin log the same way every other swallowed hook
        # exception does. Imported here, not at module scope: `hooks_runtime`
        # imports this module (lazily, inside the Stop handler), and a
        # top-level import back would make that a cycle waiting to happen.
        try:
            from work_ledger import hooks_runtime
            hooks_runtime.log_exception(exc, "stage_b.spawn")
        except Exception:
            pass
        return
