"""Model-mediated Jira MCP adapter (Spec §11.2, §12.3; DESIGN §10.3, gap G2).

**Nothing in this module ever calls an MCP tool.** A hook or a plain Python
process has no channel to the MCP servers Claude Code has connected -- that
is DESIGN gap G2 -- so `JiraMcpAdapter` is a *planner and ingester*, not a
client:

1. **Setup** (`discover_servers` / `jira_candidates` / `map_capabilities`)
   reads the *names* of configured servers out of `~/.claude.json`, the
   plugin tree, and `claude mcp list`, decides which ones look like Jira,
   and maps the concrete `mcp__<server>__<tool>` names onto work-ledger's
   capability vocabulary. `connections.record_capabilities` persists that.
2. **Query time** the skill runs `worklog jira plan-refresh --format json`
   (`plan_refresh` here), which answers *which issue keys are stale and
   which tool the model should call for them*. The model calls the tools.
3. The model pipes the raw tool results back in through
   `worklog jira ingest --connection <name>` (`ingest` here), which
   validates, redacts, and writes.

`identify_current_user`/`get_issue`/`search_issues` therefore raise
`ModelMediationRequired` naming the tool the model must call -- they are
never callable from a hook or a script.

Two safety rules govern this module:

* **MCP credentials are never read or copied** (Spec §12.3.7, §20.14).
  Discovery copies a server's *name* and, when the source volunteers them,
  its *tool names* -- never `env`, `command`, `args`, `headers`, `url`, or
  any other field of an MCP server entry. `_server_entry` is the single
  place a discovered server becomes a dict, and it builds that dict from a
  fixed field list rather than by copying the source entry.
* **`ingest` is a required redaction sink** (Ruling 15, DESIGN §12). Its
  input is model-relayed, hence untrusted (threat model B6): every string
  leaf passes through `redact.redact` before anything is normalized,
  compared, or written, every issue key must match `_ISSUE_KEY_RE` before
  it reaches a query, every write is parameterized, and nothing from the
  payload is executed, evaluated, or interpolated into SQL.

Note that `redact.redact_obj` is deliberately *not* used here even though
it is the obvious recursive helper: it blanks any value whose *key* looks
credential-shaped (`.*key.*` included), which would replace a Jira issue's
`"key": "AB-76"` and `"project": {"key": "SK"}` with `[REDACTED:key]`.
`_redact_strings` applies `redact.redact` -- the same pattern set, the same
sole owner of credential knowledge (Ruling 22) -- to string leaves only.
"""
import contextlib
import fnmatch
import json
import os
import re
import shutil
import sqlite3
import subprocess
from datetime import timedelta, timezone
from pathlib import Path

from work_ledger import config, connections, contradictions, redact, timeutil, work_items
from work_ledger.db import connection as dbc
from work_ledger.jira.adapter import (
    CAPABILITY_VALUES,
    Capability,
    HealthResult,
    IdentityResult,
    IssueResult,
    JiraAdapter,
)

__all__ = [
    "CAPABILITY_TOOL_HINTS",
    "ModelMediationRequired",
    "JiraMcpAdapter",
    "discover_servers",
    "jira_candidates",
    "map_capabilities",
    "tool_for_capability",
    "default_refresh_limit",
    "plan_refresh",
    "ingest",
    "jira_work_items",
]


class ModelMediationRequired(Exception):
    """A read this adapter cannot perform itself: only the model, inside a
    Claude Code turn, can call an `mcp__<server>__*` tool (DESIGN gap G2).
    The message always names the tool to call and the `worklog jira ingest`
    command that consumes its result."""


