"""Source-event ingestion: idempotent, redacted, size-capped append-only
writes to `source_events`, plus the PostToolUse noise filter (Spec
§6.1/§6.2, Ruling 24).

`append_source_event` is a required redaction choke point (Ruling 15):
every payload passes through `redact.redact_obj` first, unconditionally --
this is not an optimization or an opt-in, it is the one place
credential-shaped text in a hook payload is guaranteed to be scrubbed
before it reaches SQLite.
"""
import hashlib
import json
import re

from work_ledger import config, entities, ids, redact, timeutil
from work_ledger.db import connection

_TRUNCATION_SUFFIX = "…[truncated]"

# Ruling 30(g) / DESIGN §4.2: a Bash command matching this causes the
# PostToolUse hook to append a second, derived source_events row with
# event_type='git_commit_signal'. Also reused by is_interesting below --
# it is exactly the "git (commit|merge|push|rebase|cherry-pick|revert|tag)"
# keep-shape from Spec §6.1/§6.2.
GIT_COMMIT_RE = re.compile(
    r"^\s*git\s+(?:-C\s+\S+\s+)?(commit|merge|push|rebase|cherry-pick|revert|tag)\b"
)

# Test-runner / build bash shapes -- verbatim from the task brief.
_TEST_RUNNER_RE = re.compile(
    r"pytest|unittest|go test|npm (run )?test|yarn test|gradle.*test|"
    r"xcodebuild.*test|cargo test|rspec|mvn test"
)
_BUILD_RE = re.compile(r"make|go build|npm run build|gradle build|xcodebuild")
# Not given verbatim by the brief (unlike the two patterns above): a
# reasonable "migration/deploy verb" shape, kept narrow (word-boundaried) to
# avoid false-positiving on ordinary prose inside an unrelated command.
_MIGRATION_DEPLOY_RE = re.compile(r"\b(migrate|migration|deploy|rollout)\b", re.IGNORECASE)

# Ruling 24: this Bash command-shape drop rule is separate from the
# tool-name drop list (capture.noise_tools) and stays in code -- a
# read-only/inspection command that exited 0 is noise regardless of config.
# Checked only AFTER the keep-shapes above (review finding, ruled minor 7)
# so a compound command like "cat x && npm test" is kept rather than
# dropped just because it happens to start with "cat".
_BASH_DROP_RE = re.compile(
    r"^\s*(git (status|log|diff|branch|rev-parse|show)|ls|cat|pwd|which|echo|head|tail|wc|find)\b"
)

# Tools that are always interesting, unconditionally. This intentionally
# takes precedence over capture.noise_tools -- even a config that mistakenly
# lists one of these names here can never silently drop a real edit, an MCP
# call, or a subagent's own work.
_ALWAYS_KEEP_TOOLS = frozenset({"Write", "Edit", "NotebookEdit", "MultiEdit", "Task"})


def idempotency_key(session_id: str, kind: str, discriminator: str) -> str:
    return hashlib.sha256(f"{session_id}|{kind}|{discriminator}".encode("utf-8")).hexdigest()


def is_interesting(tool_name: str, tool_input: dict, tool_response) -> "tuple[bool, str]":
    """Spec §6.1/§6.2 noise filter for PostToolUse. Returns (keep, reason).

    Ruling 24: the tool-name drop list is read fresh from
    `config.get("capture.noise_tools")` on every call -- this function
    holds no hard-coded tool list of its own, never unions a second
    hard-coded list into it, and treats any tool absent from that list as
    interesting by default (a narrowed noise_tools widens capture).
    """
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    tool_response = tool_response if isinstance(tool_response, dict) else {}

    if tool_name in _ALWAYS_KEEP_TOOLS or (isinstance(tool_name, str) and tool_name.startswith("mcp__")):
        return True, "always-keep tool"

    noise_tools = config.get("capture.noise_tools") or []
    if tool_name in noise_tools:
        return False, f"{tool_name} is in capture.noise_tools"

    if tool_name == "Bash":
        command = tool_input.get("command") or ""
        exit_code = tool_response.get("exit_code")
        stderr = tool_response.get("stderr") or ""

        # Keep-shapes are evaluated BEFORE the read-only drop shape (ruled
        # minor 7): a compound command such as "cat x && npm test" must be
        # kept for the test-runner match even though it starts with "cat".
        if (GIT_COMMIT_RE.match(command) or _TEST_RUNNER_RE.search(command)
                or _BUILD_RE.search(command) or _MIGRATION_DEPLOY_RE.search(command)):
            return True, "matches an interesting bash command shape"

        if _BASH_DROP_RE.match(command) and exit_code == 0:
            return False, "read-only/inspection command exited 0"

        if exit_code not in (None, 0) and stderr:
            return True, "failing command revealed stderr"

        return False, "bash command does not match any interesting shape"

    return True, f"{tool_name} is not in capture.noise_tools"


def _truncate_strings(node, max_chars: int):
    if isinstance(node, dict):
        return {key: _truncate_strings(value, max_chars) for key, value in node.items()}
    if isinstance(node, list):
        return [_truncate_strings(value, max_chars) for value in node]
    if isinstance(node, str) and len(node) > max_chars:
        return node[:max_chars] + _TRUNCATION_SUFFIX
    return node


