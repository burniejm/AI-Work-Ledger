"""Opt-in per-repo git `post-commit` capture (DESIGN "Git-side capture").

`install_hook` appends ONE marker-tagged line to the repo's
`.git/hooks/post-commit` (creating the file if absent, never clobbering an
existing hook); `remove_hook` deletes exactly that line. Installed repos
are tracked in the `settings` table so `worklog uninstall` can clean up
precisely.

The hook line resolves the `worklog` binary at run time -- `worklog` on
PATH first, else the newest installed plugin cache version -- so a plugin
update never strands a hook pointing at a removed version directory. It
ends with `|| true` and backgrounds nothing: `capture` is fast (one
`git log -1` through the read-only allowlist plus one hook-budget INSERT)
and a broken ledger must never block a commit, so the CLI layer for
`githooks capture` swallows every error and always exits 0.
"""
import json
import os
import stat

from work_ledger import entities, gitevidence, ids, redact, timeutil
from work_ledger.db import connection as dbc

HOOK_MARKER = "# work-ledger githooks v1"

_SETTINGS_KEY = "githooks_repos"

# The hook body: one line, POSIX sh, marker-tagged. PATH first; otherwise the
# highest-version plugin cache copy. Never fails the commit.
_HOOK_LINE = (
    '( W="$(command -v worklog || ls -d "$HOME"/.claude/plugins/cache/work-ledger/'
    'work-ledger/*/bin/worklog 2>/dev/null | sort -V | tail -1)" ; '
    '[ -n "$W" ] && "$W" githooks capture --repo "$(pwd)" ) >/dev/null 2>&1 '
    f'|| true  {HOOK_MARKER}'
)

_SHEBANG = "#!/bin/sh\n"


def hook_line() -> str:
    return _HOOK_LINE


def _hook_path(repo: str) -> str:
    return os.path.join(repo, ".git", "hooks", "post-commit")


def installed_repos(conn) -> "list[str]":
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?", (_SETTINGS_KEY,)).fetchone()
    if row is None:
        return []
    try:
        repos = json.loads(row["value"])
    except (TypeError, ValueError):
        return []
    return sorted(r for r in repos if isinstance(r, str))


def _write_tracked(conn, repos: "list[str]") -> None:
    with dbc.transaction(conn):
        conn.execute(
            "INSERT INTO settings(key, value, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            " updated_at=excluded.updated_at",
            (_SETTINGS_KEY, json.dumps(sorted(set(repos))), timeutil.utc_now_iso()))


def install_hook(conn, repo: str) -> dict:
    """Append the marker line to the repo's post-commit hook (idempotent)."""
    repo = os.path.realpath(repo)
    root = gitevidence.repo_root(repo)
    if root is None:
        raise ValueError(f"not a git repository: {repo!r}")
    path = _hook_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    existing = ""
    if os.path.exists(path):
        with open(path) as f:
            existing = f.read()
    if HOOK_MARKER in existing:
        installed = True
    else:
        if not existing:
            existing = _SHEBANG
        if not existing.endswith("\n"):
            existing += "\n"
        with open(path, "w") as f:
            f.write(existing + _HOOK_LINE + "\n")
        installed = False
    mode = os.stat(path).st_mode
    os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    _write_tracked(conn, installed_repos(conn) + [root])
    return {"repo": root, "already_installed": installed}


def remove_hook(conn, repo: str) -> dict:
    """Delete exactly our marker line; leave everything else untouched."""
    repo = os.path.realpath(repo)
    root = gitevidence.repo_root(repo) or repo
    path = _hook_path(root)
    removed = False
    if os.path.exists(path):
        with open(path) as f:
            lines = f.readlines()
        kept = [line for line in lines if HOOK_MARKER not in line]
        if len(kept) != len(lines):
            with open(path, "w") as f:
                f.writelines(kept)
            removed = True
    _write_tracked(conn, [r for r in installed_repos(conn) if r != root])
    return {"repo": root, "removed": removed}


def remove_all(conn) -> "list[dict]":
    """`worklog uninstall`'s cleanup: remove every tracked hook line."""
    return [remove_hook(conn, repo) for repo in installed_repos(conn)]


def _session_is_active(conn, repo: str, now_iso: str) -> bool:
    """A live Claude session for this repo right now? 'Live' = not ended and
    seen within the idle-finalize window (same notion the sweeps use)."""
    from work_ledger import config
    idle_minutes = config.get("session.idle_finalize_minutes", 30)
    if isinstance(idle_minutes, bool) or not isinstance(idle_minutes, int) or idle_minutes <= 0:
        idle_minutes = 30
    from datetime import timedelta
    cutoff = (timeutil.parse_iso(now_iso) - timedelta(minutes=idle_minutes)) \
        .strftime("%Y-%m-%dT%H:%M:%S.000Z")
    root = os.path.realpath(repo)
    rows = conn.execute(
        "SELECT cwd FROM sessions WHERE ended_at IS NULL AND cwd IS NOT NULL"
        "  AND last_seen_at >= ?", (cutoff,)).fetchall()
    for row in rows:
        # realpath both sides: session cwd is recorded as the hook sent it,
        # which on macOS may be the /var alias of the /private/var real path.
        cwd = os.path.realpath(row["cwd"])
        if cwd == root or cwd.startswith(root.rstrip("/") + "/"):
            return True
    return False


def capture(conn, repo: str, *, hook_mode: bool = True) -> dict:
    """Record the repo's newest commit. Idempotent by (repo, sha)."""
    repo = os.path.realpath(repo)
    root = gitevidence.repo_root(repo)
    if root is None:
        return {"captured": False, "reason": "not a git repository"}
    commits = gitevidence.commits_since(root, "1970-01-01T00:00:00Z", limit=1)
    if not commits:
        return {"captured": False, "reason": "no commits"}
    commit = commits[0]
    now = timeutil.utc_now_iso()
    subject, _fired = redact.redact(commit.get("subject") or "")
    author, _fired = redact.redact(commit.get("author_email") or "")
    files = [f for f in (commit.get("files") or []) if isinstance(f, str)][:100]
    device_id = entities.current_device(conn, hook_mode=hook_mode)
    active = _session_is_active(conn, root, now)
    with dbc.transaction(conn, hook_mode=hook_mode):
        cursor = conn.execute(
            "INSERT OR IGNORE INTO human_commits(id, repo_root, sha, authored_at,"
            " author_email, subject, files_json, session_active, origin_device_id, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ids.new_id(), root, commit["sha"],
             commit.get("authored_at") or now, author, subject,
             json.dumps(files), 1 if active else 0, device_id, now))
    return {"captured": cursor.rowcount == 1, "sha": commit["sha"],
            "session_active": active}
