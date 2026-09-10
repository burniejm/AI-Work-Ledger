"""Hook runtime dispatch (Ruling 26).

`worklog hook <event>` (the CLI entry, registered in `cli.py`) reads hook
JSON from stdin and calls `handle(event, payload)`. `handle` NEVER raises
and ALWAYS returns 0 -- the Global Constraints forbid a hook exiting
non-zero toward Claude Code (exit 2 blocks the user). Every dispatched
handler runs inside a single top-level `try/except BaseException`; a
failure is logged (redacted) to `logs/hooks.log` and, best-effort, also
recorded as a failed `capture_checkpoints` row so `worklog doctor`'s
`checkpoint_failures` check has something to show.
"""
import hashlib
import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler

from work_ledger import config, entities, gitevidence, paths, redact, timeutil
from work_ledger.capture import checkpoints, events, sessions
from work_ledger.db import connection as dbc
from work_ledger.db import migrations
from work_ledger.jira import refresh as jira_refresh

_LOGGER_NAME = "work_ledger.hooks"
_LOG_MAX_BYTES = 1_000_000
_LOG_BACKUP_COUNT = 5


def _get_logger() -> logging.Logger:
    """A module-level logger writing to logs/hooks.log, rotated at 1 MB x 5.

    Recreated whenever the resolved log path changes (WORK_LEDGER_HOME
    differs between tests/processes), so a stale handler never keeps
    writing into an already-deleted temp directory.
    """
    paths.ensure_dirs()
    log_path = str(paths.log_dir() / "hooks.log")
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if getattr(logger, "_work_ledger_log_path", None) != log_path:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
        handler = RotatingFileHandler(log_path, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUP_COUNT)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger._work_ledger_log_path = log_path
    return logger


def log_exception(exc: BaseException, context: str) -> None:
    """Append one redacted line to logs/hooks.log. Never raises."""
    try:
        line = f"{timeutil.utc_now_iso()} {context}: {exc.__class__.__name__}: {exc}"
        redacted_line, _ = redact.redact(line)
        _get_logger().info(redacted_line)
    except BaseException:
        pass


def _content_discriminator(payload) -> str:
    """A stable, content-based fallback discriminator for a `post-tool-use`
    payload that lacks a `tool_use_id` -- replaying the identical payload
    still dedupes, while distinct payloads get distinct keys. NOT used for
    the marker events (see `_handle_marker`): a real Claude Code payload
    always carries `tool_use_id` for tool calls, so this only matters for
    defensively-malformed input."""
    try:
        encoded = json.dumps(payload, sort_keys=True, default=str)
    except Exception:
        encoded = repr(payload)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _open_hook_conn():
    conn = dbc.connect(hook_mode=True)
    migrations.ensure_current(conn)
    return conn


def _commit_subject(tool_response: dict) -> "str | None":
    """Best-effort commit subject line, e.g. from
    "[feature/ab-76 a1b2c3d] AB-76 fix upload retry" -> "AB-76 fix upload retry"."""
    stdout = (tool_response or {}).get("stdout") or ""
    lines = stdout.splitlines()
    first_line = lines[0] if lines else ""
    if first_line.startswith("[") and "]" in first_line:
        return first_line.split("]", 1)[1].strip() or None
    return None


def _bump_drop_counter(session_id, reason: str) -> None:
    """Ruling 24: a dropped PostToolUse event still bumps a per-session
    signal, without growing the append-only capture_checkpoints table at
    full tool-call rate. Accumulated in a small best-effort JSON file under
    state/ for Task 25's Stop handler to fold into its checkpoint detail.
    Never raises -- a missing/unwritable state dir must not break the hook.
    """
    if not session_id:
        return
    try:
        paths.ensure_dirs()
        state_path = paths.state_dir() / f"drops-{session_id}.json"
        data = {}
        if state_path.exists():
            try:
                data = json.loads(state_path.read_text())
            except Exception:
                data = {}
        data["count"] = int(data.get("count", 0)) + 1
        data["last_reason"] = reason
        data["last_at"] = timeutil.utc_now_iso()
        tmp_path = state_path.with_name(state_path.name + ".tmp")
        tmp_path.write_text(json.dumps(data))
        os.replace(tmp_path, state_path)
    except Exception:
        pass


