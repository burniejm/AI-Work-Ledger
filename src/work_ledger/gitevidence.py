"""Bounded, read-only git evidence collector (Task 24; Spec §2.2; Ruling 15).

Every function in this module is a *read*. ``run_git`` is the single choke
point that shells out to the real ``git`` binary. Reads always use an argv
list — the subprocess shell is never enabled — and always carry a timeout,
so a hung or huge repository can never stall the plugin.

CONTROLLER RULING (supersedes the brief's args[0]-denylist spec): a denylist
keyed on ``args[0]`` is bypassable in at least three ways confirmed during
review:

- ``["config", "user.email", "x"]`` — the *write* form of an otherwise
  legitimate read command.
- ``["symbolic-ref", "HEAD", "refs/heads/x"]`` — same problem, different
  dual-use command.
- ``["-c", "user.email=x", "commit", "--allow-empty", "-m", "x"]`` — a
  leading global option pushes the real subcommand off position 0, so a
  check that only inspects ``args[0]`` against a mutating-subcommand set
  never even sees ``"commit"``.

``run_git`` therefore enforces an ALLOWLIST instead: ``args[0]`` must be
exactly one of ``_ALLOWED_SUBCOMMANDS`` (never an option — any leading
argument starting with ``"-"`` is refused outright, closing the global-option
bypass), and every dual-use command on that list is further restricted to its
read forms only. A first allowlist pass admitted ``branch`` and ``remote``
outright with no read-form check — live probes during a follow-up review
confirmed ``branch newbranch``/``branch -D``/``branch -m`` and ``remote
add``/``remote remove`` all mutate the repo despite being "allowed"
subcommands, exactly the shape of hole a bare allowlist-of-names invites.
Fixed by dropping ``branch`` from the allowlist entirely (nothing in this
module calls it) and adding a `remote` read-form check
(``_check_remote_read_form``) mirroring ``config``/``symbolic-ref``'s,
restricting it to ``-v``, ``get-url``, and ``show``. A further probe found
that ANY allowed subcommand in the diff/log/show family accepts
``--output[=<path>]``, which writes its output to an arbitrary file on disk
regardless of which read-only subcommand carries it — closed by rejecting
any argument starting with ``--output``, in any position, for every
subcommand (not just the dual-use ones). So the allowlist alone is not the
whole story: `args[0]` membership is necessary but not sufficient — each
admitted subcommand is only as safe as its own read-form check (or, for
options like `--output` that cut across every subcommand, a check that runs
regardless of which subcommand was requested). Any git global options this
module itself needs (see ``_SAFE_GLOBAL_OPTIONS`` below) are a module
constant that ``run_git`` prepends internally — callers can never supply
their own, since a caller-supplied leading ``"-"`` argument is exactly what
the allowlist rejects.

``MUTATING_SUBCOMMANDS`` is kept, for compatibility with anything that still
references it, but it is no longer what enforces anything — the allowlist
(plus each admitted subcommand's own read-form/whole-args-list checks) is.

Ruling 15: remote URLs and commit excerpts collected here are a required
redaction sink — ``remotes()`` scrubs credentials embedded in a remote URL
(e.g. ``https://user:pass@host/repo.git``) via ``work_ledger.redact`` before
returning them. Callers that fold ``commits_since`` output into evidence rows
are responsible for running subjects/bodies through ``redact`` too, since this
module has no opinion on how those strings are stored.

Nothing here raises for "expected" failure modes (missing repo, git not on
PATH, a command that times out) — those come back as ``None``/``[]``/an
error tuple so a caller can log and move on rather than crashing the plugin
around a directory that merely isn't a git repo. Refusing an unsafe/disallowed
invocation is the one thing that *does* raise (``ValueError``), since that is
a programming error in the caller, not an expected runtime condition.
"""
import subprocess

from work_ledger import config, redact

# Module-owned global options prepended to EVERY git invocation. This is a
# fixed constant, not something a caller can influence (a caller-supplied
# leading "-" argument in `args` is rejected by `_check_allowed` below) —
# `core.quotePath=false` keeps non-ASCII filenames coming back from
# `--name-only` unescaped/unquoted instead of octal-escaped.
_SAFE_GLOBAL_OPTIONS = ["-c", "core.quotePath=false"]

