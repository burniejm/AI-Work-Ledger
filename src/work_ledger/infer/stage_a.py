"""Stage A: deterministic attribution, session splitting, and templated
activities (Task 26; SPEC §4/§6/§8, DESIGN §6/§9.1).

Stage A is the layer that always runs and never guesses prose. Everything
it writes is derived from counted facts already in `source_events`:

* **Attribution** comes from ticket keys matched against a connection's
  `ticket_patterns`, plus the repository mapping the event's `cwd` resolves
  to. Each signal carries the DESIGN §6 point value; the sum is bucketed.
  **Ruling 6:** a *direct* signal -- a ticket key in the git branch name or
  in a commit message -- plus a persisted repository mapping reaches
  `high`, and Stage B's absence does not lower it. What Stage B's absence
  caps is the *narrative*: the summary stays a template and no
  `source='stage_b'` row is written.
* **Splitting** is one activity per `(client_id, project_id, work_item_id)`
  tuple seen in the session (SPEC §4).
* **Summaries** are composed from the three templates below and nothing
  else (Ruling 21). Stage A never invents a sentence it cannot count.

The scoring table (`SIGNAL_POINTS`), `classify`, and `narrative_policy` live
in `infer.confidence` (Task 27); this module is the call site.

One seam remains deliberate and belongs to a later task:

* `ActivityDraft.remaining` / `.decisions` are *extracted* here (from
  commit subjects and bodies only, never from file contents) but not
  persisted: `infer.extract` and `infer.dedupe` own that write in Task 28,
  because the DESIGN §7 similarity bands and `possible_duplicate`
  contradiction live there. Writing rows here would only be ripped out.

Non-reentrancy: `db.connection.transaction` is not reentrant, so every
helper that opens its own transaction (`entities.current_device`,
`work_items.upsert_external`, `work_items.get_or_create_local`) is called
from `split_activities` -- *before* `run` opens the single transaction that
writes the activities and their evidence.

CRITICAL data fact: transcript reconciliation stores both user prompts and
assistant turns as `event_type='prompt'`, with the real role in
`payload.role`. Every prompt read here goes through `_is_user_prompt`;
a bare `event_type == 'prompt'` test would credit Claude's own narration
as a user statement.
"""
import hashlib
import json
import re

from work_ledger import entities, ids, manual, redact, timeutil, work_items
from work_ledger.capture import events as capture_events, sessions
from work_ledger.db import connection
from work_ledger.infer import confidence

__all__ = [
    "COMMIT_TEMPLATE",
    "EDIT_TEMPLATE",
    "TEST_TEMPLATE",
    "TicketCandidate",
    "ActivityDraft",
    "ticket_patterns",
    "extract_ticket_candidates",
    "parse_test_result",
    "split_activities",
    "run",
]

# --------------------------------------------------------------------------
# Summary templates (Ruling 21). Stage A composes summaries from these and
# nothing else; `s`/`fs` are "" when the count is 1 (conditional plural).
# --------------------------------------------------------------------------

COMMIT_TEMPLATE = "{n} commit{s} on {branch} touching {m} file{fs}"
EDIT_TEMPLATE = "Edited {m} file{fs} under {prefix}; no commits"
TEST_TEMPLATE = "Ran the test suite: {detail}"

# Clause separator, and the compact test clause used when the test result is
# joined with a commit/edit clause: the brief's worked example renders
# "1 commit on feature/ab-76 touching 2 files; pytest passed", so TEST_TEMPLATE
# is the *standalone* form and `detail` is the clause when other facts exist.
_CLAUSE_JOIN = "; "


# What the templates say when the session had no branch at all.
_NO_BRANCH = "(no branch)"


def _plural(count: int) -> str:
    return "" if count == 1 else "s"


# --------------------------------------------------------------------------
# Ticket patterns
# --------------------------------------------------------------------------

# Characters a glob may carry through to the regex untouched, so `"AB-*"`
# compiles to exactly `\bAB-\d+\b` rather than an equivalent-but-noisy
# `\bSK\-\d+\b`.
_GLOB_LITERAL_RE = re.compile(r"[A-Za-z0-9_-]")


def _glob_to_regex(glob: str) -> str:
    """`"AB-*"` -> `r"\\bAB-\\d+\\b"`.

    A `*` immediately after a `-` is the issue *number* and becomes `\\d+`
    (Jira keys are `PROJECT-123`); any other `*` is a project-key run and
    becomes `[A-Z0-9]+`, so `"SKY*-*"` -> `\\bSKY[A-Z0-9]+-\\d+\\b`.
    `?` matches one key character.
    """
    out = []
    previous = ""
    for ch in glob:
        if ch == "*":
            out.append(r"\d+" if previous == "-" else "[A-Z0-9]+")
        elif ch == "?":
            out.append("[A-Z0-9]")
        elif _GLOB_LITERAL_RE.fullmatch(ch):
            out.append(ch)
        else:
            out.append(re.escape(ch))
        previous = ch
    return r"\b" + "".join(out) + r"\b"