def _session_context(conn, session_id) -> "tuple[str | None, str | None, str | None]":
    """`(repository_id, git_branch, cwd)` recorded on the session at
    SessionStart, so a later event can inherit the repository the session is
    in -- a PostToolUse payload carries neither `repository_id` (it is our
    own entity id) nor, reliably, `git_branch`. One indexed lookup on a
    unique index; a session we have never seen reads as all-None.

    This closes the Task 22 review pointer that `source_events.repository_id`
    and `git_branch` were never populated from hooks.
    """
    try:
        row = sessions.get(conn, session_id)
    except Exception:
        return None, None, None
    if row is None:
        return None, None, None
    return row["repository_id"], row["git_branch"], row["cwd"]


def _handle_post_tool_use(payload: dict) -> None:
    session_id = payload.get("session_id")
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    tool_response = payload.get("tool_response") or {}
    tool_use_id = payload.get("tool_use_id")
    cwd = payload.get("cwd")
    git_branch = payload.get("git_branch")

    keep, reason = events.is_interesting(tool_name, tool_input, tool_response)
    if not keep:
        _bump_drop_counter(session_id, reason)
        return

    now = timeutil.utc_now_iso()
    discriminator = tool_use_id or _content_discriminator(payload)

    conn = _open_hook_conn()
    try:
        repository_id, session_branch, session_cwd = _session_context(conn, session_id)
        git_branch = git_branch or session_branch
        cwd = cwd or session_cwd
        # This event IS evidence the session is alive: advance last_seen_at
        # (and retract a lazy idle_finalized guess) so a long turn between
        # SessionStart and Stop cannot be swept out from under a session
        # that is plainly still working. The connection is already open and
        # this is one indexed UPDATE.
        sessions.touch(conn, session_id, occurred_at=now, hook_mode=True)
        idem = events.idempotency_key(session_id, "post_tool_use", discriminator)
        events.append_source_event(
            conn, session_id=session_id, event_type="tool_use", occurred_at=now,
            payload={"tool_name": tool_name, "tool_input": tool_input,
                     "tool_response": tool_response, "cwd": cwd},
            hook_event="post_tool_use", tool_name=tool_name, tool_use_id=tool_use_id,
            cwd=cwd, git_branch=git_branch, repository_id=repository_id,
            is_sidechain=bool(payload.get("isSidechain")),
            idem=idem, hook_mode=True,
        )

        if tool_name == "Bash":
            command = tool_input.get("command") or ""
            if events.GIT_COMMIT_RE.match(command):
                # Ruling 30(g) / DESIGN §4.2: a derived git_commit_signal row,
                # its own idempotency key so replay yields exactly one of each.
                commit_idem = events.idempotency_key(session_id, "git_commit_signal", discriminator)
                events.append_source_event(
                    conn, session_id=session_id, event_type="git_commit_signal", occurred_at=now,
                    payload={
                        "command": command,
                        "subject": _commit_subject(tool_response),
                        "exit_code": tool_response.get("exit_code"),
                    },
                    hook_event="post_tool_use", tool_name=tool_name, tool_use_id=tool_use_id,
                    cwd=cwd, git_branch=git_branch, repository_id=repository_id,
                    idem=commit_idem, hook_mode=True,
                )

        checkpoints.checkpoint(
            conn, session_id=session_id, hook_event="post_tool_use",
            key=f"post_tool_use:{session_id}:{discriminator}", status="ok", started_at=now,
            hook_mode=True,
        )
    finally:
        conn.close()


