"""Resumable, idempotent setup wizard (Spec §12, §13, §20.18).

Ten steps, in the Spec §12.1 order, each one wrapped so that a failure yields
`{"state": "fail", ...}` and the wizard *continues*. Spec §13 is explicit that
"a failed Jira connection must not prevent Claude, Git, or manual capture", and
the only way to honour that is for no single step to be able to abort the run.

Resumable and idempotent, concretely
------------------------------------
`run()` re-executes every step on every invocation rather than skipping the
ones already in `state["completed"]`, because every step is idempotent by
construction: the connection, identity and repository writes all go through
upsert helpers keyed on a natural key (`connections.name`,
`(user_identities.kind, connection_id, value)`, `repositories.root_path`), and
this module performs no direct INSERT of its own into any of the three. A
re-run therefore updates rows in place and never duplicates them (Spec §20.18,
`test_rerun_does_not_duplicate_connections_identities_or_repositories`).

What `state/setup.json` buys is the other half of "resumable": it remembers
the *answers* a previous run collected, so an interactive setup interrupted at
step 7 does not re-ask for the timezone and the development roots on the next
attempt, and it records which steps have ever succeeded so a partially
completed setup is visible (`worklog setup --only <step>` advances exactly one
of them). `--reset` throws that memory away and starts from a blank state
without touching a single row of history.

Secrets (Global Constraints §Secrets, Spec §14.1)
------------------------------------------------
The wizard stores a *credential reference* (`keychain://…` / `env://…`) and
nothing else. A raw token supplied interactively (or via `answers["jira"]
["token"]`) is written straight into the OS credential store and is never
persisted to `config.yaml`, to `state/setup.json`, or to the `connections`
table. `_persistable_answers` strips secret-shaped keys and runs
`redact.redact_obj` over what is left before anything is written to disk.

Repository discovery (Spec §12.6, CARRIED POINTER from Task 21)
---------------------------------------------------------------
`discover_repositories` searches *only* the roots the user approved plus
directories that recent Claude transcripts actually recorded as their `cwd`.
It never scans the whole filesystem, and it never reverse-decodes a
`~/.claude/projects/<encoded-cwd>` directory name: that encoding (every "/"
replaced by "-") is not reversible, because a directory name may legitimately
contain a dash. The cwd is read out of the transcript *records*, which carry
it verbatim.
"""
import json
import os
import pathlib

from work_ledger import (
    config,
    connections,
    credentials,
    doctor,
    entities,
    gitevidence,
    ids,
    paths,
    redact,
    timeutil,
    transcript,
)
from work_ledger.capture import events as capture_events
from work_ledger.capture import sessions as capture_sessions
from work_ledger.db import connection as dbc
from work_ledger.db import migrations
from work_ledger.infer import stage_a
from work_ledger.jira import factory as jira_factory
from work_ledger.jira import mcp as jira_mcp

__all__ = [
    "STEPS",
    "State",
    "run",
    "discover_repositories",
    "completion_report",
    "step_directories",
    "step_preferences",
    "step_transcripts",
    "step_jira_detect",
    "step_jira_identity",
    "step_git_identity",
    "step_repositories",
    "step_hooks",
    "step_end_to_end",
    "step_report",
]

# Spec §12.1, in order. `run()` walks exactly this tuple.
STEPS: "tuple[str, ...]" = (
    "directories",
    "preferences",
    "transcripts",
    "jira_detect",
    "jira_identity",
    "git_identity",
    "repositories",
    "githooks",
    "hooks",
    "end_to_end",
    "report",
)

# A failure in one of these means setup did not achieve its purpose. Everything
# else (Jira above all -- Spec §13) degrades the run without failing it.
ESSENTIAL_STEPS = frozenset({"directories", "preferences", "end_to_end"})

STATE_VERSION = 1
STATE_FILENAME = "setup.json"

# Discovery bounds. Spec §12.6 forbids scanning the filesystem, so every walk
# is bounded three ways: an approved root list, a depth, and a repo count.
MAX_ROOT_DEPTH = 6
MAX_TRANSCRIPT_PROJECT_DIRS = 10
MAX_TRANSCRIPT_BYTES = 200_000

# Directory names never worth descending into while hunting for `.git`.
_SKIP_DIR_NAMES = frozenset({
    "node_modules", "__pycache__", "venv", ".venv", "env", ".tox", ".mypy_cache",
    ".pytest_cache", "build", "dist", "target", "vendor", "Pods", "DerivedData",
    "Library", "Applications", ".Trash", ".npm", ".cache", ".gradle", ".m2",
})

# Answer keys that could carry a raw secret. Never persisted, never logged.
_SECRET_ANSWER_KEYS = frozenset({"token", "secret", "password", "api_token", "pat"})

_JIRA_MODES = ("skip", "mcp", "rest", "auto")


# --------------------------------------------------------------------------
# Persisted state
# --------------------------------------------------------------------------