# Keyed by the capability's plain string value (Ruling 27) -- never by the
# `Capability` member -- because these keys cross module and storage
# boundaries (`connection_capabilities.capability`, `map_capabilities`,
# `JiraAdapter.capabilities()`). Hint order is preference order: the first
# hint that matches an available tool wins, so the Atlassian connector's
# camelCase names are preferred over the generic snake_case fallbacks.
CAPABILITY_TOOL_HINTS = {
    "get_issue":             ["getJiraIssue", "jira_get_issue", "get_issue"],
    "identify_current_user": ["atlassianUserInfo", "jira_myself"],
    "search_issues":         ["searchJiraIssuesUsingJql", "jira_search"],
    "list_projects":         ["getVisibleJiraProjects", "jira_get_projects"],
    "list_sites":            ["getAccessibleAtlassianResources"],
    "get_issue_history":     ["getJiraIssueRemoteIssueLinks"],
    "get_user_activity":     [],
}

# The hint table must cover the whole vocabulary and invent nothing: a
# capability added to `jira.adapter.Capability` without a hint here (or a
# typo'd key) fails at import time rather than silently probing as
# unsupported forever.
assert set(CAPABILITY_TOOL_HINTS) == CAPABILITY_VALUES, (
    "CAPABILITY_TOOL_HINTS must be keyed by exactly jira.adapter.CAPABILITY_VALUES"
)

# A server whose name -- or one of whose tools -- mentions Jira or
# Atlassian is a candidate for `jira_candidates`.
_JIRA_RE = re.compile(r"jira|atlassian", re.IGNORECASE)

# MCP server names become part of a tool name (`mcp__<server>__<tool>`) and
# are printed back to the model, so anything outside this shape is dropped
# rather than propagated.
_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
# Anchored over the WHOLE tool name, not just its last `__` segment, so a
# qualified `mcp__<server>__<tool>` is validated end to end (underscores are
# in the class, so both the bare and the qualified form match) and nothing
# with a stray character in its server segment is carried forward.
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

# `claude mcp list` prints one server per line as "<name>: <target> - <status>".
_MCP_LIST_LINE_RE = re.compile(r"^\s*(?:[*\-•]\s*)?([A-Za-z0-9][A-Za-z0-9_.-]*)\s*:")

# Untrusted stdin: an issue key is matched against this before it reaches a
# parameter list, and never used to build SQL either way.
_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")

# Bounds on what a single ingest may write. The payload is model-relayed and
# therefore untrusted as to size as well as content: EVERY string it
# contributes to `work_items` -- the title and all four `jira_*` text
# columns, each of which is rendered in reports -- is truncated to this
# many characters. `_capped` is the single place that happens.
_MAX_FIELD_CHARS = 512
_CLI_TIMEOUT_SECONDS = 2


# --------------------------------------------------------------------------
# Discovery (setup time) -- names only, never credentials
# --------------------------------------------------------------------------

def _server_entry(name: str, source: str, tools: "list[str]") -> dict:
    """The ONLY place a discovered server becomes a dict. Built from a fixed
    field list rather than by copying the source entry, so no `env`,
    `command`, `args`, `headers`, or `url` field of an MCP server
    configuration can ever be carried along (Spec §12.3.7)."""
    return {"name": name, "source": source, "tools": sorted(set(tools))}


def _tool_names_from(entry) -> "list[str]":
    """Tool names a configuration entry volunteers, if any. Only a `tools`
    list of well-formed names is read; every other field is ignored."""
    if not isinstance(entry, dict):
        return []
    raw = entry.get("tools")
    if not isinstance(raw, list):
        return []
    names = []
    for item in raw:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"])
    return [n for n in names if _TOOL_NAME_RE.match(n)]