def ticket_patterns(conn) -> list:
    """`[(connection_id, compiled_pattern), ...]` for every live, enabled
    connection -- one entry per `ticket_patterns` entry, so a connection with
    two patterns contributes two rows.

    Compiled case-insensitively: branch names are routinely lowercased
    (`feature/ab-76`) while the key itself is uppercase. Callers uppercase
    every match before using it as an `external_key`.
    """
    rows = conn.execute(
        "SELECT id, ticket_patterns_json FROM connections "
        "WHERE enabled=1 AND deleted_at IS NULL ORDER BY name, id"
    ).fetchall()

    compiled = []
    for row in rows:
        try:
            patterns = json.loads(row["ticket_patterns_json"] or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(patterns, list):
            continue
        for glob in patterns:
            if not isinstance(glob, str) or not glob.strip():
                continue
            try:
                compiled.append((row["id"], re.compile(_glob_to_regex(glob.strip()),
                                                       re.IGNORECASE)))
            except re.error:
                continue  # a malformed pattern must never break capture
    return compiled


# --------------------------------------------------------------------------
# Event payload readers
# --------------------------------------------------------------------------

_COMMIT_EVENT_TYPES = ("commit", "git_commit_signal")
# Tools whose input names a file the user changed. Read-only tools are
# deliberately absent (SPEC §6.2: routine reads are evidence, not activity).
_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
_PATH_KEYS = ("file_path", "path", "notebook_path")


def _payload(row) -> dict:
    try:
        value = json.loads(row["payload_json"])
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _is_user_prompt(payload: dict) -> bool:
    """True only for a real user turn.

    Transcript reconciliation stores assistant turns as `event_type='prompt'`
    too, distinguished solely by `payload.role`. Hook-origin prompt rows (the
    UserPromptSubmit marker) carry no `role` but do carry `prompt`, and are
    user turns by construction.
    """
    role = payload.get("role")
    if role is not None:
        return role == "user"
    return "prompt" in payload


def _prompt_text(payload: dict) -> str:
    for key in ("text", "prompt"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _creates_a_commit(command, subject) -> bool:
    """True only for a `git_commit_signal` row that actually created a commit.

    Ruling 30(g) emits that row for `commit|merge|push|rebase|cherry-pick|
    revert|tag` alike, so counting every one of them turned a push-only
    session into "1 commit ... 0 files". A real subject is conclusive
    (`hooks_runtime._commit_subject` only parses one out of `git commit`'s
    `[branch sha] subject` line); otherwise the verb itself must be `commit`.
    """
    if isinstance(subject, str) and subject.strip():
        return True
    if not isinstance(command, str):
        return False
    match = capture_events.GIT_COMMIT_RE.match(command)
    return match is not None and match.group(1) == "commit"


def _commit_text(payload: dict) -> str:
    parts = [payload.get("subject"), payload.get("body")]
    return "\n".join(part for part in parts if isinstance(part, str) and part)


def _tool_calls(row, payload: dict):
    """Normalize the three tool-event payload shapes into
    `(tool_use_id, tool_name, tool_input, tool_response)` tuples.

    1. hook PostToolUse: `{"tool_name", "tool_input", "tool_response"}`
    2. transcript record: `{"tool_uses": [{"id", "name", "input"}], "tool_results": [...]}`
    3. anything else: the payload itself is treated as the input, with the
       tool name taken from the row or a bare `tool`/`tool_name` key.

    `tool_use_id` is what lets the caller collapse the *same* tool call
    captured twice -- once by the PostToolUse hook and once by transcript
    reconciliation. It is `None` only when neither the row nor the payload
    carries one.

    Shape 2 note: a transcript `tool_use` block and its `tool_result` live in
    two different records (the result arrives in the *next*, user-role
    record), so the assistant record's blocks normally have no response at
    all. Results present in the same record are matched by `tool_use_id`
    rather than by position, because one assistant turn can issue several
    tool calls.
    """
    blocks = payload.get("tool_uses")
    if isinstance(blocks, list) and blocks:
        by_id = {}
        results = payload.get("tool_results")
        if isinstance(results, list):
            for result in results:
                if isinstance(result, dict):
                    by_id[result.get("tool_use_id")] = result
        for block in blocks:
            if not isinstance(block, dict):
                continue
            tool_input = block.get("input")
            block_id = block.get("id")
            response = by_id.get(block_id)
            yield (block_id or row["tool_use_id"],
                   block.get("name"),
                   tool_input if isinstance(tool_input, dict) else {},
                   response if isinstance(response, dict) else {})
        return

    name = row["tool_name"] or payload.get("tool_name") or payload.get("tool")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = payload
    tool_response = payload.get("tool_response")
    if not isinstance(tool_response, dict):
        tool_response = {}
    yield row["tool_use_id"] or payload.get("tool_use_id"), name, tool_input, tool_response


def _edited_paths(tool_name, tool_input: dict) -> list:
    """File paths a write-shaped tool call touched. Empty for every other tool."""
    if tool_name not in _WRITE_TOOLS:
        return []
    found = []
    for key in _PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            found.append(value)
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if isinstance(edit, dict):
                value = edit.get("file_path")
                if isinstance(value, str) and value:
                    found.append(value)
    return found


def _tool_text(payload: dict) -> str:
    """Every string leaf of a tool event, for `ticket_in_tool_io` scanning.
    Already redacted and size-capped at ingest, so this is bounded."""
    return json.dumps(payload, ensure_ascii=False)


# --------------------------------------------------------------------------
# Test-result parsing (DESIGN §9.1)
# --------------------------------------------------------------------------

# Ordered: the first match wins, so `npm run test` is npm, not a bare word.
# Every pattern is matched with `re.match` against a *normalized command
# segment* (see `_test_command_segments`), so the runner must be the command
# actually being executed -- `grep -rn pytest .` and
# `git commit -m "fix pytest flake"` are not test runs (SPEC §6.2).
_RUNNERS = (
    ("pytest", re.compile(r"pytest\b")),
    ("unittest", re.compile(r"unittest\b")),
    ("go test", re.compile(r"go\s+test\b")),
    ("npm test", re.compile(r"npm\s+(?:run\s+)?test\b")),
    ("yarn test", re.compile(r"yarn\s+test\b")),
    ("cargo test", re.compile(r"cargo\s+test\b")),
    ("gradle test", re.compile(r"gradlew?\b.*\btest\b")),
    ("xcodebuild test", re.compile(r"xcodebuild\b.*\btest\b")),
    ("mvn test", re.compile(r"mvn\b.*\btest\b")),
    ("rspec", re.compile(r"rspec\b")),
)

# A shell command is split on these and each segment is judged on its own, so
# `make build && pytest -q` is a pytest run.
_SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||;|\|")
# Leading noise a real invocation may carry before the runner itself.
_WRAPPER_WORDS = frozenset({"env", "time", "nice", "sudo", "npx", "uv", "uvx", "exec"})
_WRAPPER_PAIRS = frozenset({"poetry run", "pipenv run", "bundle exec", "pdm run",
                            "hatch run", "uv run", "npm exec"})
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PYTHON_RE = re.compile(r"^python(?:3(?:\.\d+)?)?$")
_LEADING_DIR_RE = re.compile(r"^\S*/")
# Head verbs that are never a test run, however the rest of the line reads.
# `grep -rn pytest .` and `git commit -m "fix pytest flake"` land here.
_NON_TEST_HEAD_VERBS = frozenset({
    "git", "grep", "rg", "ag", "ack", "echo", "printf", "cat", "less", "more",
    "head", "tail", "find", "sed", "awk", "ls", "man", "which", "type",
    "rm", "cp", "mv", "touch", "mkdir", "open", "code", "vim", "nano",
})

# `0 failed` is a *pass*: the count must be non-zero for this marker to fire,
# which is what stops `cargo test`'s "5 passed; 0 failed" reading as a failure.
_FAIL_COUNT_RE = re.compile(
    r"\b[1-9]\d*\s+(?:failed|failing|failures?|errors?)\b", re.IGNORECASE)
# Bare word markers are deliberately CASE SENSITIVE: `FAILED` / `FAIL` are what
# pytest, go test and xcodebuild print on a failing run, whereas the lowercase
# word "failed" appears in ordinary passing output ("5 passed; 0 failed").
_FAIL_WORD_RE = re.compile(r"\bFAILED\b|\bFAIL\b|\*\*\* Failures|\bTest failed\b")
_PASS_MARKER_RE = re.compile(
    r"\b\d+\s+(?:passed|passing)\b|\bOK\b|\bBUILD SUCCEEDED\b|\bPASS\b|\bok\b")

_OUTPUT_KEYS = ("stdout", "stderr", "output", "content", "result")


def _test_command_segments(command: str):
    """Yield each `&&`/`||`/`;`/`|`-separated segment of `command` with its
    environment assignments, wrapper words (`env`, `npx`, `poetry run`, ...),
    `python -m` prefix and leading directory stripped -- and drop any segment
    whose head verb can never be a test run."""
    for raw in _SEGMENT_SPLIT_RE.split(command):
        tokens = raw.strip().split()
        index = 0
        while index < len(tokens):
            token = tokens[index]
            lowered = token.lower()
            if _ENV_ASSIGN_RE.match(token):
                index += 1
                continue
            if lowered in _WRAPPER_WORDS:
                index += 1
                continue
            if (index + 1 < len(tokens)
                    and f"{lowered} {tokens[index + 1].lower()}" in _WRAPPER_PAIRS):
                index += 2
                continue
            if (_PYTHON_RE.match(_LEADING_DIR_RE.sub("", lowered))
                    and index + 1 < len(tokens) and tokens[index + 1] == "-m"):
                index += 2
                continue
            break
        rest = tokens[index:]
        if not rest:
            continue
        rest[0] = _LEADING_DIR_RE.sub("", rest[0])
        if not rest[0] or rest[0].lower() in _NON_TEST_HEAD_VERBS:
            continue
        yield " ".join(rest)


def _response_text(response) -> str:
    if isinstance(response, str):
        return response
    if not isinstance(response, dict):
        return ""
    parts = []
    for key in _OUTPUT_KEYS:
        value = response.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    return "\n".join(parts)


def parse_test_result(command: str, response) -> "dict | None":
    """`{"runner": ..., "passed": bool, "detail": ...}` for a recognized test
    command, or `None` when `command` is not a test run.

    The verdict is the exit code, overridden by an explicit failure marker in
    the output (a runner that exits 0 while reporting failures is still a
    failure). `detail` is deliberately the bare `"<runner> passed"` /
    `"<runner> failed"` clause -- it is what the summary templates join.
    """
    if not isinstance(command, str) or not command.strip():
        return None

    runner = None
    for segment in _test_command_segments(command):
        for name, pattern in _RUNNERS:
            if pattern.match(segment):
                runner = name
                break
        if runner is not None:
            break
    if runner is None:
        return None

    text = _response_text(response)
    exit_code = response.get("exit_code") if isinstance(response, dict) else None

    # No exit code AND no output means the result simply is not in this
    # record -- which is the *normal* shape of a transcript `tool_use` block,
    # whose `tool_result` arrives in the next record. Guessing "failed" here
    # turned every passing suite on the dual-capture path into a failure.
    # Recording nothing is correct: the hook row for the same call carries
    # the exit code, and `_test_key` collapses the pair.
    if exit_code is None and not text.strip():
        return None

    if _FAIL_COUNT_RE.search(text) or _FAIL_WORD_RE.search(text):
        passed = False
    elif exit_code is not None:
        try:
            passed = int(exit_code) == 0
        except (TypeError, ValueError):
            passed = False
    else:
        passed = bool(_PASS_MARKER_RE.search(text))

    return {"runner": runner, "passed": passed,
            "detail": f"{runner} {'passed' if passed else 'failed'}"}


# --------------------------------------------------------------------------
# Deterministic extraction from commit subjects and bodies
# --------------------------------------------------------------------------
# Never from file contents (a TODO in source code is not a statement about
# this session's work). Task 28's `infer.extract` takes ownership of these.

_REMAINING_TRAILER_RE = re.compile(r"^[ \t]*Remaining[ \t]*:[ \t]*(.+?)[ \t]*$",
                                   re.MULTILINE | re.IGNORECASE)
_TODO_KEY_RE = re.compile(r"^[ \t]*TODO\(([^)]*)\)[ \t]*:[ \t]*(.+?)[ \t]*$",
                          re.MULTILINE)
_TODO_BARE_RE = re.compile(r"^[ \t]*TODO[ \t]*:[ \t]*(.+?)[ \t]*$", re.MULTILINE)
_FIXME_RE = re.compile(r"^[ \t]*FIXME[ \t]*:[ \t]*(.+?)[ \t]*$", re.MULTILINE)
_DECISION_RE = re.compile(r"^[ \t]*Decision[ \t]*:[ \t]*(.+?)[ \t]*$",
                          re.MULTILINE | re.IGNORECASE)
_BLOCKED_RE = re.compile(r"^[ \t]*Blocked[ \t]*:[ \t]*(.+?)[ \t]*$",
                         re.MULTILINE | re.IGNORECASE)

_MAX_EXCERPT_CHARS = 512
# SPEC §14.2: evidence is a pointer set, not a copy of the work. A commit
# touching hundreds of files contributes its count to the summary and a
# bounded sample of paths to `activity_evidence`.
_MAX_FILE_EVIDENCE = 50


def _clean(text: str) -> str:
    """Redact and bound a string before it is persisted or handed on."""
    redacted, _fired = redact.redact(text)
    return redacted[:_MAX_EXCERPT_CHARS]


def _extract_from_commit(text: str, *, source_event_id, occurred_at):
    """`(remaining, decisions)` for one commit message."""
    remaining = []
    decisions = []

    def _add(title, kind):
        cleaned = _clean(title)
        if cleaned:
            remaining.append({"title": cleaned, "detail": None, "kind": kind,
                              "source_event_id": source_event_id,
                              "occurred_at": occurred_at})

    for match in _REMAINING_TRAILER_RE.finditer(text):
        _add(match.group(1), "remaining")
    for match in _TODO_KEY_RE.finditer(text):
        _add(match.group(2), "remaining")
    for match in _TODO_BARE_RE.finditer(text):
        _add(match.group(1), "remaining")
    for match in _FIXME_RE.finditer(text):
        _add(match.group(1), "remaining")
    for match in _BLOCKED_RE.finditer(text):
        _add(match.group(1), "blocker")
    for match in _DECISION_RE.finditer(text):
        statement = _clean(match.group(1))
        if statement:
            decisions.append({"statement": statement, "rationale": None,
                              "source_event_id": source_event_id,
                              "occurred_at": occurred_at})
    return remaining, decisions


# --------------------------------------------------------------------------
# Ticket candidates
# --------------------------------------------------------------------------

class TicketCandidate:
    """A ticket key the session's evidence names, with its per-signal counts.

    `signals` holds only the *ticket* signals (DESIGN §6 rows 2-5); the
    repository signals are per-activity and are merged in by
    `split_activities`, because two activities in one session can resolve to
    different repositories.
    """

    __slots__ = ("key", "connection_id", "signals", "score")

    def __init__(self, key, connection_id, signals, score):
        self.key = key
        self.connection_id = connection_id
        self.signals = signals
        self.score = score

    def __repr__(self):  # pragma: no cover - debugging aid
        return (f"TicketCandidate(key={self.key!r}, connection_id={self.connection_id!r}, "
                f"signals={self.signals!r}, score={self.score!r})")

    def __eq__(self, other):
        return (isinstance(other, TicketCandidate)
                and (self.key, self.connection_id, self.signals, self.score)
                == (other.key, other.connection_id, other.signals, other.score))


def extract_ticket_candidates(conn, events, *, branch) -> list:
    """Every ticket key the branch and the session's events name, scored.

    Sorted by descending score, then key, so the first entry is the session's
    dominant candidate and the ordering is stable across runs.
    """
    patterns = ticket_patterns(conn)
    if not patterns:
        return []

    # Two connections configured with the same glob must not double-score a
    # single mention. Dedupe the compiled patterns, then dedupe the match
    # *spans* they produce, so overlapping-but-different globs ("AB-*" and
    # "S?-*") also count one occurrence once.
    distinct = {}
    for _connection_id, pattern in patterns:
        distinct.setdefault(pattern.pattern, pattern)
    scan_patterns = list(distinct.values())

    identities = _known_git_identities(conn)
    signals = {}

    def scan(text, signal_name):
        if not text:
            return
        seen = set()
        for pattern in scan_patterns:
            for match in pattern.finditer(text):
                span = (match.start(), match.end())
                if span in seen:
                    continue
                seen.add(span)
                key = match.group(0).upper()
                counts = signals.setdefault(key, {})
                counts[signal_name] = counts.get(signal_name, 0) + 1

    scan(branch, "ticket_in_branch")
    for row in events:
        event_type = row["event_type"]
        payload = _payload(row)
        if event_type in _COMMIT_EVENT_TYPES:
            # DESIGN §6 scores +30 for a ticket key in a commit message
            # "authored by a configured git identity". A commit whose author
            # is not a configured identity is weaker evidence than that --
            # anyone's commit can be in the working tree -- so it drops to
            # the +8 "present only in tool input/output" tier rather than
            # letting an unverified author reach `high` on its own.
            author = payload.get("author_email")
            attributed = (isinstance(author, str)
                          and author.strip().lower() in identities)
            scan(_commit_text(payload),
                 "ticket_in_commit_message" if attributed else "ticket_in_tool_io")
        elif event_type == "prompt":
            if _is_user_prompt(payload):
                scan(_prompt_text(payload), "ticket_in_prompt")
        elif event_type == "tool_use":
            scan(_tool_text(payload), "ticket_in_tool_io")

    candidates = [
        TicketCandidate(key, manual.matching_connection_id(conn, key), counts,
                        confidence.score(counts))
        for key, counts in signals.items()
    ]
    candidates.sort(key=lambda candidate: (-candidate.score, candidate.key))
    return candidates


def _competing_candidates(candidates):
    """DESIGN §6 override 1: the top candidate plus every other candidate from
    a *different* connection within `confidence.CONTRADICTORY_WITHIN_POINTS`,
    or None.

    Two keys claimed by the same connection are not an ambiguity -- one
    session legitimately touches two tickets in one Jira project, which is
    exactly what the per-work-item split exists for.
    """
    if len(candidates) < 2:
        return None
    top = candidates[0]
    rivals = [
        candidate for candidate in candidates[1:]
        if candidate.connection_id != top.connection_id
        and top.score - candidate.score <= confidence.CONTRADICTORY_WITHIN_POINTS
    ]
    if not rivals:
        return None
    return [(top.key, top.score)] + [(r.key, r.score) for r in rivals]


# --------------------------------------------------------------------------
# Scope resolution
# --------------------------------------------------------------------------

# `repository_mappings.source` values that mean "a human or setup persisted
# this", which is what DESIGN §6 scores at +40.
_PERSISTED_MAPPING_SOURCES = ("setup", "correction", "manual")


def _mapping_for_path(conn, path):
    """The `repository_mappings` row `entities.resolve_scope_for_path` would
    have used for `path`, or None.

    `resolve_scope_for_path` stays the authority for *which* client/project a
    path belongs to; this repeats its longest-root / longest-prefix rule only
    to recover the mapping's `source`, which the scoring table needs and the
    public function does not return.
    """
    rows = conn.execute(
        "SELECT m.id, m.path_prefix, m.source, m.confidence, r.root_path "
        "FROM repository_mappings m JOIN repositories r ON r.id = m.repository_id "
        "WHERE m.deleted_at IS NULL AND r.deleted_at IS NULL AND r.approved = 1"
    ).fetchall()

    best = None
    best_rank = (-1, -1)
    for row in rows:
        root = (row["root_path"] or "").rstrip("/")
        if not root or not (path == root or path.startswith(root + "/")):
            continue
        relative = path[len(root):].lstrip("/")
        prefix = row["path_prefix"] or ""
        if prefix and not (relative == prefix or relative.startswith(prefix + "/")):
            continue
        rank = (len(root), len(prefix))
        if rank > best_rank:
            best_rank = rank
            best = row
    return best


def _scope_for_path(conn, path, cache):
    """`(client_id, project_id, repo_signals, repo_name, path_prefix)` for a
    filesystem path, memoized for the life of one `split_activities` call.

    `repo_name` and `path_prefix` are the *stable* identity of the scope --
    they name the same thing session after session, which is what a local
    work item's title has to be built from.

    `repo_alias_persisted` (+30) is a real DESIGN §6 signal but Stage A never
    awards it: `repository_mappings.source` is only ever `setup`/`correction`/
    `manual`/an inference label, and a repository resolved through the
    `aliases` table is repository discovery's path (Task 33), not this one.
    The entry stays in `SIGNAL_POINTS` because Task 27 pins that table
    verbatim; the branch that could never fire is gone.
    """
    if path in cache:
        return cache[path]

    if not path:
        result = (None, None, {}, None, "")
        cache[path] = result
        return result

    client_id, project_id, _confidence, _reason = entities.resolve_scope_for_path(conn, path)
    repo_signals = {}
    repo_name = None
    path_prefix = ""
    if client_id is not None or project_id is not None:
        mapping = _mapping_for_path(conn, path)
        source = mapping["source"] if mapping is not None else None
        if source in _PERSISTED_MAPPING_SOURCES:
            repo_signals["repo_mapping_persisted"] = 1
        else:
            repo_signals["repo_name_similarity"] = 1
        if mapping is not None:
            root = (mapping["root_path"] or "").rstrip("/")
            repo_name = root.rsplit("/", 1)[-1] or root or None
            path_prefix = mapping["path_prefix"] or ""
            # A non-empty path_prefix is a *narrower* persisted claim than the
            # repository mapping itself; an empty prefix is the repository
            # mapping and must not be counted twice.
            if path_prefix:
                repo_signals["path_prefix_mapping"] = 1

    result = (client_id, project_id, repo_signals, repo_name, path_prefix)
    cache[path] = result
    return result


def _known_git_identities(conn) -> frozenset:
    rows = conn.execute(
        "SELECT value FROM user_identities WHERE kind='git' AND deleted_at IS NULL"
    ).fetchall()
    return frozenset((row["value"] or "").strip().lower() for row in rows)


# --------------------------------------------------------------------------
# Session splitting
# --------------------------------------------------------------------------

class ActivityDraft:
    """One activity's worth of counted facts, before it is written."""

    __slots__ = ("client_id", "project_id", "work_item_id", "kind", "summary",
                 "outcome", "started_at", "ended_at", "evidence", "remaining",
                 "decisions", "signals")

    def __init__(self, *, client_id, project_id, work_item_id, kind, summary, outcome,
                 started_at, ended_at, evidence, remaining, decisions, signals):
        self.client_id = client_id
        self.project_id = project_id
        self.work_item_id = work_item_id
        self.kind = kind
        self.summary = summary
        self.outcome = outcome
        self.started_at = started_at
        self.ended_at = ended_at
        self.evidence = evidence
        self.remaining = remaining
        self.decisions = decisions
        self.signals = signals

    def __repr__(self):  # pragma: no cover - debugging aid
        return (f"ActivityDraft(work_item_id={self.work_item_id!r}, kind={self.kind!r}, "
                f"summary={self.summary!r})")


class _Group:
    """Accumulator for one `(client_id, project_id, ticket_key)` bucket."""

    def __init__(self, client_id, project_id, ticket_key):
        self.client_id = client_id
        self.project_id = project_id
        self.ticket_key = ticket_key
        self.commits = []        # {sha, subject, files, author_email, event_id, occurred_at}
        self.commit_files = []   # ordered, de-duplicated
        self.edited_files = []   # ordered, de-duplicated
        # path -> the source_events.id that first evidenced it, so a file
        # evidence row points back at the event that touched the file.
        self.file_events = {}
        self.tests = []          # {runner, passed, detail, event_id, occurred_at}
        # Dual capture: the same Bash call arrives once from the PostToolUse
        # hook and once from transcript reconciliation. Keyed by tool_use_id
        # (falling back to the row id) so it is counted exactly once, and the
        # copy that carries a real exit code wins.
        self.test_keys = {}
        self.branch = None
        self.cwd = None
        self.started_at = None
        self.ended_at = None
        self.repo_signals = {}
        self.repo_name = None
        self.path_prefix = ""
        self.commit_by_known_identity = False
        self.remaining = []
        self.decisions = []

    def add_files(self, target: list, paths, event_id):
        for path in paths:
            if path not in target:
                target.append(path)
            self.file_events.setdefault(path, event_id)

    def add_test(self, key, result, *, authoritative, event_id, occurred_at):
        """Record one test run, collapsing the hook and transcript copies of
        the same call. A copy backed by an explicit exit code replaces one
        that was only inferred from output text."""
        entry = {**result, "event_id": event_id, "occurred_at": occurred_at,
                 "authoritative": authoritative}
        index = self.test_keys.get(key)
        if index is None:
            self.test_keys[key] = len(self.tests)
            self.tests.append(entry)
            return
        existing = self.tests[index]
        if authoritative and not existing["authoritative"]:
            self.tests[index] = entry

    def observe(self, row):
        occurred_at = row["occurred_at"]
        if self.started_at is None or occurred_at < self.started_at:
            self.started_at = occurred_at
        if self.ended_at is None or occurred_at > self.ended_at:
            self.ended_at = occurred_at
        if self.branch is None and row["git_branch"]:
            self.branch = row["git_branch"]
        if self.cwd is None and row["cwd"]:
            self.cwd = row["cwd"]

    @property
    def meaningful(self) -> bool:
        """SPEC §6.2: routine reads, searches and narration stay raw evidence.
        A group becomes an activity only when it changed something, committed
        something, or produced a test result."""
        return bool(self.commits or self.edited_files or self.tests)


def _append_unique(target: list, values):
    for value in values:
        if value not in target:
            target.append(value)


def _session_events(conn, session_id) -> list:
    return conn.execute(
        "SELECT * FROM source_events WHERE session_id=? ORDER BY occurred_at, id",
        (session_id,),
    ).fetchall()


def _candidate_key_matcher(keys):
    """A word-boundaried alternation over the session's candidate keys, or
    None when there are none.

    Word boundaries matter: a plain substring test would let the key `AB-7`
    claim an event that only ever mentions `AB-76`. Longest-first so the
    alternation prefers `AB-76` over `AB-7` at the same position.
    """
    if not keys:
        return None
    ordered = sorted(keys, key=lambda key: (-len(key), key))
    return re.compile(r"\b(?:" + "|".join(re.escape(key) for key in ordered) + r")\b",
                      re.IGNORECASE)


def _event_ticket_key(row, payload, candidate_by_key, matcher, dominant_key):
    """The ticket key this single event belongs to.

    An event that names keys itself is attributed to its own highest-scoring
    key; an event that names none inherits the session's dominant candidate,
    which is what keeps "edited a file, then committed AB-76" one activity.
    """
    if matcher is None:
        return dominant_key

    event_type = row["event_type"]
    if event_type in _COMMIT_EVENT_TYPES:
        text = _commit_text(payload)
    elif event_type == "prompt" and _is_user_prompt(payload):
        text = _prompt_text(payload)
    elif event_type == "tool_use":
        text = _tool_text(payload)
    else:
        text = ""

    named = {match.group(0).upper() for match in matcher.finditer(text)} if text else set()
    named &= set(candidate_by_key)
    if not named:
        return dominant_key
    return max(named, key=lambda key: (candidate_by_key[key].score, key))


def _common_directory(paths: list) -> str:
    """Longest shared directory prefix of `paths`, or "" when they share none."""
    directories = []
    for path in paths:
        head = path.rsplit("/", 1)[0] if "/" in path else ""
        directories.append(head)
    if not directories or any(d == "" for d in directories):
        return ""
    split = [d.split("/") for d in directories]
    shared = []
    for parts in zip(*split):
        first = parts[0]
        if all(part == first for part in parts):
            shared.append(first)
        else:
            break
    return "/".join(shared)


def _compose_summary(group, branch, fallback_prefix) -> str:
    clauses = []
    if group.commits:
        commit_count = len(group.commits)
        file_count = len(group.commit_files)
        clauses.append(COMMIT_TEMPLATE.format(
            n=commit_count, s=_plural(commit_count), branch=branch,
            m=file_count, fs=_plural(file_count)))
    elif group.edited_files:
        file_count = len(group.edited_files)
        prefix = _common_directory(group.edited_files) or fallback_prefix
        clauses.append(EDIT_TEMPLATE.format(
            m=file_count, fs=_plural(file_count), prefix=prefix))

    if group.tests:
        detail = _test_detail(group.tests)
        clauses.append(detail if clauses else TEST_TEMPLATE.format(detail=detail))

    return _CLAUSE_JOIN.join(clauses)


def _test_detail(tests) -> str:
    """One clause covering every test run in the group; a single failure makes
    the whole clause a failure, and the runners are listed once each."""
    failed = [t for t in tests if not t["passed"]]
    runners = []
    for test in (failed or tests):
        if test["runner"] not in runners:
            runners.append(test["runner"])
    verdict = "failed" if failed else "passed"
    return f"{', '.join(runners)} {verdict}"


def _outcome(group) -> "str | None":
    if group.tests:
        return "tests failing" if any(not t["passed"] for t in group.tests) else "tests passing"
    if group.commits:
        return "committed"
    return None


def _kind(group) -> str:
    if group.commits or group.edited_files:
        return "code"
    if group.tests:
        return "test"
    return "other"


def _evidence_for(group) -> list:
    evidence = []
    for commit in group.commits:
        evidence.append({
            "evidence_type": "commit",
            "ref": commit["sha"] or commit["subject"][:64],
            "excerpt": commit["subject"],
            "occurred_at": commit["occurred_at"],
            "source_event_id": commit["event_id"],
        })
    files = list(group.commit_files)
    _append_unique(files, group.edited_files)
    for path in files[:_MAX_FILE_EVIDENCE]:
        evidence.append({
            "evidence_type": "file",
            "ref": path,
            "excerpt": None,
            "occurred_at": group.started_at,
            "source_event_id": group.file_events.get(path),
        })
    for test in group.tests:
        evidence.append({
            "evidence_type": "test",
            "ref": test["runner"],
            "excerpt": test["detail"],
            "occurred_at": test["occurred_at"],
            "source_event_id": test["event_id"],
        })
    return evidence


def _local_work_item_title(group, branch) -> str:
    """A *stable* title for an unticketed local work item.

    SPEC §10 forbids one local item per session, and `find_reusable_local`
    only reuses at >= 0.70 Jaccard similarity -- so the title must name the
    scope, not the session. A counted summary ("Edited 3 files under src; no
    commits") drifts with every session and produced LOCAL-0001, LOCAL-0002,
    ... for the same work. Repository name (plus the mapped path prefix) and
    branch do not drift, so the same ongoing work lands on the same item.
    """
    scope = group.repo_name
    if not scope:
        scope = (group.cwd or "").rstrip("/").rsplit("/", 1)[-1]
    if not scope:
        scope = "work"
    if group.path_prefix:
        scope = f"{scope}/{group.path_prefix}"
    if branch and branch != _NO_BRANCH:
        return f"{scope}: {branch}"
    return f"{scope}: ongoing work"


def _resolve_work_item(conn, group, ticket_key, candidate, title, *, hook_mode=False):
    """The work item this activity hangs off.

    A ticket key becomes a Jira-backed item through `upsert_external`, which
    adopts an existing orphan row for the same key (Ruling 18). Unticketed
    work reuses or creates a local item, but only when the repository
    resolved to a client or project -- an unattributable session must not
    manufacture a local item (SPEC §10: "avoid one local item per session"),
    it belongs in the inference inbox with a null work item.
    """
    if ticket_key is not None:
        connection_id = candidate.connection_id if candidate is not None else None
        return work_items.upsert_external(
            conn, connection_id, ticket_key, title=ticket_key,
            client_id=group.client_id, project_id=group.project_id,
            hook_mode=hook_mode)

    if group.client_id is None and group.project_id is None:
        return None

    work_item_id, _created = work_items.get_or_create_local(
        conn, title, client_id=group.client_id, project_id=group.project_id,
        hook_mode=hook_mode)
    return work_item_id


def split_activities(conn, session_id: str, *, hook_mode: bool = False) -> list:
    """One `ActivityDraft` per `(client_id, project_id, work_item_id)` tuple the
    session touched (SPEC §4, §20.1).

    Resolves (and, where needed, creates) work items, so it must be called
    *outside* an open transaction -- see the module docstring. `hook_mode`
    threads to those work-item writes.
    """
    session_row = sessions.get(conn, session_id)
    if session_row is None:
        return []

    events = _session_events(conn, session_id)
    if not events:
        return []

    session_branch = session_row["git_branch"]
    session_cwd = session_row["cwd"]

    candidates = extract_ticket_candidates(conn, events, branch=session_branch)
    candidate_by_key = {candidate.key: candidate for candidate in candidates}
    dominant_key = candidates[0].key if candidates else None

    identities = _known_git_identities(conn)
    matcher = _candidate_key_matcher(candidate_by_key)
    scope_cache = {}
    groups = {}

    # Subjects of the *reconciled* commits (event_type='commit'), which carry
    # the sha and the file list. Collected up front rather than as we go: the
    # hook's `git_commit_signal` row is stamped at hook time while the
    # reconciled row carries the commit's authored time, so the two do not
    # arrive in a guaranteed order and a running set would sometimes miss the
    # duplicate.
    commit_subjects = set()
    for row in events:
        if row["event_type"] == "commit":
            subject = _payload(row).get("subject")
            if isinstance(subject, str) and subject:
                commit_subjects.add(subject)

    for row in events:
        event_type = row["event_type"]
        if event_type not in _COMMIT_EVENT_TYPES and event_type != "tool_use":
            # session_start/end, compact, subagent_stop and prompts carry no
            # countable work of their own; they still feed the ticket scan
            # above, which is where their evidence value lives.
            continue

        payload = _payload(row)
        path = row["cwd"] or session_cwd or ""
        (client_id, project_id, repo_signals,
         repo_name, path_prefix) = _scope_for_path(conn, path, scope_cache)
        ticket_key = _event_ticket_key(row, payload, candidate_by_key, matcher, dominant_key)

        bucket_key = (client_id, project_id, ticket_key)
        group = groups.get(bucket_key)
        if group is None:
            group = groups[bucket_key] = _Group(client_id, project_id, ticket_key)
        if not group.repo_signals:
            group.repo_signals = repo_signals
            group.repo_name = repo_name
            group.path_prefix = path_prefix

        if event_type in _COMMIT_EVENT_TYPES:
            subject = payload.get("subject")
            subject = subject if isinstance(subject, str) else ""
            if event_type == "git_commit_signal":
                exit_code = payload.get("exit_code")
                if exit_code not in (None, 0):
                    continue
                # `git push`/`merge`/`tag` also raise this signal; only the
                # ones that actually created a commit are commits.
                if not _creates_a_commit(payload.get("command"), subject):
                    continue
                # The reconciled `commit` row for the same commit is richer
                # (sha + files); never count both.
                if subject and subject in commit_subjects:
                    continue

            group.observe(row)
            files = [f for f in (payload.get("files") or []) if isinstance(f, str)]
            author_email = payload.get("author_email")
            group.commits.append({
                "sha": payload.get("sha") or payload.get("short_sha"),
                "subject": _clean(subject),
                "files": files,
                "author_email": author_email,
                "event_id": row["id"],
                "occurred_at": row["occurred_at"],
            })
            group.add_files(group.commit_files, files, row["id"])
            if isinstance(author_email, str) and author_email.strip().lower() in identities:
                group.commit_by_known_identity = True

            remaining, decisions = _extract_from_commit(
                _commit_text(payload), source_event_id=row["id"],
                occurred_at=row["occurred_at"])
            group.remaining.extend(remaining)
            group.decisions.extend(decisions)
            continue

        # tool_use
        touched = False
        for index, (tool_use_id, tool_name, tool_input, tool_response) in enumerate(
                _tool_calls(row, payload)):
            paths = _edited_paths(tool_name, tool_input)
            if paths:
                group.add_files(group.edited_files, paths, row["id"])
                touched = True
            if tool_name == "Bash":
                result = parse_test_result(tool_input.get("command"), tool_response)
                if result is not None:
                    group.add_test(
                        tool_use_id or f"{row['id']}:{index}", result,
                        authoritative=isinstance(tool_response, dict)
                        and tool_response.get("exit_code") is not None,
                        event_id=row["id"], occurred_at=row["occurred_at"])
                    touched = True
        if touched:
            group.observe(row)

    drafts = []
    for group in groups.values():
        if not group.meaningful:
            continue

        branch = group.branch or session_branch or _NO_BRANCH
        fallback_prefix = group.cwd or session_cwd or branch
        summary = _compose_summary(group, branch, fallback_prefix)
        if not summary:
            continue

        candidate = candidate_by_key.get(group.ticket_key)
        work_item_id = _resolve_work_item(
            conn, group, group.ticket_key, candidate,
            _local_work_item_title(group, branch), hook_mode=hook_mode)

        signals = dict(group.repo_signals)
        if candidate is not None:
            for name, count in candidate.signals.items():
                signals[name] = signals.get(name, 0) + count
        if group.commit_by_known_identity:
            signals["commit_by_known_identity"] = 1

        drafts.append(ActivityDraft(
            client_id=group.client_id,
            project_id=group.project_id,
            work_item_id=work_item_id,
            kind=_kind(group),
            summary=summary,
            outcome=_outcome(group),
            started_at=group.started_at,
            ended_at=group.ended_at,
            evidence=_evidence_for(group),
            remaining=group.remaining,
            decisions=group.decisions,
            signals=signals,
        ))

    # Stable order: earliest work first, then work item, so two runs write the
    # same rows in the same order.
    drafts.sort(key=lambda draft: (draft.started_at or "", draft.work_item_id or ""))
    return drafts


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

def _draft_ticket_key(conn, draft) -> "str | None":
    """The ticket key a draft is attributed to, read back from its work item.

    `ActivityDraft` carries exactly the twelve fields the interface specifies,
    so the key is recovered here rather than smuggled onto the dataclass. One
    indexed primary-key lookup per draft.
    """
    if draft.work_item_id is None:
        return None
    row = conn.execute(
        "SELECT external_key FROM work_items WHERE id=?", (draft.work_item_id,)
    ).fetchone()
    if row is None or not row["external_key"]:
        return None
    return row["external_key"].upper()


def _idempotency_key(session_id, draft) -> str:
    return hashlib.sha256(
        f"stage_a|{session_id}|{draft.client_id}|{draft.project_id}|{draft.work_item_id}".encode()
    ).hexdigest()


def _source_hash(draft) -> str:
    """Content hash of everything a re-run could legitimately change, so an
    unchanged re-run rewrites nothing."""
    material = {
        "kind": draft.kind,
        "summary": draft.summary,
        "outcome": draft.outcome,
        "started_at": draft.started_at,
        "ended_at": draft.ended_at,
        "signals": sorted(draft.signals.items()),
        "evidence": sorted((e["evidence_type"], e["ref"] or "") for e in draft.evidence),
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_evidence(conn, activity_id, draft, device_id, now):
    """Insert the evidence rows this activity does not already have.

    `activity_evidence` is treated as append-only here: a re-run adds rows for
    newly-seen evidence and never deletes or rewrites what is already linked.
    """
    for item in draft.evidence:
        existing = conn.execute(
            "SELECT id FROM activity_evidence WHERE activity_id=? AND evidence_type=? "
            "AND IFNULL(ref,'')=IFNULL(?,'')",
            (activity_id, item["evidence_type"], item["ref"]),
        ).fetchone()
        if existing is not None:
            continue
        excerpt = item["excerpt"]
        conn.execute(
            "INSERT INTO activity_evidence(id, activity_id, source_event_id, evidence_type, "
            "ref, excerpt, occurred_at, origin_device_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (ids.new_id(), activity_id, item["source_event_id"], item["evidence_type"],
             item["ref"], excerpt[:_MAX_EXCERPT_CHARS] if excerpt else None,
             item["occurred_at"], device_id, now),
        )


def run(conn, session_id: str, *, hook_mode: bool = False) -> dict:
    """Interpret one session deterministically.

    Returns `{"activities": [ids], "created": n, "updated": n, "inbox": n}`.
    Idempotent: the activity key is
    `sha256("stage_a|<session>|<client>|<project>|<work_item>")`, so a re-run
    over the same evidence updates the same rows and a re-run over unchanged
    evidence writes nothing at all (`created` and `updated` both 0).

    `hook_mode=True` (what `sessions.run_stage_a` passes from the Stop hook)
    threads to every write below -- `entities.current_device`, the work-item
    writes inside `split_activities`, and this function's own transaction --
    so Stage A gives up on lock contention inside the hook budget.
    """
    result = {"activities": [], "created": 0, "updated": 0, "inbox": 0}

    session_row = sessions.get(conn, session_id)
    if session_row is None:
        return result

    drafts = split_activities(conn, session_id, hook_mode=hook_mode)
    if not drafts:
        return result

    events = _session_events(conn, session_id)
    candidates = extract_ticket_candidates(conn, events, branch=session_row["git_branch"])
    competing = _competing_candidates(candidates)
    competing_keys = {key for key, _points in competing} if competing else set()

    # Opens (and commits) its own transaction: must run before ours.
    device_id = entities.current_device(conn, hook_mode=hook_mode)
    now = timeutil.utc_now_iso()

    with connection.transaction(conn, hook_mode=hook_mode):
        for draft in drafts:
            # DESIGN §6 override 1 marks the *ambiguous* attributions, not
            # every activity in a session that happens to contain one. Only a
            # draft whose own key is in the competing set is contradictory.
            draft_competing = (competing
                               if _draft_ticket_key(conn, draft) in competing_keys
                               else None)
            resolved, value, reason = confidence.classify(
                draft.signals,
                competing=draft_competing,
                # Stage A runs before Stage B by construction (DESIGN §4.3);
                # Task 29 owns re-running attribution once Stage B has spoken.
                stage_b_ran=False,
            )
            # Ruling 6's narrative cap, expressed where it belongs: Stage A
            # never writes model-authored prose or a `source='stage_b'` row,
            # so `draft.summary` (built purely from the Task 26 templates)
            # and `resolved` (which can still be `high`, above) both stand.
            narrative = confidence.narrative_policy(stage_b_ran=False)
            assert narrative == {"summary": "templated", "allow_stage_b_fields": False}
            idempotency_key = _idempotency_key(session_id, draft)
            source_hash = _source_hash(draft)

            existing = conn.execute(
                "SELECT id, source_hash, source FROM activities WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()

            if existing is None:
                activity_id = ids.new_id()
                conn.execute(
                    "INSERT INTO activities(id, session_row_id, client_id, project_id, "
                    "work_item_id, kind, summary, outcome, started_at, ended_at, confidence, "
                    "confidence_score, confidence_reason, source, in_inbox, idempotency_key, "
                    "source_hash, origin_device_id, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (activity_id, session_row["id"], draft.client_id, draft.project_id,
                     draft.work_item_id, draft.kind, draft.summary, draft.outcome,
                     draft.started_at, draft.ended_at, resolved, value, reason, "stage_a",
                     confidence.in_inbox(resolved), idempotency_key, source_hash, device_id, now, now),
                )
                result["created"] += 1
            else:
                activity_id = existing["id"]
                # A user correction owns the row from then on: Stage A may add
                # evidence to it but never rewrites its summary or attribution.
                #
                # Note: `corrections.move_activity` re-attributes an activity
                # *without* changing `source`, so this guard does not engage
                # for a move -- but a move also changes the client/project/
                # work_item the idempotency key is built from, so the moved
                # row is no longer the row this key finds. Either way the
                # correction's own columns are never rewritten here. See the
                # "Concerns" section of the task report for the re-creation
                # follow-up that leaves.
                if existing["source"] == "stage_a" and existing["source_hash"] != source_hash:
                    conn.execute(
                        "UPDATE activities SET kind=?, summary=?, outcome=?, started_at=?, "
                        "ended_at=?, confidence=?, confidence_score=?, confidence_reason=?, "
                        "in_inbox=?, source_hash=?, updated_at=? WHERE id=?",
                        (draft.kind, draft.summary, draft.outcome, draft.started_at,
                         draft.ended_at, resolved, value, reason, confidence.in_inbox(resolved),
                         source_hash, now, activity_id),
                    )
                    result["updated"] += 1

            _write_evidence(conn, activity_id, draft, device_id, now)
            result["activities"].append(activity_id)
            if resolved == "low":
                result["inbox"] += 1

    # New evidence landed: refresh the AI-usage score of every work item this
    # run touched. Imported here (not at module top) because ai_usage imports
    # this module for `_is_user_prompt`/`_creates_a_commit`.
    from work_ledger.infer import ai_usage as _ai_usage
    touched = set()
    for activity_id in result["activities"]:
        row = conn.execute(
            "SELECT work_item_id FROM activities WHERE id=? AND work_item_id IS NOT NULL",
            (activity_id,)).fetchone()
        if row is not None:
            touched.add(row["work_item_id"])
    for work_item_id in sorted(touched):
        _ai_usage.recompute_for_work_item(conn, work_item_id, hook_mode=hook_mode)

    return result