class State:
    """`state/setup.json`: `{"version":1,"completed":[...],"data":{...},"updated_at":...}`.

    `data` holds each step's last recorded result plus the answers collected
    so far. `cache` is deliberately *not* persisted: it memoizes work that is
    only valid for the duration of one `run()` (the discovered repository
    list, which both `git_identity` and `repositories` need and neither should
    compute twice).
    """

    def __init__(self, completed=None, data=None, updated_at=None):
        self.version = STATE_VERSION
        self.completed: "list[str]" = list(completed or [])
        self.data: dict = dict(data or {})
        self.updated_at = updated_at
        self.cache: dict = {}

    # -- persistence -------------------------------------------------------

    @staticmethod
    def path() -> pathlib.Path:
        return paths.state_dir() / STATE_FILENAME

    @classmethod
    def load(cls) -> "State":
        """Read the state file. A missing, unreadable, malformed, or
        wrong-version file yields a blank state rather than an exception:
        setup must always be runnable."""
        path = cls.path()
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            return cls()
        if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
            return cls()
        completed = raw.get("completed")
        data = raw.get("data")
        return cls(
            completed=completed if isinstance(completed, list) else [],
            data=data if isinstance(data, dict) else {},
            updated_at=raw.get("updated_at"),
        )

    def save(self) -> None:
        paths.ensure_dirs()
        self.updated_at = timeutil.utc_now_iso()
        payload = {
            "version": self.version,
            "completed": [s for s in STEPS if s in self.completed],
            "data": self.data,
            "updated_at": self.updated_at,
        }
        path = self.path()
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(payload, indent=2, sort_keys=True))
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, path)

    @classmethod
    def clear(cls) -> None:
        """Delete the progress file. History (the database) is untouched."""
        try:
            cls.path().unlink(missing_ok=True)
        except OSError:
            pass

    # -- progress ----------------------------------------------------------

    def record(self, name: str, outcome: dict) -> None:
        # `redact.redact` (the text form) has no key rule, so a credential
        # reference quoted in a detail message survives it unchanged while a
        # real secret would not.
        detail, _fired = redact.redact(str(outcome["detail"] or ""))
        self.data.setdefault("steps", {})[name] = {
            "state": outcome["state"],
            "detail": detail,
            "recorded_at": timeutil.utc_now_iso(),
        }
        if outcome["state"] in ("ok", "warn") and name not in self.completed:
            self.completed.append(name)
        elif outcome["state"] == "fail" and name in self.completed:
            self.completed.remove(name)


# --------------------------------------------------------------------------
# Answers
# --------------------------------------------------------------------------

def _is_safe_reference(value) -> bool:
    """True for a credential *reference* -- the one thing config and the
    database are allowed to hold (`keychain://…`, `env://…`).

    Deliberately asks the two modules that own the vocabulary rather than
    matching a prefix here: `credentials.parse_reference` decides what a
    reference is, and `redact.looks_like_secret` gets the final say, so this
    can never wave through something the redactor considers a secret.
    """
    if not isinstance(value, str):
        return False
    try:
        credentials.parse_reference(value)
    except credentials.InvalidReference:
        return False
    return not redact.looks_like_secret(value)


def _persistable_answers(answers: dict) -> dict:
    """`answers` minus anything that could be a secret. Secret-shaped keys are
    dropped outright and the remainder goes through the ordinary redactor, so
    a token pasted into the wrong field still never lands in the state file.

    One value is then restored: a credential *reference*. `redact_obj`'s key
    rule fires on any key containing "credential", so `credential_reference`
    came back as `[REDACTED:credential_reference]` -- and because the state
    file feeds the next run's answers, that placeholder was then written over
    a perfectly good connection, taking its health to `missing`. A reference
    is not a secret (that is the entire point of holding one), so it is put
    back, but only when `_is_safe_reference` agrees it really is one.
    """
    def strip(node):
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items()
                    if k.lower() not in _SECRET_ANSWER_KEYS}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    stripped = strip(answers)
    cleaned, _names = redact.redact_obj(stripped)

    def restore(original, redacted):
        if isinstance(original, dict) and isinstance(redacted, dict):
            return {k: restore(original.get(k), v) for k, v in redacted.items()}
        if isinstance(original, list) and isinstance(redacted, list):
            return [restore(o, r) for o, r in zip(original, redacted)]
        return original if _is_safe_reference(original) else redacted

    return restore(stripped, cleaned)


def _csv(raw) -> "list[str]":
    """A comma-separated answer as a list, with blanks dropped."""
    return [part.strip() for part in str(raw or "").split(",") if part.strip()]


def _ask(prompt_fn, question: str, default):
    """One interactive question with a default. Never called when `answers`
    was supplied, which is why no test ever blocks on stdin."""
    suffix = f" [{default}]" if default not in (None, "") else ""
    try:
        typed = prompt_fn(f"{question}{suffix}: ")
    except (EOFError, KeyboardInterrupt):
        return default
    typed = (typed or "").strip()
    return typed if typed else default


# Which questions a step needs answered. `--only <step>` asks only the
# questions that step actually consumes, so advancing one step of a resumed
# setup does not re-interrogate the user about everything else.
_STEP_QUESTIONS = {
    "preferences": ("preferences",),
    "jira_detect": ("jira",),
    "git_identity": ("roots", "git_emails"),
    "repositories": ("roots",),
}