def _read_json(path: Path):
    """Parse a JSON file, or return None. A missing, unreadable, or corrupt
    file is never an error: discovery degrades to "found nothing there"."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _servers_from_mapping(mapping, source: str) -> "list[tuple[str, str, list[str]]]":
    if not isinstance(mapping, dict):
        return []
    found = []
    for name, entry in mapping.items():
        if isinstance(name, str) and _SERVER_NAME_RE.match(name):
            found.append((name, source, _tool_names_from(entry)))
    return found


def _plugin_servers(home: Path) -> "list[tuple[str, str, list[str]]]":
    """Servers declared by installed plugins.

    A plugin declares MCP servers either in a `.mcp.json` at its root or
    under an `mcpServers` key in its `.claude-plugin/plugin.json`. The scan
    is bounded (two levels of `<home>/.claude/plugins/repos`) and every
    read is guarded -- a plugin tree that does not exist, or holds
    unparseable JSON, simply contributes nothing.
    """
    root = home / ".claude" / "plugins" / "repos"
    found = []
    try:
        owners = sorted(root.iterdir())
    except OSError:
        return found
    for owner in owners:
        try:
            plugins = sorted(owner.iterdir())
        except OSError:
            continue
        for plugin_dir in plugins:
            for relative in (".mcp.json", ".claude-plugin/plugin.json", "plugin.json"):
                data = _read_json(plugin_dir / relative)
                if isinstance(data, dict):
                    found.extend(_servers_from_mapping(data.get("mcpServers"), "plugin"))
    return found


def _cli_servers() -> "list[tuple[str, str, list[str]]]":
    """Servers enumerated by `claude mcp list`.

    argv list (subprocess shell never enabled), a 2 s timeout, and every failure mode --
    no `claude` on PATH, a non-zero exit, a timeout, an OSError -- ignored:
    discovery must never be the reason setup fails. `WORK_LEDGER_NESTED=1`
    is exported so a `claude` that itself triggers work-ledger hooks does
    not recurse.
    """
    executable = shutil.which("claude")
    if not executable:
        return []
    env = dict(os.environ, WORK_LEDGER_NESTED="1")
    try:
        completed = subprocess.run(
            [executable, "mcp", "list"],
            capture_output=True, text=True, timeout=_CLI_TIMEOUT_SECONDS, env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    found = []
    for line in (completed.stdout or "").splitlines():
        match = _MCP_LIST_LINE_RE.match(line)
        if match:
            found.append((match.group(1), "cli", []))
    return found


def discover_servers(*, claude_json_path=None, run_cli: bool = True) -> "list[dict]":
    """Every MCP server this machine has configured, as
    `[{"name": ..., "source": ..., "tools": [...]}, ...]`.

    Merges three sources in precedence order -- top-level `mcpServers` in
    `~/.claude.json` (`source="claude.json"`), plugin-provided servers
    (`source="plugin"`), and `claude mcp list` (`source="cli"`, skipped
    when `run_cli=False`, e.g. in tests). A server seen more than once
    keeps the first source that named it and accumulates every tool name
    any source volunteered.

    **No credential ever leaves this function** (Spec §12.3.7): the
    per-server configuration -- which is where a token, header, or `env`
    block would live -- is read for its `tools` list and nothing else, and
    the returned dict is constructed field-by-field by `_server_entry`.
    """
    config_path = Path(claude_json_path) if claude_json_path else Path.home() / ".claude.json"
    # The plugin tree lives beside the config file (~/.claude/plugins next to
    # ~/.claude.json), so a test pointing at a temp config never reaches the
    # real home directory.
    home = config_path.parent

    discovered: "list[tuple[str, str, list[str]]]" = []
    data = _read_json(config_path)
    if isinstance(data, dict):
        discovered.extend(_servers_from_mapping(data.get("mcpServers"), "claude.json"))
    discovered.extend(_plugin_servers(home))
    if run_cli:
        discovered.extend(_cli_servers())

    merged: "dict[str, dict]" = {}
    for name, source, tools in discovered:
        if name in merged:
            merged[name]["tools"] = sorted(set(merged[name]["tools"]) | set(tools))
        else:
            merged[name] = _server_entry(name, source, tools)
    return list(merged.values())


def jira_candidates(servers: "list[dict]") -> "list[dict]":
    """The subset of `servers` whose name, or one of whose tool names,
    mentions Jira or Atlassian."""
    candidates = []
    for server in servers:
        if not isinstance(server, dict):
            continue
        name = server.get("name") or ""
        tools = server.get("tools") or []
        if _JIRA_RE.search(name) or any(
            isinstance(tool, str) and _JIRA_RE.search(tool) for tool in tools
        ):
            candidates.append(server)
    return candidates


def _bare_tool_name(tool_name: str) -> str:
    """`mcp__atlassian__getJiraIssue` -> `getJiraIssue`."""
    return tool_name.rsplit("__", 1)[-1]


def map_capabilities(tool_names: "list[str]") -> "tuple[dict[str, bool], dict[str, str]]":
    """Map concrete MCP tool names onto work-ledger's capability vocabulary.

    Returns `(supported, tools)`. `supported` always carries every key in
    `CAPABILITY_VALUES` (unmatched ones are `False`); `tools` carries only
    the matched capabilities, mapped to the *full* tool name the model must
    call. Both are keyed by plain strings (Ruling 27), so
    `supported["get_issue"]` and `tools["get_issue"]` work.

    Matching is case-insensitive on the tool's bare name (the segment after
    the last `__`), so the same hint table serves `mcp__atlassian__getJiraIssue`
    and a bare `getJiraIssue`.
    """
    index: "dict[str, str]" = {}
    for tool_name in tool_names or []:
        if not isinstance(tool_name, str):
            continue
        index.setdefault(_bare_tool_name(tool_name).lower(), tool_name)

    supported = {capability: False for capability in sorted(CAPABILITY_VALUES)}
    tools: "dict[str, str]" = {}
    for capability, hints in CAPABILITY_TOOL_HINTS.items():
        for hint in hints:
            match = index.get(hint.lower())
            if match is not None:
                supported[capability] = True
                tools[capability] = match
                break
    return supported, tools


# --------------------------------------------------------------------------
# Row helpers
# --------------------------------------------------------------------------

def _field(row, key, default=None):
    """Read `key` from a `sqlite3.Row` or a plain dict, defaulting when the
    column is absent (`sqlite3.Row` raises `IndexError` for an unknown key)."""
    try:
        value = row[key]
    except (IndexError, KeyError, TypeError):
        return default
    return default if value is None else value


@contextlib.contextmanager
def _database(conn):
    """Yield the caller's connection, or -- when the adapter was built
    without one -- a short-lived connection to the ledger at its configured
    path, closed again on the way out."""
    if conn is not None:
        yield conn
        return
    opened = dbc.connect()
    try:
        yield opened
    finally:
        opened.close()


def _recorded_tools(conn, connection_id: str) -> "dict[str, str]":
    """`capability -> tool_name` as persisted by
    `connections.record_capabilities`, for supported capabilities only."""
    if not connection_id:
        return {}
    try:
        with _database(conn) as db:
            rows = db.execute(
                "SELECT capability, tool_name FROM connection_capabilities "
                "WHERE connection_id=? AND supported=1 AND tool_name IS NOT NULL",
                (connection_id,),
            ).fetchall()
    except sqlite3.Error:
        # A capability table that cannot be read is a degraded connection,
        # not a crash: callers fall back to the hint-derived tool name.
        # Deliberately narrow -- a bug in this module must still surface.
        return {}
    return {row["capability"]: row["tool_name"] for row in rows
            if row["capability"] in CAPABILITY_VALUES}


def tool_for_capability(conn, connection_row, capability: str) -> "str | None":
    """The concrete `mcp__<server>__<tool>` the model should call for
    `capability` on this connection.

    Prefers the tool name setup actually verified and persisted; falls back
    to `mcp__<server>__<first hint>` so a plan or an error message can still
    name something useful before the first capability probe has run.
    """
    recorded = _recorded_tools(conn, _field(connection_row, "id", ""))
    if capability in recorded:
        return recorded[capability]
    server = _field(connection_row, "mcp_server_name")
    hints = CAPABILITY_TOOL_HINTS.get(capability) or []
    if server and hints:
        return f"mcp__{server}__{hints[0]}"
    return None


class JiraMcpAdapter(JiraAdapter):
    """The MCP-backed `JiraAdapter`. Read-only, like every adapter -- and
    in fact call-free: see the module docstring.

    `conn` is optional so the adapter can be constructed from a connection
    row alone (`connections.list_connections(...)[0]`); when it is omitted,
    the capability/health reads open the ledger at its configured path for
    the duration of the query. `jira.factory.build_adapter` always passes
    the caller's connection.
    """

    def __init__(self, connection_row, conn=None):
        self._row = connection_row
        self._conn = conn
        self._name = _field(connection_row, "name", "")
        self._connection_id = _field(connection_row, "id", "")
        self._server = _field(connection_row, "mcp_server_name")

    @property
    def name(self) -> str:
        return self._name

    @property
    def mcp_server_name(self) -> "str | None":
        return self._server

    def capabilities(self) -> "dict[str, bool]":
        """What setup verified this MCP server can do, read back from
        `connection_capabilities` and keyed by plain strings (Ruling 27).
        Unprobed capabilities read as `False` rather than being absent."""
        result = {capability: False for capability in sorted(CAPABILITY_VALUES)}
        if not self._connection_id:
            return result
        with _database(self._conn) as db:
            rows = db.execute(
                "SELECT capability, supported FROM connection_capabilities WHERE connection_id=?",
                (self._connection_id,),
            ).fetchall()
        for row in rows:
            if row["capability"] in result:
                result[row["capability"]] = bool(row["supported"])
        return result

    def _last_checked_at(self) -> "str | None":
        if not self._connection_id:
            return None
        with _database(self._conn) as db:
            row = db.execute(
                "SELECT MAX(checked_at) AS last FROM connection_capabilities WHERE connection_id=?",
                (self._connection_id,),
            ).fetchone()
        return row["last"] if row is not None else None

    def _require_model(self, capability: str, detail: str):
        tool = tool_for_capability(self._conn, self._row, capability) or (
            f"the MCP server's {capability} tool"
        )
        raise ModelMediationRequired(
            f"{self._name or 'this connection'}: {detail} is model-mediated -- a hook or script "
            f"cannot call an MCP tool. The model must call {tool} and pipe the result into "
            f"`worklog jira ingest --connection {self._name}`."
        )

    def identify_current_user(self) -> IdentityResult:
        self._require_model(Capability.IDENTIFY_CURRENT_USER.value, "identity lookup")

    def get_issue(self, key: str) -> IssueResult:
        self._require_model(Capability.GET_ISSUE.value, f"fetching {key}")

    def search_issues(self, jql: str, limit: int = 50) -> "list[IssueResult]":
        self._require_model(Capability.SEARCH_ISSUES.value, "JQL search")

    def health(self) -> HealthResult:
        """`healthy` while the capability probe is younger than
        `jira.stale_after_hours`, otherwise `degraded` -- because for an MCP
        connection nothing refreshes on its own (DESIGN §10.4, gap G2): the
        model has to run the plan/ingest loop."""
        checked_at = timeutil.utc_now_iso()
        last = self._last_checked_at()
        if last and not _older_than_stale_window(last, checked_at):
            return HealthResult(state="healthy", detail=None, checked_at=checked_at)
        return HealthResult(
            state="degraded", detail="awaiting model-mediated refresh", checked_at=checked_at
        )


def _is_count(value) -> bool:
    """A usable non-negative integer from user-edited YAML. `bool` is
    excluded explicitly -- `isinstance(True, int)` is `True`, and a config
    that says `limit: yes` must fall back to the default, not plan one
    issue key."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _stale_after_hours() -> int:
    hours = config.get("jira.stale_after_hours", 12)
    return hours if _is_count(hours) else 12