# The read-only commands gitevidence actually needs. `run_git` raises
# ValueError if `args[0]` is anything else, INCLUDING an option (an option at
# position 0 can never be a legitimate subcommand for this module's callers,
# and allowing one is exactly the "-c ... commit" bypass above).
#
# `diff`, `show`, `status`, and `ls-files` were dropped here (final review
# Important #3): none of them had a single production caller (only
# `rev-parse`/`remote`/`log`/`config`/`symbolic-ref` are actually invoked --
# verified with `grep -rn "run_git(" src/`), and `diff --no-index <path>
# <path>` is an arbitrary-file-read primitive with no read-form check
# guarding it -- pure attack surface for zero benefit. If a future caller
# needs one of these back, it must land with the same kind of read-form
# check `config`/`symbolic-ref`/`remote` already have, not a bare name on
# this set.
_ALLOWED_SUBCOMMANDS = frozenset({
    "rev-parse", "remote", "log", "config", "symbolic-ref",
})

# Retained for compatibility with anything that references it; no longer the
# enforcement mechanism (see module docstring's CONTROLLER RULING).
MUTATING_SUBCOMMANDS: "frozenset[str]" = frozenset({
    "commit", "push", "merge", "rebase", "cherry-pick", "revert", "reset",
    "checkout", "switch", "branch", "tag", "add", "rm", "mv", "stash",
    "clean", "apply", "am", "filter-branch", "gc", "prune",
})

_CONFIG_READ_FLAGS = frozenset({"--get", "--get-all"})
_SYMBOLIC_REF_ALLOWED_FLAGS = frozenset({"--quiet", "-q", "--short"})
_REMOTE_GET_URL_ALLOWED_FLAGS = frozenset({"--push", "--all"})
_REMOTE_SHOW_ALLOWED_FLAGS = frozenset({"-n"})

_DEFAULT_TIMEOUT_SECONDS = 2
_DEFAULT_MAX_COMMITS = 50

_RECORD_SEP = "\x1e"
_FIELD_SEP = "\x1f"
_COMMIT_LOG_FORMAT = (
    f"format:%H{_FIELD_SEP}%an{_FIELD_SEP}%ae{_FIELD_SEP}%aI{_FIELD_SEP}%s{_FIELD_SEP}%b{_RECORD_SEP}"
)


def _check_config_read_form(args: "list[str]") -> None:
    rest = args[1:]
    if not rest:
        raise ValueError("gitevidence.run_git: git config requires a read-form argument")
    if rest[0] == "--list":
        if len(rest) > 1:
            raise ValueError("gitevidence.run_git: git config --list takes no further arguments")
        return
    if rest[0] in _CONFIG_READ_FLAGS:
        if len(rest) != 2:
            raise ValueError(f"gitevidence.run_git: git config {rest[0]} requires exactly one key")
        return
    if len(rest) == 1 and not rest[0].startswith("-"):
        return  # bare `git config <key>` read form
    raise ValueError(
        "gitevidence.run_git: git config is restricted to its read form "
        "(--get/--get-all/--list, or a single bare key) — no write form"
    )


def _check_symbolic_ref_read_form(args: "list[str]") -> None:
    rest = args[1:]
    positional = []
    for arg in rest:
        if arg.startswith("-"):
            if arg not in _SYMBOLIC_REF_ALLOWED_FLAGS:
                raise ValueError(f"gitevidence.run_git: git symbolic-ref flag not allowed: {arg!r}")
        else:
            positional.append(arg)
    if len(positional) != 1:
        raise ValueError(
            "gitevidence.run_git: git symbolic-ref is restricted to its single-ref "
            "read form — no write form (e.g. no second ref argument)"
        )


def _check_remote_read_form(args: "list[str]") -> None:
    rest = args[1:]
    if not rest:
        raise ValueError("gitevidence.run_git: git remote requires a read-form argument")
    if rest[0] in ("-v", "--verbose"):
        if len(rest) > 1:
            raise ValueError("gitevidence.run_git: git remote -v takes no further arguments")
        return
    if rest[0] == "get-url":
        sub = rest[1:]
        positional = []
        for arg in sub:
            if arg.startswith("-"):
                if arg not in _REMOTE_GET_URL_ALLOWED_FLAGS:
                    raise ValueError(f"gitevidence.run_git: git remote get-url flag not allowed: {arg!r}")
            else:
                positional.append(arg)
        if len(positional) != 1:
            raise ValueError("gitevidence.run_git: git remote get-url requires exactly one remote name")
        return
    if rest[0] == "show":
        sub = rest[1:]
        positional = []
        for arg in sub:
            if arg.startswith("-"):
                if arg not in _REMOTE_SHOW_ALLOWED_FLAGS:
                    raise ValueError(f"gitevidence.run_git: git remote show flag not allowed: {arg!r}")
            else:
                positional.append(arg)
        if len(positional) != 1:
            raise ValueError("gitevidence.run_git: git remote show requires exactly one remote name")
        return
    raise ValueError(
        "gitevidence.run_git: git remote is restricted to its read forms "
        "(-v, get-url, show) — no write form (add/remove/rename/set-url/prune)"
    )