def _interactive_answers(prompt_fn, previous: dict, wanted: "set[str]") -> dict:
    cfg = config.load()
    answers = dict(previous)

    if "preferences" in wanted:
        answers["timezone"] = _ask(
            prompt_fn, "Reporting timezone",
            previous.get("timezone") or cfg.get("timezone"))
        answers["rollover_hour"] = _ask(
            prompt_fn, "Workday rollover hour (0-23)",
            previous.get("rollover_hour", cfg.get("rollover_hour")))

    if "roots" in wanted:
        roots = _ask(
            prompt_fn, "Development roots to search for repositories (comma separated)",
            ", ".join(previous.get("dev_roots") or []))
        answers["dev_roots"] = _csv(roots)

    if "git_emails" in wanted:
        # Spec §12.5: "additional author emails belonging to the user". Only
        # commits matching a stored identity can be described as the user's
        # work, so an author email git's own config never sees (a work address
        # used on another machine, say) has to be collectable here.
        extra = _ask(
            prompt_fn, "Additional git author emails (comma separated, blank for none)",
            ", ".join(previous.get("git_emails") or []))
        answers["git_emails"] = _csv(extra)

    if "jira" in wanted:
        jira = dict(previous.get("jira") or {})
        mode = _ask(prompt_fn, "Jira setup (skip/auto/mcp/rest)", jira.get("mode") or "auto")
        jira["mode"] = mode if mode in _JIRA_MODES else "skip"
        if jira["mode"] == "rest":
            jira["name"] = _ask(prompt_fn, "Connection name", jira.get("name") or "jira")
            jira["base_url"] = _ask(prompt_fn, "Jira base URL", jira.get("base_url"))
            jira["username"] = _ask(prompt_fn, "Jira account email", jira.get("username"))
            # A reference, never a secret: any token typed below goes straight
            # to the credential store and is dropped from `answers` before
            # anything is persisted (`_persistable_answers`).
            default_ref = jira.get("credential_reference") or (
                f"keychain://work-ledger/{jira.get('name') or 'jira'}")
            jira["credential_reference"] = _ask(
                prompt_fn, "Credential reference (keychain://… or env://VAR)", default_ref)
            token = _ask(prompt_fn, "API token (blank to keep the stored one)", "")
            if token:
                jira["token"] = token

        if jira["mode"] != "skip":
            # Spec §12.2: ticket-key patterns, the client hint, and the
            # optional project restriction. `connections.ticket_patterns_json`
            # has no other writer in the system -- an interactive user who is
            # never asked gets an empty pattern list and, with it, zero ticket
            # attribution from Stage A.
            hint = _ask(prompt_fn, "Client this Jira serves (blank for none)",
                        jira.get("client_hint") or "")
            jira["client_hint"] = hint or None
            patterns = _ask(
                prompt_fn, "Ticket key patterns (comma separated globs, e.g. AB-*)",
                ", ".join(jira.get("ticket_patterns") or []))
            jira["ticket_patterns"] = _csv(patterns)
            projects = _ask(
                prompt_fn, "Restrict to Jira project keys (comma separated, blank for all)",
                ", ".join(jira.get("project_filter") or []))
            jira["project_filter"] = _csv(projects)

        answers["jira"] = jira

    return answers


def _resolve_answers(answers, state: State, prompt_fn, steps: "tuple[str, ...]") -> dict:
    """Merge previously stored answers with this run's. `answers is None`
    means interactive; any dict (including `{}`) means non-interactive, with
    defaults filling every gap."""
    previous = dict(state.data.get("answers") or {})
    if answers is None:
        wanted = {group for step in steps
                  for group in _STEP_QUESTIONS.get(step, ())}
        resolved = _interactive_answers(prompt_fn, previous, wanted)
    else:
        resolved = {**previous, **dict(answers)}
        if "jira" in previous and "jira" in (answers or {}):
            resolved["jira"] = {**(previous.get("jira") or {}), **(answers["jira"] or {})}

    cfg = config.load()
    resolved.setdefault("timezone", cfg.get("timezone"))
    resolved.setdefault("rollover_hour", cfg.get("rollover_hour"))
    resolved.setdefault("nominal_start_hour", cfg.get("nominal_start_hour"))
    resolved.setdefault("dev_roots", [])
    resolved.setdefault("jira", {"mode": "skip"})

    state.data["answers"] = _persistable_answers(resolved)
    return resolved


# --------------------------------------------------------------------------
# Repository discovery (Spec §12.6)
# --------------------------------------------------------------------------

def _normalize_root(root: str) -> "str | None":
    """An approved root, absolute and expanded, or None if it is unusable or
    dangerous. The filesystem root is refused outright: Spec §12.6 forbids
    scanning the whole filesystem, and `/` is exactly that."""
    if not isinstance(root, str) or not root.strip():
        return None
    expanded = os.path.abspath(os.path.expanduser(root.strip()))
    if os.path.dirname(expanded) == expanded:  # "/" (or a Windows drive root)
        return None
    if not os.path.isdir(expanded):
        return None
    return expanded


def _repo_metadata(root_path: str, source: str) -> dict:
    return {
        "root_path": root_path,
        "name": os.path.basename(root_path.rstrip("/")) or root_path,
        "default_branch": gitevidence.default_branch(root_path),
        "remotes": gitevidence.remotes(root_path),
        "source": source,
    }


def _walk_for_repo_dirs(root: str, remaining: int) -> "list[str]":
    """Directories under `root` (inclusive) that contain a `.git` entry.

    Bounded by `MAX_ROOT_DEPTH` and `remaining`. A directory that *is* a repo
    is not descended into: nested working trees and submodules are deliberately
    not enumerated, which keeps the walk cheap and the result predictable.
    """
    found: "list[str]" = []
    root_depth = root.rstrip("/").count("/")
    for dirpath, dirnames, _filenames in os.walk(root, topdown=True, onerror=lambda _e: None):
        if len(found) >= remaining:
            break
        if os.path.exists(os.path.join(dirpath, ".git")):
            found.append(dirpath)
            dirnames[:] = []
            continue
        if dirpath.rstrip("/").count("/") - root_depth >= MAX_ROOT_DEPTH:
            dirnames[:] = []
            continue
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIR_NAMES and not d.startswith(".")
        ]
    return found