def default_refresh_limit() -> int:
    """`jira.max_refresh_per_turn` for `worklog jira plan-refresh`, guarded
    the same way `_stale_after_hours` guards `jira.stale_after_hours`:
    config.yaml is hand-editable, so a string, a float, or a negative value
    falls back to the documented default instead of reaching SQL."""
    limit = config.get("jira.max_refresh_per_turn", 20)
    return limit if _is_count(limit) else 20


def _older_than_stale_window(timestamp: str, now_iso: str) -> bool:
    try:
        age_hours = (
            timeutil.parse_iso(now_iso) - timeutil.parse_iso(timestamp)
        ).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return True
    return age_hours > _stale_after_hours()


# --------------------------------------------------------------------------
# plan-refresh (what the model should fetch, and with which tool)
# --------------------------------------------------------------------------

def _stale_cutoff(now_iso: str) -> str:
    moment = timeutil.parse_iso(now_iso) - timedelta(hours=_stale_after_hours())
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _stale_issue_keys(conn, connection_id: str, limit: int, now_iso: str) -> "list[str]":
    """Jira-backed work items on this connection that need a refresh:
    explicitly flagged stale, never fetched, or fetched longer ago than
    `jira.stale_after_hours`. Oldest fetch first so a bounded plan always
    makes progress."""
    rows = conn.execute(
        "SELECT external_key FROM work_items "
        "WHERE connection_id=? AND kind='jira' AND deleted_at IS NULL "
        "AND merged_into_id IS NULL AND external_key IS NOT NULL "
        "AND (jira_stale=1 OR jira_fetched_at IS NULL OR jira_fetched_at < ?) "
        "ORDER BY (jira_fetched_at IS NOT NULL), jira_fetched_at, external_key "
        "LIMIT ?",
        (connection_id, _stale_cutoff(now_iso), max(0, int(limit))),
    ).fetchall()
    return [row["external_key"] for row in rows]