def _canonical_bytes(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _iter_leaves(node):
    """Yield (parent_container, key_or_index, value) for every non-container leaf."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                yield from _iter_leaves(value)
            else:
                yield node, key, value
    elif isinstance(node, list):
        for index, value in enumerate(node):
            if isinstance(value, (dict, list)):
                yield from _iter_leaves(value)
            else:
                yield node, index, value


_REMOVE = object()  # sentinel: list items are marked, not deleted in place,
                     # so earlier-computed indices in the same pass never go stale


def _prune(node) -> None:
    """Single pass: drop _REMOVE-marked list items and any dict/list value
    that became empty, recursively. Mutates in place."""
    if isinstance(node, dict):
        for key in list(node.keys()):
            value = node[key]
            if isinstance(value, (dict, list)):
                _prune(value)
                if not value:
                    del node[key]
    elif isinstance(node, list):
        i = 0
        while i < len(node):
            value = node[i]
            if value is _REMOVE:
                del node[i]
                continue
            if isinstance(value, (dict, list)):
                _prune(value)
                if not value:
                    del node[i]
                    continue
            i += 1


def _cap_payload_bytes(payload: dict, max_bytes: int) -> dict:
    """Cap the canonical encoding of `payload` at `max_bytes`.

    Sizes every leaf exactly once, then deletes largest-first in a single
    descending pass (O(n log n), not the previous O(n^2) re-encode-the-
    whole-payload-per-deletion). Dict keys are deleted directly; list items
    are marked with a sentinel and filtered out afterward in one pass so
    earlier-computed indices never go stale mid-loop. Emptied containers
    are pruned. If the result is still over budget (e.g. container/key
    overhead alone exceeds max_bytes), falls back to a small stub payload
    rather than looping or returning an over-budget result.
    """
    encoded = _canonical_bytes(payload)
    if len(encoded) <= max_bytes:
        return payload
    original_bytes = len(encoded)

    leaves = []
    for parent, key, value in _iter_leaves(payload):
        cost = len(_canonical_bytes(value)) + (len(json.dumps(key)) + 1 if isinstance(parent, dict) else 1)
        leaves.append((cost, parent, key))
    leaves.sort(key=lambda item: item[0], reverse=True)

    remaining = len(encoded)
    for cost, parent, key in leaves:
        if remaining <= max_bytes:
            break
        if isinstance(parent, dict):
            if key in parent:
                del parent[key]
                remaining -= cost
        else:  # list: mark, don't delete yet (keeps every other index valid)
            if 0 <= key < len(parent) and parent[key] is not _REMOVE:
                parent[key] = _REMOVE
                remaining -= cost

    _prune(payload)

    final = _canonical_bytes(payload)
    if len(final) <= max_bytes:
        return payload

    return {"truncated": True, "original_bytes": original_bytes, "max_bytes": max_bytes}


def append_source_event(conn, *, session_id, event_type, occurred_at, payload: dict,
                         hook_event=None, tool_name=None, tool_use_id=None, cwd=None,
                         git_branch=None, repository_id=None, is_sidechain=False,
                         idem: str, hook_mode: bool = False) -> "str | None":
    """Redact, truncate, and byte-cap `payload`, then `INSERT OR IGNORE` into
    `source_events`. Returns the new row id, or None when `idem` already
    existed (a replayed hook payload).

    Order (Ruling 15/29 -- redaction is a required choke point, not an
    exception):
      1. redact.redact_obj(payload)
      2. truncate every string leaf to capture.excerpt_max_chars
      3. cap the whole payload at capture.payload_max_bytes by dropping leaves
      4. source_hash = sha256(canonical json)
      5. INSERT OR IGNORE

    `hook_mode=True` (passed by hooks_runtime, which always opens its
    connection with `connection.connect(hook_mode=True)`) threads through to
    `entities.current_device` and `connection.transaction` so a hook write
    gives up on lock contention within the hook budget rather than burning
    the full non-hook retry schedule.

    `session_row_id` (Minor #8, final review) is populated from a single
    indexed lookup against `sessions.session_id` (`uq_sessions_session_id`)
    when a matching row already exists, else left NULL -- every real caller
    upserts the `sessions` row for `session_id` before its first
    `append_source_event` call, so this resolves in practice; nothing here
    creates a `sessions` row (that stays `capture.sessions.upsert`'s job).
    """
    redacted, redaction_names = redact.redact_obj(payload)

    excerpt_max_chars = config.get("capture.excerpt_max_chars", 512)
    truncated = _truncate_strings(redacted, excerpt_max_chars)

    payload_max_bytes = config.get("capture.payload_max_bytes", 8192)
    capped = _cap_payload_bytes(truncated, payload_max_bytes)

    encoded = _canonical_bytes(capped)
    source_hash = hashlib.sha256(encoded).hexdigest()

    # current_device() opens (and commits) its own transaction, so it must
    # run before this function opens its own -- connection.transaction is
    # not reentrant (see entities.py's module docstring).
    device_id = entities.current_device(conn, hook_mode=hook_mode)
    now = timeutil.utc_now_iso()
    row_id = ids.new_id()

    session_row = conn.execute(
        "SELECT id FROM sessions WHERE session_id=?", (session_id,)
    ).fetchone()
    session_row_id = session_row["id"] if session_row is not None else None

    with connection.transaction(conn, hook_mode=hook_mode):
        cur = conn.execute(
            "INSERT OR IGNORE INTO source_events("
            "id, session_row_id, session_id, idempotency_key, event_type, hook_event, tool_name, "
            "tool_use_id, repository_id, git_branch, cwd, occurred_at, ingested_at, payload_json, "
            "payload_bytes, redaction_json, source_hash, is_sidechain, origin_device_id, created_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row_id, session_row_id, session_id, idem, event_type, hook_event, tool_name, tool_use_id,
             repository_id, git_branch, cwd, occurred_at, now, encoded.decode("utf-8"), len(encoded),
             json.dumps(redaction_names), source_hash, int(bool(is_sidechain)), device_id, now),
        )
        if cur.rowcount == 0:
            return None
        return row_id