def discover_repositories(*, roots: "list[str]", transcript_cwds: "list[str]",
                          limit: int = 200) -> "list[dict]":
    """Git repositories under the approved `roots`, plus the repositories that
    contain each path in `transcript_cwds` (Spec §12.6).

    Never scans the whole filesystem: `roots` is the user's explicit approval
    list, `/` is refused, the walk is depth- and count-bounded, and
    `transcript_cwds` are literal `cwd` values read out of transcript records
    (never decoded from a `~/.claude/projects` directory name -- that encoding
    is not reversible).

    Each entry is `{"root_path", "name", "default_branch", "remotes",
    "source"}`, where `source` is `"root"` (found by walking an approved root)
    or `"referenced"` (the repository containing a supplied cwd). De-duplicated
    by the canonical git top-level path, `"root"` winning.
    """
    found: "dict[str, dict]" = {}

    for root in roots or []:
        if len(found) >= limit:
            break
        normalized = _normalize_root(root)
        if normalized is None:
            continue
        for candidate in _walk_for_repo_dirs(normalized, limit - len(found)):
            top = gitevidence.repo_root(candidate) or candidate
            if top not in found:
                found[top] = _repo_metadata(top, "root")
            if len(found) >= limit:
                break

    for cwd in transcript_cwds or []:
        if len(found) >= limit:
            break
        if not isinstance(cwd, str) or not cwd or not os.path.isdir(cwd):
            continue
        top = gitevidence.repo_root(cwd)
        if top and top not in found:
            found[top] = _repo_metadata(top, "referenced")

    return sorted(found.values(), key=lambda r: r["root_path"])


def recent_transcript_cwds(limit_dirs: int = MAX_TRANSCRIPT_PROJECT_DIRS) -> "list[str]":
    """The `cwd` recorded by the most recently touched Claude transcripts.

    CARRIED POINTER (Task 21): the `~/.claude/projects/<cwd with / -> ->`
    encoding is NOT reversible -- a directory name that legitimately contains a
    dash is indistinguishable from an encoded separator -- so this reads the
    `cwd` field out of the transcript records themselves and never decodes a
    directory name.
    """
    root = transcript.projects_root()
    try:
        if not root.is_dir():
            return []
        project_dirs = [d for d in root.iterdir() if d.is_dir()]
    except OSError:
        return []

    def mtime(path):
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    project_dirs.sort(key=mtime, reverse=True)
    cwds: "list[str]" = []
    seen = set()
    for project_dir in project_dirs[:limit_dirs]:
        try:
            files = sorted(project_dir.glob("*.jsonl"), key=mtime, reverse=True)
        except OSError:
            continue
        for path in files[:1]:
            for record in transcript.iter_records(path, max_bytes=MAX_TRANSCRIPT_BYTES):
                cwd = record.get("cwd")
                if not isinstance(cwd, str) or not cwd:
                    continue
                if cwd not in seen:
                    seen.add(cwd)
                    cwds.append(cwd)
                break  # one cwd per transcript is enough; it never changes mid-file
    return cwds


def _discovered(state: State, answers: dict) -> "list[dict]":
    """Discovery result for this run, memoized so `git_identity` and
    `repositories` (two consecutive steps that both need it) pay for it once."""
    cached = state.cache.get("discovered")
    if cached is not None:
        return cached

    roots = [r for r in (answers.get("dev_roots") or []) if isinstance(r, str)]
    cwds = list(state.data.get("transcript_cwds") or [])
    # Spec §12.6 lists the current repository first among the places worth
    # looking; `git_identity` also uses it to pick the primary identity.
    here = gitevidence.repo_root(os.getcwd())
    state.cache["current_repo"] = here
    if here:
        cwds.insert(0, here)

    discovered = discover_repositories(roots=roots, transcript_cwds=cwds)
    state.cache["discovered"] = discovered
    return discovered


# --------------------------------------------------------------------------
# Identity persistence
# --------------------------------------------------------------------------

def _upsert_identity(conn, *, kind: str, value: str, connection_id=None,
                     display_name=None, is_primary: bool = False) -> str:
    """Idempotent on the `uq_user_identities` key
    `(kind, IFNULL(connection_id,''), value)` -- the natural key the schema
    already enforces, so a re-run updates rather than duplicating (Spec §20.18).

    `is_primary` is only ever set, never cleared: a later re-run that does not
    claim primacy must not demote an identity the user (or an earlier run)
    designated.
    """
    device_id = entities.current_device(conn)
    now = timeutil.utc_now_iso()
    with dbc.transaction(conn):
        row = conn.execute(
            "SELECT id, is_primary FROM user_identities "
            "WHERE kind=? AND IFNULL(connection_id,'')=IFNULL(?,'') AND value=? "
            "AND deleted_at IS NULL",
            (kind, connection_id, value),
        ).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE user_identities SET display_name=COALESCE(?, display_name), "
                "is_primary=MAX(is_primary, ?), updated_at=? WHERE id=?",
                (display_name, int(bool(is_primary)), now, row["id"]),
            )
            return row["id"]
        identity_id = ids.new_id()
        conn.execute(
            "INSERT INTO user_identities(id, kind, connection_id, value, display_name, "
            "is_primary, origin_device_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (identity_id, kind, connection_id, value, display_name,
             int(bool(is_primary)), device_id, now, now),
        )
        return identity_id


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------

def _outcome(state: str, detail: str, **data) -> dict:
    return {"state": state, "detail": detail, "data": data}


def step_directories(conn, state: State, answers: dict) -> dict:
    """Spec §12.1.1 -- directories, configuration, database, migrations."""
    paths.ensure_dirs()
    if not paths.config_path().exists():
        config.save(dict(config.DEFAULTS))
    applied = migrations.pending(conn)
    migrations.ensure_current(conn)
    return _outcome(
        "ok",
        f"data directory ready at {paths.home()}",
        home=str(paths.home()),
        migrations_applied=[version for version, _name, _sql in applied],
    )