def plan_refresh(conn, *, connection_name=None, limit: int) -> dict:
    """What the model should fetch this turn, per MCP connection.

    `{"connections": [{"connection", "mcp_server", "tool", "cloud_id",
    "issue_keys"}]}`. Only `adapter='mcp'` connections appear (REST
    connections refresh themselves), and only those with at least one stale
    key -- an empty plan is an empty list, never a list of empty entries.
    """
    now_iso = timeutil.utc_now_iso()
    planned = []
    for row in connections.list_connections(conn):
        if row["adapter"] != "mcp":
            continue
        if connection_name is not None and row["name"] != connection_name:
            continue
        issue_keys = _stale_issue_keys(conn, row["id"], limit, now_iso)
        if not issue_keys:
            continue
        planned.append({
            "connection": row["name"],
            "mcp_server": _field(row, "mcp_server_name"),
            "tool": tool_for_capability(conn, row, Capability.GET_ISSUE.value),
            "cloud_id": _field(row, "cloud_id"),
            "issue_keys": issue_keys,
        })
    return {"connections": planned}


# --------------------------------------------------------------------------
# ingest (untrusted stdin -> ledger)
# --------------------------------------------------------------------------

def _capped(value):
    """Truncate an untrusted payload string to `_MAX_FIELD_CHARS`, passing
    non-strings (and `None`) through untouched. Applied to every value
    `ingest` writes, not just the title."""
    if isinstance(value, str) and len(value) > _MAX_FIELD_CHARS:
        return value[:_MAX_FIELD_CHARS]
    return value