def _handle_marker(hook_event_name: str, event_type: str, payload: dict) -> None:
    """A minimal marker source_event + checkpoint for hook events whose full
    downstream processing (transcript reconciliation, Stage A/B, ...) lands
    in later tasks. See source_events.event_type's documented enum.

    Discriminator: these payloads carry no natural per-invocation id (no
    `tool_use_id`), so a content hash would collapse two genuinely distinct
    firings with identical content -- e.g. two SubagentStop calls in the
    same session, or the same prompt text submitted twice -- into a single
    row forever. Millisecond-precision `occurred_at` is used instead: two
    real invocations are essentially never issued in the same millisecond
    (each does several DB round-trips), so this reliably distinguishes
    them. The tradeoff is documented and accepted: an exact byte-for-byte
    *replay* of one of these hook deliveries (not a realistic risk for
    these event types, unlike PostToolUse's tool_use_id-keyed replays)
    could in principle land in the same millisecond and create a second
    row -- acceptable, since Claude Code does not replay these hooks.
    """
    session_id = payload.get("session_id")
    now = timeutil.utc_now_iso()
    discriminator = payload.get("tool_use_id") or now
    conn = _open_hook_conn()
    try:
        repository_id, session_branch, session_cwd = _session_context(conn, session_id)
        sessions.touch(conn, session_id, occurred_at=now, hook_mode=True)   # see _handle_post_tool_use
        idem = events.idempotency_key(session_id, event_type, discriminator)
        events.append_source_event(
            conn, session_id=session_id, event_type=event_type, occurred_at=now,
            payload=payload, hook_event=hook_event_name,
            cwd=payload.get("cwd") or session_cwd,
            git_branch=payload.get("git_branch") or session_branch,
            repository_id=repository_id, idem=idem, hook_mode=True,
        )
        checkpoints.checkpoint(
            conn, session_id=session_id, hook_event=hook_event_name,
            key=f"{hook_event_name}:{session_id}:{discriminator}", status="ok", started_at=now,
            hook_mode=True,
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Session lifecycle hooks (Task 25). Ruling 8: the writes below are inside
# the hook allowlist, and each optional block is separately guarded --
# `session-start`'s repository upsert (allowlist item (b)), `session-start`'s
# bounded REST Jira refresh (item (d), Task 35), and `stop`'s Stage A
# projection (item (c)) each get their own try/except BaseException and
# their own slice of the hook's time budget, so a failure in any one of them
# can never prevent the mandatory work (the session_start source event, the
# stop checkpoint) from happening. A block that failed or was skipped for
# budget is named in the checkpoint's detail, and the checkpoint's status
# drops to 'partial'.
# --------------------------------------------------------------------------

# Shared wall-clock budget for one hook invocation. Deliberately far below
# the 10 s timeouts hooks/hooks.json configures for SessionStart/Stop: the
# Global Constraints ask a hook to return in ~200 ms typically, and every
# block this bounds is resumable (reconciliation advances a watermark;
# Stage A is keyed by session, not by time), so cutting one short costs
# latency on the next hook rather than data.
_HOOK_BUDGET_SECONDS = 1.5


class _Budget:
    """A monotonic deadline for one hook invocation. `deadline` is handed to
    long-running callees (e.g. `sessions.reconcile_transcript`) so they can
    stop mid-pass rather than only being skipped up front."""

    def __init__(self, seconds: float = _HOOK_BUDGET_SECONDS):
        self.deadline = time.monotonic() + seconds

    def expired(self) -> bool:
        return time.monotonic() >= self.deadline


class _Blocks:
    """Bookkeeping for the optional blocks in one hook invocation.

    `deferred` and `failed` are deliberately separate: a block skipped
    because the budget ran out is normal, expected, and self-healing (every
    deferred block is resumable on the next hook), so it must not colour the
    checkpoint `partial` and make `worklog doctor` cry wolf. A block that
    *raised* is a real problem and does."""

    def __init__(self):
        self.deferred: "list[str]" = []
        self.failed: "list[str]" = []

    def status(self) -> str:
        return "partial" if self.failed else "ok"

    def notes(self) -> "list[str]":
        notes = []
        if self.deferred:
            notes.append(f"deferred={','.join(self.deferred)}")
        if self.failed:
            notes.append(f"failed_blocks={','.join(self.failed)}")
        return notes


def _guarded(name: str, budget: _Budget, fn, blocks: _Blocks, default=None):
    """Run one optional block. Returns `default` -- recording the block as
    deferred when the budget is gone, or as failed when it raised -- and
    never propagates, because the caller's remaining work is more important
    than this block's result."""
    if budget.expired():
        blocks.deferred.append(name)
        return default
    try:
        return fn()
    except BaseException as exc:      # Ruling 8: per-block isolation
        log_exception(exc, f"hook-block:{name}")
        blocks.failed.append(name)
        return default


def _stage_note(result) -> str:
    """How a stage's return value reads in a checkpoint detail. `None` means
    the guarded block failed (already named in `blocks.failed`); a dict carrying
    `ran: False` means the stage declined (not yet implemented, rate
    limited, nothing to do); anything else means it ran."""
    if result is None:
        return "failed"
    if isinstance(result, dict) and result.get("ran", True) is False:
        return "skipped"
    return "ran"


def _detail(parts: list, blocks: _Blocks) -> str:
    return " ".join(str(part) for part in list(parts) + blocks.notes())[:500]


def _drain_drop_counter(session_id) -> int:
    """Read (and clear) the per-session dropped-tool-call counter written by
    `_bump_drop_counter`, so the Stop checkpoint can report how many noise
    events were filtered since the last Stop (Ruling 24). Never raises; a
    missing or unreadable file simply reads as zero."""
    if not session_id:
        return 0
    try:
        state_path = paths.state_dir() / f"drops-{session_id}.json"
        if not state_path.exists():
            return 0
        try:
            count = int(json.loads(state_path.read_text()).get("count", 0))
        except Exception:
            count = 0
        state_path.unlink()          # reset: the next Stop reports its own interval
        return count
    except Exception:
        return 0


def _resolve_repository(conn, cwd) -> "tuple[str | None, str | None]":
    """Resolve `cwd` to a `repositories` row, returning
    `(repository_id, branch)`. Kept to two `git` reads (repo root, current
    branch) because it runs inside the SessionStart hook budget -- richer
    repository metadata (remotes, default branch) is not needed to attribute
    events and can be filled in by a non-hook code path.

    Runs inside `_guarded`: every failure mode here is a caught exception or
    a `None`, never an exception that reaches the caller.
    """
    if not cwd:
        return None, None
    root = gitevidence.repo_root(cwd)
    if not root:
        return None, None
    branch = gitevidence.current_branch(root)
    repository_id = entities.upsert_repository(conn, root, hook_mode=True)
    return repository_id, branch


def _handle_session_start(payload: dict) -> None:
    session_id = payload.get("session_id")
    cwd = payload.get("cwd")
    now = timeutil.utc_now_iso()
    budget = _Budget()
    blocks = _Blocks()

    conn = _open_hook_conn()
    try:
        # Allowlist item (b): repositories / repository_mappings. Guarded --
        # a repo git cannot read must not cost us the session row or the
        # session_start event, so a failure here simply yields (None, None).
        # Resolved BEFORE the session upsert so there is exactly one write
        # to the sessions row for this hook rather than an insert followed
        # by a repository-linking update.
        repository_id, branch = _guarded(
            "repository_upsert", budget, lambda: _resolve_repository(conn, cwd),
            blocks, default=(None, None),
        )

        sessions.upsert(
            conn, session_id=session_id, cwd=cwd,
            transcript_path=payload.get("transcript_path"),
            git_branch=branch or payload.get("git_branch"),
            repository_id=repository_id,
            claude_version=payload.get("version"),
            occurred_at=now, hook_mode=True,
        )

        # DESIGN gap G1: SessionEnd is not guaranteed to fire, so the next
        # SessionStart closes out anything left idle. Runs after our own
        # upsert, so this session's fresh last_seen_at keeps it out of its
        # own sweep.
        finalized = _guarded(
            "finalize_stale", budget,
            lambda: sessions.finalize_stale(
                conn, now=now, idle_minutes=sessions.idle_finalize_minutes(), hook_mode=True),
            blocks, default=[],
        ) or []

        scope = _guarded(
            "resolve_scope", budget,
            lambda: entities.resolve_scope_for_path(conn, cwd) if cwd else None,
            blocks,
        )

        # Code review CRITICAL 1: the mandatory session_start source event is
        # written BEFORE the (bounded-but-still-blocking-on-one-in-flight-
        # request) Jira refresh block below, not after. `sessions.upsert`
        # above already recorded the session; this is the second piece of
        # "the session was captured" -- if the host kills a slow-but-
        # responsive hook process partway through the Jira refresh, this
        # event (unlike the checkpoint written further down) has already
        # landed. The checkpoint stays after the refresh block so its detail
        # can still report the refresh's outcome -- it is bookkeeping, not
        # the capture itself.
        idem = events.idempotency_key(session_id, "session_start", now)
        events.append_source_event(
            conn, session_id=session_id, event_type="session_start", occurred_at=now,
            payload={
                "source": payload.get("source"),
                "cwd": cwd,
                "transcript_path": payload.get("transcript_path"),
                "permission_mode": payload.get("permission_mode"),
                "version": payload.get("version"),
                "scope": {
                    "client_id": scope[0], "project_id": scope[1],
                    "confidence": scope[2], "reason": scope[3],
                } if scope else None,
            },
            hook_event="session_start", cwd=cwd, git_branch=branch or payload.get("git_branch"),
            repository_id=repository_id, idem=idem, hook_mode=True,
        )

        # Allowlist item (d), Ruling 8: bounded REST Jira refresh
        # (work_items.jira_*, connections.health_*, contradictions). Separately
        # guarded and bounded precisely so a Jira outage can never affect the
        # checkpoint's mandatory 'ok' work (Global Constraints: a failed Jira
        # connection never blocks local capture). Bounded two ways: `limit`
        # (config.get("jira.max_refresh_on_start")) caps how many issues are
        # fetched, and `deadline=budget.deadline` (this hook invocation's own
        # wall-clock ceiling) is checked between issues/connections so a
        # slow-but-responsive Jira cannot run the block past the hook's
        # overall time budget (code review CRITICAL 1) -- the one remaining
        # exposure is a single in-flight request's own timeout, which cannot
        # be preempted from outside. MCP connections are never contacted from
        # here (DESIGN gap G2) -- `refresh_all_rest` only ever refreshes
        # `adapter='jira-rest'` rows. `hook_mode=True` bounds this block's own
        # writes to the hook's shortened busy-retry schedule too.
        jira_result = _guarded(
            "jira_refresh", budget,
            lambda: jira_refresh.refresh_all_rest(
                conn, limit=config.get("jira.max_refresh_on_start"),
                deadline=budget.deadline, hook_mode=True),
            blocks,
        )

        jira_note = _stage_note(jira_result)
        if isinstance(jira_result, dict) and jira_result.get("partial"):
            jira_note += "(partial)"  # deadline cut the refresh short (CRITICAL 1)
        checkpoints.checkpoint(
            conn, session_id=session_id, hook_event="session_start",
            key=f"session_start:{session_id}:{now}", status=blocks.status(),
            detail=_detail([f"idle_finalized={len(finalized)}",
                            f"repository={'yes' if repository_id else 'no'}",
                            f"jira_refresh={jira_note}"], blocks),
            started_at=now, hook_mode=True,
        )
    finally:
        conn.close()


def _handle_stop(payload: dict) -> None:
    session_id = payload.get("session_id")
    cwd = payload.get("cwd")
    now = timeutil.utc_now_iso()
    budget = _Budget()
    blocks = _Blocks()

    conn = _open_hook_conn()
    try:
        if payload.get("stop_hook_active"):
            # Claude Code is already continuing from a Stop hook. Doing the
            # reconciliation work again here would duplicate effort inside a
            # loop we did not start and cannot see the end of -- record that
            # we deliberately stood down and return.
            checkpoints.checkpoint(
                conn, session_id=session_id, hook_event="stop",
                key=f"stop:{session_id}:{now}", status="skipped",
                detail="stop_hook_active=true", started_at=now, hook_mode=True,
            )
            return

        # Stop can be the first hook we see for a session (a resumed session,
        # or a plugin installed mid-session), so create the row if needed.
        sessions.upsert(
            conn, session_id=session_id, cwd=cwd,
            transcript_path=payload.get("transcript_path"),
            git_branch=payload.get("git_branch"),
            occurred_at=now, hook_mode=True,
        )

        transcript_events = _guarded(
            "reconcile_transcript", budget,
            lambda: sessions.reconcile_transcript(
                conn, session_id, hook_mode=True, deadline=budget.deadline),
            blocks, default=0,
        )
        git_events = _guarded(
            "reconcile_git", budget,
            lambda: sessions.reconcile_git(conn, session_id, hook_mode=True),
            blocks, default=0,
        )
        # Allowlist item (c): Stage A's projection into activities and
        # friends. Guarded -- Stage A failing must not cost us the checkpoint.
        stage_a = _guarded(
            "stage_a", budget,
            lambda: sessions.run_stage_a(conn, session_id, hook_mode=True),
            blocks, default=None,
        )

        parts = [f"transcript_events={transcript_events}",
                 f"git_events={git_events}",
                 f"dropped={_drain_drop_counter(session_id)}",
                 f"stage_a={_stage_note(stage_a)}"]
        # A truncated git window means older commits in the same window were
        # dropped by --max-count and a later pass still owes us that tail.
        # Surfaced here so it is visible in `worklog sessions` / doctor
        # rather than only inferable from a watermark row.
        git_state = _guarded("git_watermark", budget,
                             lambda: sessions.git_watermark(conn, session_id), blocks)
        if git_state and git_state.get("truncated"):
            parts.append("git_backlog=pending")

        checkpoints.checkpoint(
            conn, session_id=session_id, hook_event="stop",
            key=f"stop:{session_id}:{now}", status=blocks.status(),
            detail=_detail(parts, blocks), started_at=now, hook_mode=True,
        )
    finally:
        conn.close()

    # Stage B is spawned detached and is ALWAYS the last statement: the hook
    # never blocks on it (Ruling 8). A missing infer.stage_b (before Task 29)
    # is a documented no-op.
    try:
        from work_ledger.infer import stage_b
    except ImportError as exc:
        if getattr(exc, "name", None) not in ("work_ledger.infer", "work_ledger.infer.stage_b"):
            raise
        return
    stage_b.spawn(session_id)


def _handle_session_end(payload: dict) -> None:
    """Marker event plus `sessions.finalize`, and nothing else -- SessionEnd
    shares a tight budget with everything else Claude Code runs at teardown,
    and there is nothing here that a later `reprocess` could not redo."""
    session_id = payload.get("session_id")
    now = timeutil.utc_now_iso()
    raw_reason = payload.get("reason")
    # `sessions.end_reason`'s documented set is session_end|idle_finalized|
    # clear|unknown; Claude Code's own reason vocabulary is wider and
    # undocumented, so it is preserved verbatim in the marker event's
    # payload and only 'clear' is mapped through.
    reason = "clear" if raw_reason == "clear" else "session_end"

    conn = _open_hook_conn()
    try:
        repository_id, session_branch, session_cwd = _session_context(conn, session_id)
        idem = events.idempotency_key(session_id, "session_end", now)
        events.append_source_event(
            conn, session_id=session_id, event_type="session_end", occurred_at=now,
            payload=payload, hook_event="session_end",
            cwd=payload.get("cwd") or session_cwd,
            git_branch=payload.get("git_branch") or session_branch,
            repository_id=repository_id, idem=idem, hook_mode=True,
        )
        sessions.finalize(conn, session_id, ended_at=now, reason=reason, hook_mode=True)
        checkpoints.checkpoint(
            conn, session_id=session_id, hook_event="session_end",
            key=f"session_end:{session_id}:{now}", status="ok",
            detail=f"end_reason={reason}", started_at=now, hook_mode=True,
        )
    finally:
        conn.close()


# Ruling 25: v1 registers exactly these events (PostToolUseFailure/PostCompact
# dropped). post-tool-use and the session lifecycle events get full
# processing; user-prompt-submit / pre-compact / subagent-stop are markers
# whose richer processing rides the Stop reconciliation pass.
_DISPATCH = {
    "post-tool-use": _handle_post_tool_use,
    "user-prompt-submit": lambda payload: _handle_marker("user_prompt_submit", "prompt", payload),
    "pre-compact": lambda payload: _handle_marker("pre_compact", "compact", payload),
    "subagent-stop": lambda payload: _handle_marker("subagent_stop", "subagent_stop", payload),
    "session-end": _handle_session_end,
    "session-start": _handle_session_start,
    "stop": _handle_stop,
}


def _validate_payload(event: str, payload: dict) -> "str | None":
    """Cheap, DB-free sanity check run before any handler touches SQLite.
    Returns a problem description, or None when the payload is usable."""
    if not payload.get("session_id"):
        return "missing session_id"
    if event == "post-tool-use" and not payload.get("tool_name"):
        return "missing tool_name"
    return None


def _record_failure(event: str, payload, exc: BaseException) -> None:
    """Best-effort: also leave a failed capture_checkpoints row so `worklog
    doctor`'s checkpoint_failures check has something to show. Wrapped by
    its only caller in a try/except BaseException, so it must never be
    relied upon to raise cleanly -- but it also never lets an unredacted
    exception string reach SQLite (Global Constraints: secrets never in
    diagnostic output/logs)."""
    session_id = (payload or {}).get("session_id")
    now = timeutil.utc_now_iso()
    redacted_detail, _ = redact.redact(str(exc))
    conn = dbc.connect(hook_mode=True)
    try:
        checkpoints.checkpoint(
            conn, session_id=session_id, hook_event=event,
            key=f"{event}:{session_id}:{_content_discriminator(payload or {})}:error",
            status="failed", detail=redacted_detail[:500], started_at=now,
            hook_mode=True,
        )
    finally:
        conn.close()


def handle(event: str, payload: dict) -> int:
    """Dispatch `event` to its handler. Always returns 0 -- Global
    Constraints forbid a hook exiting non-zero toward Claude Code."""
    if os.environ.get("WORK_LEDGER_NESTED"):
        return 0

    payload = payload or {}

    fn = _DISPATCH.get(event)
    if fn is None:
        # Unrecognized event name: log only, no checkpoint (nothing to
        # attribute a failed checkpoint to, and it isn't a data-loss risk).
        try:
            log_exception(ValueError(f"unrecognized hook event: {event!r}"), f"hook:{event}")
        except BaseException:
            pass
        return 0

    problem = _validate_payload(event, payload)
    if problem is not None:
        # Garbage/incomplete payload: log and bail before opening a
        # connection at all -- no source_events row, no checkpoint.
        try:
            log_exception(ValueError(problem), f"hook:{event}")
        except BaseException:
            pass
        return 0

    try:
        fn(payload)
    except BaseException as exc:
        log_exception(exc, f"hook:{event}")
        try:
            _record_failure(event, payload, exc)
        except BaseException:
            pass
    return 0