def step_preferences(conn, state: State, answers: dict) -> dict:
    """Spec §12.1.2 -- timezone, rollover, evidence preferences.

    Writes only the non-secret preference keys; `config.validate` decides
    whether what the user supplied is usable, and an invalid value is reported
    rather than silently written.
    """
    cfg = config.load()
    proposed = dict(cfg)
    for key in ("timezone", "rollover_hour", "nominal_start_hour"):
        value = answers.get(key)
        if value is None:
            continue
        if key.endswith("_hour"):
            try:
                value = int(value)
            except (TypeError, ValueError):
                return _outcome("warn", f"{key}={answers.get(key)!r} is not an integer; kept {cfg.get(key)!r}")
        proposed[key] = value

    excerpt_max = answers.get("excerpt_max_chars")
    if excerpt_max is not None:
        try:
            excerpt_max = int(excerpt_max)
        except (TypeError, ValueError):
            return _outcome("warn", f"excerpt_max_chars={excerpt_max!r} is not an integer")
        proposed["capture"] = {**(proposed.get("capture") or {}),
                               "excerpt_max_chars": excerpt_max}

    problems = config.validate(proposed)
    if problems:
        return _outcome("warn", "; ".join(problems), problems=problems)

    config.save(proposed)
    return _outcome(
        "ok",
        f"timezone={proposed['timezone']} rollover_hour={proposed['rollover_hour']}",
        timezone=proposed["timezone"], rollover_hour=proposed["rollover_hour"],
    )


def step_transcripts(conn, state: State, answers: dict) -> dict:
    """Spec §12.1.3 -- identify Claude transcript locations."""
    root = transcript.projects_root()
    if not root.is_dir():
        state.data["transcript_cwds"] = []
        return _outcome("warn", f"no Claude transcript directory at {root}",
                        projects_root=str(root), readable=False)
    if not os.access(root, os.R_OK | os.X_OK):
        state.data["transcript_cwds"] = []
        return _outcome("warn", f"Claude transcript directory {root} is not readable",
                        projects_root=str(root), readable=False)

    cwds = recent_transcript_cwds()
    state.data["transcript_cwds"] = cwds
    return _outcome(
        "ok",
        f"{root} readable; {len(cwds)} recent project cwd(s)",
        projects_root=str(root), readable=True, cwds=cwds,
    )


def _jira_answers(answers: dict) -> dict:
    jira = answers.get("jira")
    return dict(jira) if isinstance(jira, dict) else {"mode": "skip"}


def _store_token(jira: dict) -> "str | None":
    """Put an interactively supplied raw token into the credential store and
    return a problem description, or None on success. The token itself never
    leaves this function."""
    token = jira.get("token")
    reference = jira.get("credential_reference")
    if not token or not reference:
        return None
    try:
        credentials.get_store(reference).set(reference, token)
    except (credentials.Unsupported, credentials.InvalidReference) as exc:
        return str(exc)
    return None


def _configure_mcp(conn, jira: dict) -> dict:
    """Spec §12.3 -- detect MCP servers, pick a Jira-capable one, record its
    capabilities. No MCP credential is ever read or copied."""
    servers = jira_mcp.discover_servers(run_cli=bool(jira.get("run_cli", True)))
    candidates = jira_mcp.jira_candidates(servers)
    if not candidates:
        return _outcome("warn", "no Jira-capable MCP server is configured",
                        servers=[s["name"] for s in servers])

    wanted = jira.get("server")
    chosen = next((c for c in candidates if c["name"] == wanted), candidates[0])
    name = jira.get("name") or chosen["name"]
    caps, tool_names = jira_mcp.map_capabilities(chosen.get("tools") or [])
    connection_id = connections.upsert_connection(
        conn, name=name, adapter="mcp", mcp_server_name=chosen["name"],
        auth_method="mcp-oauth", client_hint=jira.get("client_hint"),
        ticket_patterns=jira.get("ticket_patterns"),
        project_filter=jira.get("project_filter"),
    )
    connections.record_capabilities(conn, connection_id, caps, tool_names=tool_names)
    result = connections.test_connection(conn, name)
    step_state = "ok" if result["state"] == "healthy" else "warn"
    return _outcome(step_state, f"MCP connection {name!r} is {result['state']}",
                    connection=name, adapter="mcp", health=result["state"],
                    checks=result["checks"])


def _configure_rest(conn, jira: dict) -> dict:
    """Spec §12.4 -- direct Jira. SQLite and config store only a credential
    reference; the secret lives in the credential store."""
    name = jira.get("name") or "jira"
    base_url = jira.get("base_url")
    reference = jira.get("credential_reference")
    if not base_url:
        return _outcome("warn", "no Jira base URL supplied", connection=name)

    store_problem = _store_token(jira)
    connection_id = connections.upsert_connection(
        conn, name=name, adapter="jira-rest", base_url=base_url,
        username=jira.get("username"),
        auth_method=jira.get("auth_method") or "cloud-api-token",
        credential_reference=reference, client_hint=jira.get("client_hint"),
        ticket_patterns=jira.get("ticket_patterns"),
        project_filter=jira.get("project_filter"),
    )
    if store_problem:
        connections.set_health(conn, connection_id, "missing", store_problem)
        return _outcome("warn", f"{name}: {store_problem}", connection=name, adapter="jira-rest")

    # Resolve the credential before touching the network: an unresolvable
    # reference is an authentication failure that no request can fix, and
    # issuing an unauthenticated request to the user's Jira site to discover
    # that would be both slower and ruder.
    resolved = None
    try:
        resolved = credentials.resolve(reference) if reference else None
    except Exception as exc:  # a broken store must not abort setup
        connections.set_health(conn, connection_id, "missing", str(exc))
        return _outcome("warn", f"{name}: credential store unavailable: {exc}",
                        connection=name, adapter="jira-rest")
    if resolved is None:
        detail = f"credential reference {reference!r} does not resolve to a secret"
        connections.set_health(conn, connection_id, "missing", detail)
        return _outcome("warn", f"{name}: {detail}", connection=name, adapter="jira-rest")

    result = connections.test_connection(conn, name)
    step_state = "ok" if result["state"] == "healthy" else "warn"
    return _outcome(step_state, f"REST connection {name!r} is {result['state']}",
                    connection=name, adapter="jira-rest", health=result["state"],
                    checks=result["checks"])