def _redact_strings(node):
    """Recursively replace every string leaf with its redacted form
    (Ruling 15). Unlike `redact.redact_obj` this never blanks a value for
    the *name* of its key -- a Jira payload's `key`/`project.key` fields
    would not survive that -- but it applies the identical pattern set,
    owned solely by `redact`."""
    if isinstance(node, dict):
        return {k: _redact_strings(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_redact_strings(item) for item in node]
    if isinstance(node, str):
        return redact.redact(node)[0]
    return node


def _entries_of(payload) -> "list":
    """Accept `[...]`, `{"issues": [...]}`, or a single issue object.
    Anything else is a caller error, not a per-entry rejection."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if isinstance(payload.get("issues"), list):
            return payload["issues"]
        if "key" in payload:
            return [payload]
    raise ValueError("ingest payload must be a list of issues or {'issues': [...]}")


def _normalized_updated(value):
    """Jira Cloud stamps `updated` as `...+0000`; the ledger stores UTC ISO-8601
    with a `Z` and millisecond precision (Global Constraints). Unparseable
    input is dropped rather than stored in a foreign shape."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    match = re.search(r"([+-]\d{2})(\d{2})$", text)
    if match:
        text = text[: match.start()] + f"{match.group(1)}:{match.group(2)}"
    try:
        moment = timeutil.parse_iso(text)
    except (ValueError, TypeError):
        return None
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _claims_key(patterns, key: str) -> bool:
    """Does this connection's `ticket_patterns` claim `key`?

    Matched exactly the way `manual.matching_connection_id` and
    `infer.stage_a.ticket_patterns` match the same column, so one ledger
    never disagrees with itself about which connection owns a key:

    * **Case-insensitively** (`fnmatchcase` on both sides uppercased).
      `manual` uppercases the key and the pattern; `stage_a` compiles its
      patterns with `re.IGNORECASE` because branch names are routinely
      lowercased (`feature/ab-76`). A connection configured as `["ab-*"]`
      therefore accumulates `AB-76` work items through Stage A, and ingest
      must claim that same key rather than reporting it unknown.
    * **An empty pattern list claims nothing**, not everything. `manual`
      and `stage_a` both iterate the list and simply never match when it is
      empty; a connection that has not declared its ticket space does not
      get to absorb every key the model happens to relay.
    """
    key_upper = key.upper()
    return any(
        fnmatch.fnmatchcase(key_upper, glob.upper())
        for glob in patterns
        if isinstance(glob, str) and glob
    )


def ingest(conn, connection_name: str, payload) -> dict:
    """Write model-relayed Jira issue payloads into the ledger.

    Returns `{"updated", "unknown_keys", "rejected", "contradictions_opened"}`.

    stdin is untrusted (threat model B6). Every entry must be an object
    carrying a `key` matching `^[A-Z][A-Z0-9]+-\\d+$`; anything else is
    counted in `rejected` and never written. A well-formed key this
    connection's `ticket_patterns` do not claim is reported in
    `unknown_keys` and, likewise, never written. Every string is redacted
    first (Ruling 15), every write is parameterized, no payload value is
    ever executed, evaluated, or used to build SQL, and unknown fields are
    ignored rather than stored.
    """
    # Enabled connections only -- the same set `plan_refresh` plans for.
    # A disabled connection is one the user switched off; honoring a
    # relayed payload for it would write behind that decision.
    row = next(
        (c for c in connections.list_connections(conn) if c["name"] == connection_name),
        None,
    )
    if row is None:
        raise ValueError(f"no enabled connection named {connection_name!r}")

    entries = _entries_of(payload)
    try:
        patterns = json.loads(_field(row, "ticket_patterns_json", "[]"))
    except (ValueError, TypeError):
        patterns = []
    if not isinstance(patterns, list):
        patterns = []

    updated = 0
    unknown_keys: "list[str]" = []
    rejected: "list[dict]" = []
    touched: "list[str]" = []
    now = timeutil.utc_now_iso()

    for entry in entries:
        if not isinstance(entry, dict):
            rejected.append({"key": None, "reason": "entry is not an object"})
            continue
        safe = _redact_strings(entry)
        key = safe.get("key")
        if not isinstance(key, str) or not _ISSUE_KEY_RE.match(key):
            rejected.append({"key": key if isinstance(key, str) else None,
                             "reason": "missing or malformed issue key"})
            continue
        if not _claims_key(patterns, key):
            unknown_keys.append(key)
            continue

        issue = IssueResult.from_cloud_payload(safe)
        existing = conn.execute(
            "SELECT id, title FROM work_items WHERE external_key=? AND deleted_at IS NULL "
            "AND (connection_id=? OR connection_id IS NULL) ORDER BY connection_id IS NULL LIMIT 1",
            (key, row["id"]),
        ).fetchone()
        summary = issue.summary if isinstance(issue.summary, str) and issue.summary.strip() else None
        title = summary or (existing["title"] if existing is not None else None) or key
        work_item_id = work_items.upsert_external(
            conn, row["id"], key,
            title=_capped(title),
            external_id=_capped(issue.id) if isinstance(issue.id, str) else None,
        )
        with dbc.transaction(conn):
            conn.execute(
                # COALESCE on jira_updated_at: a payload that omits or
                # malforms `updated` must not erase a good timestamp an
                # earlier fetch established. The other columns are the
                # issue's current state, so a genuine clear (unassigned,
                # resolution removed) has to be able to write NULL.
                "UPDATE work_items SET jira_status=?, jira_status_category=?, jira_resolution=?, "
                "jira_assignee=?, jira_updated_at=COALESCE(?, jira_updated_at), jira_fetched_at=?, "
                "jira_stale=0, updated_at=? WHERE id=?",
                (_capped(issue.status), _capped(issue.status_category), _capped(issue.resolution),
                 _capped(issue.assignee_account_id), _normalized_updated(issue.updated), now, now,
                 work_item_id),
            )
        updated += 1
        if work_item_id not in touched:
            # A payload may name the same key twice; the contradiction scan
            # below is scoped by an IN clause, which wants distinct ids.
            touched.append(work_item_id)

    contradictions_opened = 0
    if touched:
        outcome = contradictions.evaluate(conn, work_item_ids=touched)
        contradictions_opened = len(outcome["opened"])
        # Data actually flowed from the MCP server through the model to
        # here, which is the only evidence of reachability an MCP
        # connection can produce (DESIGN §4.5).
        connections.set_health(conn, row["id"], "healthy", None)

    return {
        "updated": updated,
        "unknown_keys": unknown_keys,
        "rejected": rejected,
        "contradictions_opened": contradictions_opened,
    }


def jira_work_items(conn, *, stale_only: bool = False) -> "list[dict]":
    """Jira-backed work items and the Jira fields the ledger holds for them
    -- what `worklog jira issues [--stale]` renders.

    Read-only and connection-agnostic (a REST-backed item shows up here
    too): staleness is the same predicate `plan_refresh` uses, so
    `--stale` lists exactly what a refresh would go after.
    """
    now_iso = timeutil.utc_now_iso()
    # Fixed, literal clause fragments -- never built from user, model, or
    # Jira text (Global Constraints: SQL safety); only the cutoff is a
    # bound parameter.
    clauses = ["wi.kind='jira'", "wi.deleted_at IS NULL", "wi.merged_into_id IS NULL"]
    params: list = []
    if stale_only:
        clauses.append(
            "(wi.jira_stale=1 OR wi.jira_fetched_at IS NULL OR wi.jira_fetched_at < ?)"
        )
        params.append(_stale_cutoff(now_iso))
    rows = conn.execute(
        "SELECT wi.external_key, wi.title, wi.jira_status, wi.jira_status_category, "
        "wi.jira_resolution, wi.jira_assignee, wi.jira_fetched_at, wi.jira_stale, "
        "c.name AS connection "
        "FROM work_items wi LEFT JOIN connections c ON c.id = wi.connection_id "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY wi.external_key",
        params,
    ).fetchall()
    return [
        {
            "external_key": row["external_key"],
            "title": row["title"],
            "connection": row["connection"],
            "status": row["jira_status"],
            "status_category": row["jira_status_category"],
            "resolution": row["jira_resolution"],
            "assignee": row["jira_assignee"],
            "fetched_at": row["jira_fetched_at"],
            "stale": bool(row["jira_stale"]),
        }
        for row in rows
    ]