def _check_allowed(args: "list[str]") -> None:
    if not args or not isinstance(args[0], str) or args[0].startswith("-"):
        raise ValueError(
            "gitevidence.run_git requires a read-only subcommand as args[0]; "
            "leading options (including global options like -c) are never allowed"
        )
    subcommand = args[0]
    if subcommand not in _ALLOWED_SUBCOMMANDS:
        raise ValueError(
            f"gitevidence.run_git refuses subcommand not on the read-only allowlist: {subcommand!r}"
        )
    # --output[=<path>] lets diff-family commands (diff/log/show) WRITE an
    # arbitrary file anywhere on disk regardless of which subcommand is
    # otherwise allowed -- refuse it in any position, for every subcommand.
    for arg in args:
        if isinstance(arg, str) and arg.startswith("--output"):
            raise ValueError(
                f"gitevidence.run_git refuses {arg!r}: --output writes to an arbitrary file path"
            )
    if subcommand == "config":
        _check_config_read_form(args)
    elif subcommand == "symbolic-ref":
        _check_symbolic_ref_read_form(args)
    elif subcommand == "remote":
        _check_remote_read_form(args)


def run_git(repo: str, args: "list[str]", *, timeout: "float | None" = None) -> "tuple[int, str, str]":
    """Run a read-only git subcommand against `repo` and return
    `(returncode, stdout, stderr)`.

    Never enables the subprocess shell. Raises `ValueError` — before ever invoking git —
    if `args` is not a read-only allowlisted invocation (see module
    docstring). Never raises for a timeout or a missing `git` binary — both
    come back as `(-1, "", <reason>)` so a caller can treat every outcome
    uniformly.

    `timeout` defaults to `config.get("git.subprocess_timeout_seconds", 2)`,
    resolved fresh on every call when not given explicitly.
    """
    _check_allowed(args)
    if timeout is None:
        timeout = config.get("git.subprocess_timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
    try:
        result = subprocess.run(
            ["git", *_SAFE_GLOBAL_OPTIONS, "-C", repo, *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"git timeout after {timeout}s: {' '.join(args)}"
    except FileNotFoundError as exc:
        return -1, "", f"git executable not found: {exc}"
    except OSError as exc:
        return -1, "", f"git invocation failed: {exc}"


def repo_root(path: str) -> "str | None":
    """The absolute top-level directory of the repo containing `path`, or
    None if `path` is not inside a git repo (or git is unavailable)."""
    rc, out, _err = run_git(path, ["rev-parse", "--show-toplevel"])
    if rc != 0:
        return None
    return out.strip() or None


def current_branch(repo: str) -> "str | None":
    """The current branch name, or None if HEAD is detached, `repo` is not a
    git repo, or git is unavailable."""
    rc, out, _err = run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    if rc != 0:
        return None
    branch = out.strip()
    if not branch or branch == "HEAD":
        return None
    return branch


def remotes(repo: str) -> "list[dict]":
    """`[{"name": ..., "url": ...}, ...]` for every configured remote, with
    any embedded URL credentials redacted (Ruling 15)."""
    rc, out, _err = run_git(repo, ["remote", "-v"])
    if rc != 0:
        return []
    seen: "dict[str, str]" = {}
    order: "list[str]" = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name, url = parts[0], parts[1]
        if name not in seen:
            order.append(name)
        seen[name] = url
    result = []
    for name in order:
        redacted_url, _fired = redact.redact(seen[name])
        result.append({"name": name, "url": redacted_url})
    return result


def default_branch(repo: str) -> "str | None":
    """The repo's default branch: origin's HEAD symref if one is set,
    otherwise `main` or `master` if either exists locally, else None."""
    rc, out, _err = run_git(repo, ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"])
    if rc == 0 and out.strip():
        ref = out.strip()
        return ref.rsplit("/", 1)[-1]
    for candidate in ("main", "master"):
        # "show-ref" is not on the read-only allowlist; `rev-parse --verify`
        # is an equivalent existence check using an already-allowed command.
        rc, _out, _err = run_git(repo, ["rev-parse", "--verify", "--quiet", f"refs/heads/{candidate}"])
        if rc == 0:
            return candidate
    return None


def identities(repo: str) -> dict:
    """`{"name": ..., "email": ...}` from the repo's (or global, per git's
    own fallback) `user.name`/`user.email` config. Either key is None if
    unset."""
    result = {}
    for key in ("name", "email"):
        rc, out, _err = run_git(repo, ["config", f"user.{key}"])
        result[key] = out.strip() if rc == 0 and out.strip() else None
    return result


def _split_record_tail(chunk: str) -> "tuple[str, str | None]":
    """Split the text following a `\\x1e` record separator into
    (`--name-only` file-list text, next commit's raw metadata text or None).

    Framing (verified empirically against real `git log --pretty=... --name-
    only` output): each commit's `\\x1e`-terminated metadata is immediately
    followed by a newline, its file list, a blank line, then the next
    commit's metadata — so a plain split on the first blank line separates
    the two cleanly. The final chunk has no following commit, so the second
    element is None.
    """
    halves = chunk.split("\n\n", 1)
    files_text = halves[0]
    next_metadata = halves[1] if len(halves) > 1 else None
    return files_text, next_metadata


def commits_since(repo: str, since_iso: str, *, authors: "list[str] | None" = None,
                   limit: "int | None" = None, until_iso: "str | None" = None) -> "list[dict]":
    """Commits on the current branch since `since_iso` (an ISO-8601
    timestamp), newest first, each as:
    `{"sha", "short_sha", "author_email", "author_name", "authored_at",
      "subject", "body", "files": [...]}`.

    `authors`, when given, restricts to commits by any of those author
    emails/names (git's own `--author` OR semantics). Bounded to `limit`
    commits, which defaults to
    `config.get("git.max_commits_per_reconcile", 50)` when not given
    explicitly (Global Constraints: git evidence collection is bounded).

    `until_iso` adds an upper bound (`--until`), which is **inclusive** —
    verified against real git: `--until=<t>` returns the commit at exactly
    `t`. A caller paging backwards through a long history therefore re-sees
    its boundary commit each pass and must dedupe (which
    `sessions.reconcile_git` does via the commit sha idempotency key).
    Note that `--max-count` always selects the **newest** commits in the
    window — also verified: `--reverse --max-count=N` returns the newest N
    printed backwards, NOT the oldest N — so `until_iso` is the only way to
    reach commits older than a full window.
    """
    if limit is None:
        limit = config.get("git.max_commits_per_reconcile", _DEFAULT_MAX_COMMITS)

    args = ["log", f"--since={since_iso}"]
    if until_iso:
        args.append(f"--until={until_iso}")
    for author in authors or []:
        args.append(f"--author={author}")
    args.append(f"--pretty={_COMMIT_LOG_FORMAT}")
    args.append("--name-only")
    args.append(f"--max-count={limit}")

    rc, out, _err = run_git(repo, args)
    if rc != 0 or not out.strip("\n"):
        return []

    parts = out.split(_RECORD_SEP)
    # parts[0] is the first commit's metadata cleanly; each subsequent part
    # starts with the previous commit's file list (see _split_record_tail).
    metadata_texts = [parts[0]]
    file_blocks = []
    for chunk in parts[1:]:
        files_text, next_metadata = _split_record_tail(chunk)
        file_blocks.append(files_text)
        if next_metadata is not None:
            metadata_texts.append(next_metadata)

    rows = []
    for meta_text, files_text in zip(metadata_texts, file_blocks):
        fields = meta_text.split(_FIELD_SEP, 5)
        if len(fields) < 6:
            continue
        sha, author_name, author_email, authored_at, subject, body = fields
        files = [f for f in files_text.lstrip("\n").split("\n") if f]
        rows.append({
            "sha": sha,
            "short_sha": sha[:7],
            "author_email": author_email,
            "author_name": author_name,
            "authored_at": authored_at,
            "subject": subject,
            "body": body.strip("\n"),
            "files": files,
        })
    return rows