def step_jira_detect(conn, state: State, answers: dict) -> dict:
    """Spec §12.1.4 -- detect and validate Jira connection options.

    Never fatal: Spec §13 requires that a failed Jira connection leave Claude,
    Git and manual capture working, so every outcome here is `ok` or `warn`
    unless the step itself raised.
    """
    jira = _jira_answers(answers)
    mode = jira.get("mode") or "auto"
    if mode == "skip":
        return _outcome("ok", "Jira setup skipped", mode="skip")

    if mode == "auto":
        detected = _configure_mcp(conn, jira)
        if detected["state"] == "ok" or not jira.get("base_url"):
            return detected
        mode = "rest"

    if mode == "mcp":
        return _configure_mcp(conn, jira)
    if mode == "rest":
        return _configure_rest(conn, jira)
    return _outcome("warn", f"unknown Jira setup mode {mode!r}", mode=mode)


def step_jira_identity(conn, state: State, answers: dict) -> dict:
    """Spec §12.1.5 -- identify the current user for each Jira connection.

    Only probes a connection the previous step found healthy: re-probing an
    endpoint that just failed costs a second round trip to learn the same
    thing, and Spec §13 does not require it.
    """
    rows = [r for r in connections.list_connections(conn, include_disabled=True)]
    if not rows:
        return _outcome("ok", "no Jira connections to identify", identities=[])

    identified, skipped = [], []
    for row in rows:
        if row["health_state"] != "healthy":
            skipped.append(f"{row['name']} ({row['health_state']})")
            continue
        try:
            adapter = jira_factory.build_adapter(conn, row)
            identity = adapter.identify_current_user()
        except Exception as exc:  # never fatal (Spec §13)
            skipped.append(f"{row['name']} ({type(exc).__name__})")
            continue
        value = identity.account_id or identity.email
        if not value:
            skipped.append(f"{row['name']} (no account id)")
            continue
        _upsert_identity(conn, kind="jira", value=value, connection_id=row["id"],
                         display_name=identity.display_name, is_primary=True)
        identified.append({"connection": row["name"], "value": value})

    if identified:
        detail = "identified " + ", ".join(i["connection"] for i in identified)
        if skipped:
            detail += "; skipped " + ", ".join(skipped)
        return _outcome("ok", detail, identities=identified, skipped=skipped)
    return _outcome("warn", "no Jira identity could be established: " + ", ".join(skipped),
                    identities=[], skipped=skipped)


def step_git_identity(conn, state: State, answers: dict) -> dict:
    """Spec §12.1.6 / §12.5 -- detect Git author identities.

    Only commits matching a stored identity can later be described as the
    user's work, so this reads `user.email` from every discovered repository
    (git's own config cascade already folds in the global value) plus any
    additional emails the user declared.
    """
    discovered = _discovered(state, answers)
    emails: "dict[str, str | None]" = {}
    current_email = None

    for repo in discovered:
        found = gitevidence.identities(repo["root_path"])
        email = found.get("email")
        if email:
            emails.setdefault(email, found.get("name"))
            if repo["root_path"] == state.cache.get("current_repo"):
                current_email = email

    if not emails:
        # No repository to ask through: fall back to git's global config,
        # read from the current directory (a read-only `git config` call).
        rc, out, _err = gitevidence.run_git(os.getcwd(), ["config", "user.email"])
        if rc == 0 and out.strip():
            emails[out.strip()] = None

    for extra in answers.get("git_emails") or []:
        if isinstance(extra, str) and extra.strip():
            emails.setdefault(extra.strip(), None)

    if not emails:
        return _outcome("warn", "no git author identity could be detected", identities=[])

    # The identity of the repository the user ran setup from is the one they
    # are most likely to mean; fall back to the first alphabetically only when
    # there is no such repository (a bare `--only git_identity` outside a repo).
    primary = answers.get("primary_git_email") or current_email or min(emails)

    stored = []
    for email, display_name in sorted(emails.items()):
        _upsert_identity(conn, kind="git", value=email, display_name=display_name,
                         is_primary=(email == primary))
        stored.append(email)
    return _outcome("ok", f"{len(stored)} git identity(ies): " + ", ".join(stored),
                    identities=stored, primary=primary)


def step_repositories(conn, state: State, answers: dict) -> dict:
    """Spec §12.1.7 / §12.6 -- discover user-approved repositories.

    Idempotent through `entities.upsert_repository`, which matches on
    `root_path` and leaves a user's earlier `approved=False` alone, so a
    re-run never duplicates a repository nor silently re-approves one.
    """
    discovered = _discovered(state, answers)
    if not discovered:
        return _outcome("warn", "no repositories discovered in the approved roots",
                        repositories=[])

    approve = bool(answers.get("approve_discovered", True))
    stored, newly_approved = [], 0
    for repo in discovered:
        # `approved` is supplied only when the row is new. `upsert_repository`
        # treats a supplied value as an overwrite, and a re-run of setup must
        # never revert an `approved=False` the user set afterwards.
        known = conn.execute(
            "SELECT 1 FROM repositories WHERE root_path=? AND deleted_at IS NULL",
            (repo["root_path"],),
        ).fetchone() is not None
        entities.upsert_repository(
            conn, repo["root_path"], name=repo["name"],
            default_branch=repo["default_branch"], remotes=repo["remotes"],
            approved=None if known else approve,
        )
        if not known and approve:
            newly_approved += 1
        stored.append(repo["root_path"])
    detail = f"{len(stored)} repository(ies) discovered"
    if newly_approved:
        # Said out loud rather than asked per repository: setup already only
        # looks where the user pointed it, so a second yes/no per repository is
        # noise. `worklog` can flip `approved` afterwards.
        detail += f", {newly_approved} newly auto-approved"
    return _outcome("ok", detail, repositories=stored, newly_approved=newly_approved)


def step_githooks(conn, state: State, answers: dict) -> dict:
    """Opt-in git post-commit capture (DESIGN "Git-side capture").

    Strictly opt-in: this is the one step that writes into the user's own
    repositories (`.git/hooks/post-commit`), so nothing is installed unless
    `answers["githooks"]["install"]` says so. Skipping is an `ok` outcome,
    not a warning -- declining an optional feature is a healthy state.
    """
    from work_ledger import githooks

    wanted = bool((answers.get("githooks") or {}).get("install"))
    if not wanted:
        return _outcome(
            "ok", "git-side capture not requested (opt-in: worklog githooks install)",
            installed=[])
    rows = conn.execute(
        "SELECT root_path FROM repositories"
        " WHERE deleted_at IS NULL AND approved=1").fetchall()
    installed, problems = [], []
    for row in rows:
        try:
            result = githooks.install_hook(conn, row["root_path"])
            installed.append(result["repo"])
        except (ValueError, OSError) as exc:
            problems.append(f"{row['root_path']}: {exc}")
    if problems:
        return _outcome("warn", "; ".join(problems), installed=installed)
    return _outcome("ok", f"post-commit capture installed in {len(installed)} repository(ies)",
                    installed=installed)


def step_hooks(conn, state: State, answers: dict) -> dict:
    """Spec §12.1.8 -- install and validate capture hooks.

    Ruling 19: an absent or empty `hooks/hooks.json` is a `warn` ("not yet
    configured/installed"), never a `fail` -- the hooks are wired by installing
    the plugin, which setup itself cannot do on the user's behalf.
    """
    hooks_state, detail = doctor.check_hooks_installed(conn)
    if hooks_state != "ok":
        return _outcome("warn" if hooks_state == "warn" else "fail", detail)

    # doctor owns the single definition of where the plugin lives; re-deriving
    # it here would let the two drift.
    root = doctor.plugin_root()
    entry = root / "bin" / "worklog"
    if not entry.exists():
        return _outcome("warn", f"hook entry point {entry} is missing")
    if not os.access(entry, os.X_OK):
        return _outcome("warn", f"hook entry point {entry} is not executable")

    hooks_file = root / "hooks" / "hooks.json"
    data = json.loads(hooks_file.read_text())
    if not isinstance(data.get("hooks"), dict) or not data["hooks"]:
        return _outcome("warn", f"{hooks_file} declares no hook events")
    events = sorted(data["hooks"].keys())
    missing = [
        event for event, groups in data["hooks"].items()
        if not any("bin/worklog hook" in (h.get("command") or "")
                   for group in (groups or []) for h in (group.get("hooks") or []))
    ]
    if missing:
        return _outcome("warn", "hook entries do not invoke bin/worklog: " + ", ".join(sorted(missing)),
                        events=events)
    return _outcome("ok", f"{len(events)} hook event(s) wired to {entry}", events=events)


def step_end_to_end(conn, state: State, answers: dict) -> dict:
    """Spec §12.1.9 -- run an end-to-end test.

    Writes one synthetic `source_event`, runs Stage A over it, reads the
    resulting activity back, and soft-deletes it, so the whole capture ->
    interpretation -> query path is exercised without leaving fake history in
    any report. `source_events` are append-only by design (Global Constraints),
    so the probe's raw evidence row stays -- it is raw evidence of a self-test,
    which is exactly what it is.
    """
    session_id = "work-ledger-setup-selftest"
    now = timeutil.utc_now_iso()
    probe_path = str(paths.state_dir() / "setup-selftest.txt")
    # One self-test event per UTC day: `source_events` are append-only, so a
    # per-run idempotency key would grow the table by one row every time setup
    # was re-run. A same-day re-run reuses the stored event and still runs the
    # whole Stage A -> read-back -> cleanup path over it.

    capture_sessions.upsert(conn, session_id=session_id, cwd=str(paths.home()),
                            occurred_at=now)
    event_id = capture_events.append_source_event(
        conn, session_id=session_id, event_type="tool_use", occurred_at=now,
        payload={"tool_name": "Write", "tool_input": {"file_path": probe_path},
                 "tool_response": {"success": True}},
        hook_event="PostToolUse", tool_name="Write",
        tool_use_id=f"setup-selftest-{now[:10]}", cwd=str(paths.home()),
        idem=f"setup-selftest|{now[:10]}",
    )
    reused = event_id is None

    before = {r[0] for r in conn.execute("SELECT id FROM work_items")}
    result = stage_a.run(conn, session_id)
    activity_ids = list(result.get("activities") or [])
    read_back = []
    if activity_ids:
        # Placeholders only -- one "?" per id, never an id spliced into SQL.
        placeholders = ",".join("?" for _ in activity_ids)
        read_back = [dict(row) for row in conn.execute(
            f"SELECT id, summary, confidence FROM activities WHERE id IN ({placeholders})",
            activity_ids,
        ).fetchall()]

    for row in read_back:
        entities.soft_delete(conn, "activities", row["id"])
    for (item_id,) in conn.execute("SELECT id FROM work_items").fetchall():
        if item_id not in before:
            entities.soft_delete(conn, "work_items", item_id)
    capture_sessions.finalize(conn, session_id, ended_at=timeutil.utc_now_iso(),
                              reason="session_end")

    if not read_back:
        return _outcome("warn",
                        "the self-test event was stored but Stage A produced no activity",
                        source_event_id=event_id, reused=reused)
    stored = "reused today's" if reused else "captured a"
    return _outcome("ok",
                    f"{stored} self-test event, interpreted and cleaned up "
                    f"{len(read_back)} activity(ies)",
                    source_event_id=event_id, reused=reused,
                    activities=[row["id"] for row in read_back])


def step_report(conn, state: State, answers: dict) -> dict:
    """Spec §12.1.10 / §12.7 -- present a completion and health report."""
    text = completion_report(conn)
    return _outcome("ok", "completion report generated", text=text)


STEP_FUNCS = {
    "directories": step_directories,
    "preferences": step_preferences,
    "transcripts": step_transcripts,
    "jira_detect": step_jira_detect,
    "jira_identity": step_jira_identity,
    "git_identity": step_git_identity,
    "repositories": step_repositories,
    "githooks": step_githooks,
    "hooks": step_hooks,
    "end_to_end": step_end_to_end,
    "report": step_report,
}


# --------------------------------------------------------------------------
# Completion report (Spec §12.7)
# --------------------------------------------------------------------------

def _database_state(conn) -> str:
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    except Exception:
        return "unhealthy"
    return "healthy" if row and str(row[0]).lower() == "ok" else "unhealthy"


def _hooks_state() -> str:
    state, _detail = doctor.check_hooks_installed()
    return {"ok": "healthy", "warn": "not installed"}.get(state, "unhealthy")


def _format_hour(hour) -> str:
    try:
        hour = int(hour)
    except (TypeError, ValueError):
        return str(hour)
    suffix = "AM" if hour < 12 else "PM"
    display = hour % 12 or 12
    return f"{display}:00 {suffix}"


def completion_report(conn) -> str:
    """The Spec §12.7 completion and health report, as text."""
    cfg = config.load()
    lines = ["Work Ledger setup complete", ""]
    lines.append(f"Database: {_database_state(conn)}")
    lines.append(f"Claude capture hooks: {_hooks_state()}")

    git_count = conn.execute(
        "SELECT COUNT(*) FROM user_identities WHERE kind='git' AND deleted_at IS NULL"
    ).fetchone()[0]
    lines.append(f"Git identity: {'configured' if git_count else 'not configured'}")

    repo_count = conn.execute(
        "SELECT COUNT(*) FROM repositories WHERE deleted_at IS NULL"
    ).fetchone()[0]
    lines.append(f"Repositories discovered: {repo_count}")

    rows = connections.list_connections(conn, include_disabled=True)
    lines.append("Jira connections:")
    if not rows:
        lines.append("  (none configured)")
    for row in rows:
        via = "MCP" if row["adapter"] == "mcp" else "REST"
        lines.append(f"  {row['name']}: {row['health_state']} via {via}")

    lines.append(f"Reporting timezone: {cfg.get('timezone')}")
    lines.append(f"Workday rollover: {_format_hour(cfg.get('rollover_hour'))}")

    unresolved = conn.execute(
        "SELECT COUNT(*) FROM repositories r WHERE r.deleted_at IS NULL AND NOT EXISTS ("
        "  SELECT 1 FROM repository_mappings m WHERE m.repository_id = r.id "
        "  AND m.deleted_at IS NULL)"
    ).fetchone()[0]
    lines.append("")
    lines.append(f"Unresolved repository mappings: {unresolved}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def _overall(results: "list[dict]") -> str:
    if any(r["state"] == "fail" and r["name"] in ESSENTIAL_STEPS for r in results):
        return "failed"
    if any(r["state"] in ("fail", "warn") for r in results):
        return "degraded"
    return "completed"


def run(conn, *, answers: "dict | None" = None, reset: bool = False,
        only: "str | None" = None, prompt=None) -> dict:
    """Run the Spec §12.1 setup workflow and return the completion report.

    `answers` supplies non-interactive input (used by the tests and by
    `worklog setup --non-interactive`); `answers=None` prompts through
    `prompt` (defaulting to the builtin `input`), which is injectable so no
    test can ever block on stdin.

    `reset=True` discards `state/setup.json` -- progress and remembered
    answers -- before running. It deletes no history: every step is idempotent,
    so a reset run rewrites the same connection, identity and repository rows
    rather than creating new ones.

    `only` runs exactly one step (still recording its progress), for
    `worklog setup --only <step>`.

    No step can abort the run: each one is wrapped so a raised exception
    becomes `{"state": "fail", ...}` and the walk continues (Spec §13 -- a
    failed Jira connection must not prevent Claude, Git or manual capture).
    """
    if only is not None and only not in STEPS:
        raise ValueError(f"unknown setup step {only!r}; expected one of {STEPS}")

    paths.ensure_dirs()
    if reset:
        State.clear()
        state = State()
    else:
        state = State.load()

    steps = (only,) if only else STEPS
    resolved = _resolve_answers(
        answers, state, prompt if prompt is not None else input, steps)

    results = []
    for name in steps:
        try:
            outcome = STEP_FUNCS[name](conn, state, resolved)
        except Exception as exc:  # a step never aborts the wizard
            outcome = _outcome("fail", f"{type(exc).__name__}: {exc}")
        entry = {"name": name, "state": outcome["state"], "detail": outcome["detail"],
                 "data": outcome.get("data") or {}}
        results.append(entry)
        state.record(name, entry)
        try:
            state.save()
        except OSError:
            pass

    report = {
        "overall": _overall(results),
        "steps": results,
        "completed": list(state.completed),
        "home": str(paths.home()),
        "generated_at": timeutil.utc_now_iso(),
    }
    report_step = next((r for r in results if r["name"] == "report"), None)
    if report_step is not None and report_step["data"].get("text"):
        report["text"] = report_step["data"]["text"]
    return report
