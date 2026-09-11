"""Session lifecycle, lazy finalization, reconciliation, and reprocess
(Task 25; Spec §20.2; DESIGN gaps G1/G4/G5).

A "session" here is one Claude Code session id. The `sessions` row is the
spine every later stage hangs off: it carries the transcript location, the
resolved repository, the transcript watermark, and the start/end times that
bound git reconciliation.

Three properties this module exists to guarantee:

1. **Lazy finalization (G1).** Claude Code's `SessionEnd` hook is not
   guaranteed to fire (a crashed terminal, a killed process, a machine that
   slept). Nothing here runs on a timer -- `finalize_stale` is called by the
   *next* `SessionStart`, which closes out anything idle past
   `session.idle_finalize_minutes` with `end_reason='idle_finalized'`.

2. **Reconciliation is idempotent (Spec §20.2).** The transcript pass is
   keyed by record `uuid` and the git pass by commit `sha`, both through
   `capture.events.append_source_event`'s `INSERT OR IGNORE` on
   `idempotency_key`. Running either twice -- or running `reprocess` any
   number of times -- creates no second row. `transcript_watermark` is an
   optimization on top of that, not the correctness mechanism: if the
   watermark is lost or the file rotates, re-reading from the top still
   produces exactly the same rows.

3. **Bounded work.** A hook calls into here, so every pass has a ceiling:
   at most `_MAX_RECORDS_PER_RECONCILE` transcript records per call, an
   optional wall-clock `deadline` the caller supplies from its own hook
   budget, and at most `git.max_commits_per_reconcile` commits (enforced
   inside `gitevidence.commits_since`). Hitting any ceiling is not a
   failure: the watermark advances over what was processed, so the next
   call resumes exactly where this one stopped.

**Transcript record -> `source_events.event_type` mapping.** The
`event_type` set is documented in migration `003_capture.sql`, migration
files are checksummed (`db.migrations.ChecksumMismatch`), and the set has no
"assistant message" member -- so a conversational turn carrying no tool
blocks is stored as `prompt` regardless of role, with `payload.role`
preserving the user/assistant distinction losslessly. A record carrying
`tool_use` or `tool_result` blocks is stored as `tool_use`. Adding a
dedicated member is a schema-comment change and therefore a migration, left
to a later task.
"""
import json
import time
from datetime import timedelta, timezone

from work_ledger import config, entities, gitevidence, timeutil, transcript
from work_ledger.capture import checkpoints, events
from work_ledger.db import connection

# Ceiling on transcript records ingested per `reconcile_transcript` call.
# The watermark advances to the last record examined, so a very large
# transcript is drained across successive Stop hooks instead of stalling one.
_MAX_RECORDS_PER_RECONCILE = 2000

# `hook_event` stamped on rows this module writes, so reconciled evidence is
# distinguishable from evidence delivered by a hook payload.
TRANSCRIPT_HOOK_EVENT = "transcript"
GIT_HOOK_EVENT = "git_reconcile"

# `sessions.end_reason`'s documented set (migration 003_capture.sql). Not a
# CHECK constraint in the schema, so `finalize` enforces it here rather than
# letting an unrecognized reason reach the column.
END_REASONS = ("session_end", "idle_finalized", "clear", "unknown")

# Appended to every UPDATE that records new evidence for a session (`upsert`,
# `touch`). A lazy sweep's `idle_finalized` is a *guess* -- "we have not
# heard from this session in 30 minutes, it is probably over" -- so evidence
# arriving afterwards (a resumed session, a very long turn, a machine waking
# up) retracts it. A real `session_end`/`clear` is an observation, not a
# guess, and is never retracted here. Constant SQL, no interpolated input.
_REOPEN_SETS = (
    "ended_at=CASE WHEN end_reason='idle_finalized' THEN NULL ELSE ended_at END",
    "end_reason=CASE WHEN end_reason='idle_finalized' THEN NULL ELSE end_reason END",
)


def _to_utc_iso(raw) -> "str | None":
    """Normalize an arbitrary ISO-8601 timestamp to the Global-Constraints
    storage form (UTC, `Z` suffix, millisecond precision). Git's `%aI`
    carries a local offset, so commit times must go through here before
    they are stored or compared."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = timeutil.parse_iso(raw).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def get(conn, session_id: str):
    """The `sessions` row for `session_id`, or None."""
    if not session_id:
        return None
    return conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()


def upsert(conn, *, session_id, cwd=None, transcript_path=None, git_branch=None,
           repository_id=None, claude_version=None, occurred_at, hook_mode: bool = False) -> str:
    """Create the `sessions` row on first sight, and always advance
    `last_seen_at`. Returns the row id (stable across calls).

    Every optional field is "only-when-supplied" on an existing row (the
    same pattern as `entities.upsert_repository`): a later call that omits
    `transcript_path` never clears a path an earlier call established.
    `last_seen_at` only ever moves forward, so an out-of-order hook
    delivery cannot make a live session look idle.

    `hook_mode=True` threads through to `entities.current_device` and
    `connection.transaction` so a hook caller gives up on lock contention
    within the hook budget rather than the full non-hook retry schedule.
    """
    if not session_id:
        raise ValueError("sessions.upsert requires a session_id")

    # current_device() opens (and commits) its own transaction, so it must
    # run before this function opens one -- connection.transaction is not
    # reentrant (see entities.py's module docstring).
    device_id = entities.current_device(conn, hook_mode=hook_mode)
    now = timeutil.utc_now_iso()

    with connection.transaction(conn, hook_mode=hook_mode):
        row = conn.execute(
            "SELECT id, last_seen_at FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is not None:
            sets = []
            params = []
            for column, value in (("cwd", cwd), ("transcript_path", transcript_path),
                                  ("git_branch", git_branch), ("repository_id", repository_id),
                                  ("claude_version", claude_version)):
                if value is not None:
                    sets.append(f"{column}=?")
                    params.append(value)
            if occurred_at > (row["last_seen_at"] or ""):
                sets.append("last_seen_at=?")
                params.append(occurred_at)
                # Reopen ONLY alongside a real advance of last_seen_at: an
                # out-of-order delivery carrying an older occurred_at is not
                # fresh evidence and must not retract a sweep's finding.
                sets.extend(_REOPEN_SETS)
            sets.append("updated_at=?")
            params.append(now)
            params.append(row["id"])
            # Column names come only from the literal tuple above -- never
            # from input (Global Constraints: SQL safety).
            conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id=?", params)
            return row["id"]

        from work_ledger import ids  # local import keeps the module import graph flat
        session_row_id = ids.new_id()
        conn.execute(
            "INSERT INTO sessions(id, session_id, transcript_path, cwd, git_branch, repository_id, "
            "claude_version, started_at, last_seen_at, origin_device_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_row_id, session_id, transcript_path, cwd, git_branch, repository_id,
             claude_version, occurred_at, occurred_at, device_id, now, now),
        )
        return session_row_id


def touch(conn, session_id: str, *, occurred_at: str, hook_mode: bool = False) -> None:
    """Advance `last_seen_at` for an existing session, reopening it if a
    lazy sweep had guessed it was over. A no-op when the session is unknown
    (nothing here creates a row -- `upsert` owns that, and it is the only
    caller with the fields to create one properly).

    This is what every mid-session hook calls: without it, `last_seen_at`
    only moved at SessionStart/Stop and a long turn between them could be
    swept out from under a session that was plainly still alive.
    """
    now = timeutil.utc_now_iso()
    with connection.transaction(conn, hook_mode=hook_mode):
        conn.execute(
            f"UPDATE sessions SET last_seen_at=?, {', '.join(_REOPEN_SETS)}, updated_at=? "
            "WHERE session_id=? AND last_seen_at < ?",
            (occurred_at, now, session_id, occurred_at),
        )


def finalize(conn, session_id: str, *, ended_at: str, reason: str,
             hook_mode: bool = False) -> None:
    """Close out a session. Idempotent: a session that already has an
    `ended_at` is left exactly as it was, so a late `SessionEnd` after a
    lazy `idle_finalized` (or a replayed hook) never rewrites history.

    `reason` outside `END_REASONS` is stored as `'unknown'`; callers that
    have a richer reason keep it in the session_end event's payload."""
    if reason not in END_REASONS:
        reason = "unknown"
    now = timeutil.utc_now_iso()
    with connection.transaction(conn, hook_mode=hook_mode):
        conn.execute(
            "UPDATE sessions SET ended_at=?, end_reason=?, last_seen_at=max(last_seen_at, ?), "
            "updated_at=? WHERE session_id=? AND ended_at IS NULL",
            (ended_at, reason, ended_at, now, session_id),
        )


def finalize_stale(conn, *, now: str, idle_minutes: int, hook_mode: bool = False) -> "list[str]":
    """DESIGN gap G1: finalize every live session idle longer than
    `idle_minutes`, with `end_reason='idle_finalized'`. Returns the
    finalized session ids, oldest first.

    This is the whole answer to "SessionEnd is not guaranteed to fire":
    nothing runs on a timer, the next SessionStart sweeps.

    `ended_at` is set to the session's own `last_seen_at`, not to `now`:
    `ended_at` is event time and the session demonstrably stopped producing
    evidence then, while `now` is only when we noticed (Global Constraints:
    event time is always stored separately from ingestion time -- `now`
    lands in `updated_at`).
    """
    try:
        cutoff_dt = timeutil.parse_iso(now) - timedelta(minutes=int(idle_minutes))
    except (ValueError, TypeError):
        return []
    cutoff = _to_utc_iso(cutoff_dt.isoformat())
    if cutoff is None:
        return []

    rows = conn.execute(
        "SELECT session_id, last_seen_at FROM sessions "
        "WHERE ended_at IS NULL AND last_seen_at < ? ORDER BY last_seen_at",
        (cutoff,),
    ).fetchall()
    if not rows:
        return []

    updated_at = timeutil.utc_now_iso()
    with connection.transaction(conn, hook_mode=hook_mode):
        for row in rows:
            conn.execute(
                "UPDATE sessions SET ended_at=?, end_reason='idle_finalized', updated_at=? "
                "WHERE session_id=? AND ended_at IS NULL",
                (row["last_seen_at"], updated_at, row["session_id"]),
            )
    return [row["session_id"] for row in rows]


def _transcript_event_type(summary: dict) -> str:
    """See the module docstring: the documented `event_type` set has no
    assistant-message member, so tool traffic is `tool_use` and every other
    conversational turn is `prompt` (with `payload.role` preserved)."""
    if summary.get("tool_uses") or summary.get("tool_results"):
        return "tool_use"
    return "prompt"


def _resolve_transcript_path(session_row) -> "str | None":
    path = session_row["transcript_path"]
    if path:
        return path
    discovered = transcript.transcript_path_for(session_row["session_id"], session_row["cwd"])
    return str(discovered) if discovered else None


def reconcile_transcript(conn, session_id: str, *, hook_mode: bool = False,
                          deadline: "float | None" = None,
                          max_records: "int | None" = None,
                          from_start: bool = False) -> int:
    """DESIGN gap G4: ingest transcript records after `transcript_watermark`
    as `source_events` and advance the watermark. Returns the number of
    rows actually created (0 on a replay).

    Idempotency key is `sha256(session_id|transcript|record_uuid)`, so the
    same record can never produce two rows even if the watermark is lost.
    Sidechain (subagent) records are ingested with `is_sidechain=1` -- this
    pass is the *only* place that flag can be set, since `isSidechain`
    exists in transcript records and not in hook payloads.

    `deadline` is a `time.monotonic()` value: a hook passes its own budget
    so a very large transcript is drained across successive Stop hooks
    instead of overrunning one. `max_records` overrides
    `_MAX_RECORDS_PER_RECONCILE` for the same reason. `from_start=True`
    ignores the watermark and re-reads the whole file (what `reprocess`
    does); it is safe because the idempotency key is the record uuid.
    """
    session_row = get(conn, session_id)
    if session_row is None:
        return 0

    path = _resolve_transcript_path(session_row)
    if not path:
        return 0

    repository_id = session_row["repository_id"]
    session_branch = session_row["git_branch"]
    session_cwd = session_row["cwd"]
    fallback_occurred_at = session_row["started_at"]

    ceiling = _MAX_RECORDS_PER_RECONCILE if max_records is None else int(max_records)
    created = 0
    token_sums: dict = {}          # model -> 4-tuple accumulators, flushed below
    last_uuid = None
    examined = 0
    after_uuid = None if from_start else session_row["transcript_watermark"]
    for record in transcript.iter_records(path, after_uuid=after_uuid):
        if examined >= ceiling:
            break
        if deadline is not None and time.monotonic() >= deadline:
            break
        examined += 1

        record_uuid = record.get("uuid")
        if isinstance(record_uuid, str) and record_uuid:
            last_uuid = record_uuid

        summary = transcript.summarize_record(record)
        if summary is None or not summary.get("uuid"):
            continue  # a record type carrying no work evidence

        idem = events.idempotency_key(session_id, "transcript", summary["uuid"])
        row_id = events.append_source_event(
            conn, session_id=session_id, event_type=_transcript_event_type(summary),
            occurred_at=summary.get("timestamp") or fallback_occurred_at,
            payload={
                "uuid": summary["uuid"],
                "parent_uuid": summary.get("parent_uuid"),
                "record_type": summary.get("type"),
                "role": summary.get("role"),
                "text": summary.get("text"),
                "tool_uses": summary.get("tool_uses"),
                "tool_results": summary.get("tool_results"),
            },
            hook_event=TRANSCRIPT_HOOK_EVENT,
            cwd=summary.get("cwd") or session_cwd,
            git_branch=summary.get("git_branch") or session_branch,
            repository_id=repository_id,
            is_sidechain=bool(summary.get("is_sidechain")),
            idem=idem, hook_mode=hook_mode,
        )
        if row_id is not None:
            created += 1
            # Token accounting rides the same exactly-once gate as the event
            # row itself: a replayed record creates no row and adds no tokens.
            usage = summary.get("usage")
            if usage and summary.get("model"):
                sums = token_sums.setdefault(summary["model"], [0, 0, 0, 0])
                sums[0] += usage.get("input_tokens", 0)
                sums[1] += usage.get("output_tokens", 0)
                sums[2] += usage.get("cache_read_input_tokens", 0)
                sums[3] += usage.get("cache_creation_input_tokens", 0)

    if token_sums:
        _accumulate_session_tokens(conn, session_id, token_sums, hook_mode=hook_mode)

    if last_uuid and last_uuid != session_row["transcript_watermark"]:
        now = timeutil.utc_now_iso()
        with connection.transaction(conn, hook_mode=hook_mode):
            conn.execute(
                "UPDATE sessions SET transcript_watermark=?, updated_at=? WHERE session_id=?",
                (last_uuid, now, session_id),
            )
    return created


def _accumulate_session_tokens(conn, session_id: str, token_sums: dict,
                                *, hook_mode: bool = False) -> None:
    """Add this reconcile pass's newly ingested usage to `session_tokens`.

    Increments, not overwrites: reconciliation is incremental (watermarked),
    so each pass carries only records never counted before.
    """
    from work_ledger import entities, ids as _ids
    device_id = entities.current_device(conn, hook_mode=hook_mode)
    now = timeutil.utc_now_iso()
    with connection.transaction(conn, hook_mode=hook_mode):
        for model, (inp, out, cread, ccreate) in sorted(token_sums.items()):
            conn.execute(
                "INSERT INTO session_tokens(id, session_id, model, input_tokens,"
                " output_tokens, cache_read_tokens, cache_creation_tokens,"
                " origin_device_id, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(session_id, model) DO UPDATE SET"
                "  input_tokens = input_tokens + excluded.input_tokens,"
                "  output_tokens = output_tokens + excluded.output_tokens,"
                "  cache_read_tokens = cache_read_tokens + excluded.cache_read_tokens,"
                "  cache_creation_tokens = cache_creation_tokens + excluded.cache_creation_tokens,"
                "  updated_at = excluded.updated_at",
                (_ids.new_id(), session_id, model, inp, out, cread, ccreate,
                 device_id, now, now))


def _repo_root_for_session(conn, session_row) -> "str | None":
    """The repository root to reconcile git evidence against: the session's
    resolved repository if one was stored at SessionStart, otherwise
    whatever repo the session's cwd is inside (None when it is not in one)."""
    repository_id = session_row["repository_id"]
    if repository_id:
        row = conn.execute(
            "SELECT root_path FROM repositories WHERE id=? AND deleted_at IS NULL",
            (repository_id,),
        ).fetchone()
        if row is not None and row["root_path"]:
            return row["root_path"]
    cwd = session_row["cwd"]
    if not cwd:
        return None
    return gitevidence.repo_root(cwd)


def configured_git_identities(conn, repo: str) -> "list[str]":
    """Author values to filter git reconciliation by (Spec §12.5: commits by
    unconfigured identities are never described as the user's work).

    Configured `user_identities` rows win; when the user has not configured
    any yet, the repository's own `user.name`/`user.email` stand in, which
    is the identity the user's commits are actually made under. An empty
    result means "no filter" -- collecting the branch's commits as evidence
    is strictly better than collecting nothing, and attribution downstream
    still has the author fields to judge by.
    """
    rows = conn.execute(
        "SELECT value FROM user_identities WHERE kind='git' AND deleted_at IS NULL "
        "ORDER BY is_primary DESC, created_at"
    ).fetchall()
    values = [row["value"] for row in rows if row["value"]]
    if values:
        return values
    identity = gitevidence.identities(repo)
    return [value for value in (identity.get("email"), identity.get("name")) if value]


GIT_WATERMARK_HOOK_EVENT = "git_watermark"

# Absolute ceiling on the window `reconcile_git` will ask git for, used only
# to unstick a cluster of commits sharing one timestamp (see
# `_reconcile_git_state`). Not a normal-path limit: ordinary backlogs are
# drained by moving the `upper` cursor, which needs no escalation at all.
_GIT_ABSOLUTE_MAX_COMMITS = 5000


def git_watermark(conn, session_id: str) -> dict:
    """The persisted state of this session's git reconciliation:

    - `since` — low bound. Everything at or before it is ingested.
    - `upper` — backfill cursor. `None` means "caught up: take the newest";
      a timestamp means "still walking backwards through a long window".
    - `high`  — the newest commit ever ingested for this session; `since`
      jumps to it once a backfill finishes.
    - `limit` — the window size the next pass should ask for.
    - `truncated` — whether the last pass hit its window size, i.e. a
      backlog is still owed.

    Stored as an append-only `capture_checkpoints` row rather than a
    `sessions` column on purpose: migration files are checksummed and
    forward-only, so a column would mean a new migration (and a
    `SCHEMA_VERSION` bump) that this task does not own. `capture_checkpoints`
    is already append-only and already indexed by `(session_id,
    created_at)`, so the newest row wins -- `id` breaks a same-millisecond
    tie, and UUIDv7 ids sort in creation order.
    """
    row = conn.execute(
        "SELECT detail FROM capture_checkpoints WHERE session_id=? AND hook_event=? "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (session_id, GIT_WATERMARK_HOOK_EVENT),
    ).fetchone()
    empty = {"since": None, "upper": None, "high": None, "limit": None, "truncated": False}
    if row is None or not row["detail"]:
        return empty
    try:
        parsed = json.loads(row["detail"])
    except (ValueError, TypeError):
        return empty
    if not isinstance(parsed, dict):
        return empty
    return {
        "since": parsed.get("since"),
        "upper": parsed.get("upper"),
        "high": parsed.get("high"),
        "limit": parsed.get("limit"),
        "truncated": bool(parsed.get("truncated")),
    }


def _write_git_watermark(conn, session_id: str, state: dict, *, ingested: int,
                          hook_mode: bool = False) -> None:
    """Append this session's git reconciliation state. `status='ok'` because
    this is bookkeeping, not a capture failure -- `worklog doctor`'s failure
    check and `sessions.recent`'s problem count both ignore it."""
    now = timeutil.utc_now_iso()
    checkpoints.checkpoint(
        conn, session_id=session_id, hook_event=GIT_WATERMARK_HOOK_EVENT,
        key=f"{GIT_WATERMARK_HOOK_EVENT}:{session_id}:{now}", status="ok",
        detail=json.dumps({**state, "ingested": ingested}),
        started_at=now, hook_mode=hook_mode,
    )


def reconcile_git(conn, session_id: str, *, hook_mode: bool = False) -> int:
    """DESIGN gap G5: append `commit` source events for this session's
    repository. Returns the number of rows created (0 on a replay -- the
    key is the commit sha).

    This is what picks up work committed outside Claude Code: the next
    session's reconciliation covers the same repository.

    **The algorithm, and why it is not just `--since`.** `git log
    --since=S --max-count=N` returns the N *newest* commits after S and
    silently drops the older tail; `--reverse` does not help (verified: it
    reverses the *printed order* of the same newest N). A naive
    implementation therefore loses any backlog bigger than one window,
    permanently, because the lost commits are older than everything it went
    on to ingest. This walks the window backwards instead, with two
    watermarks (see `git_watermark`):

    1. Ask for the newest `limit` commits in `(since, upper]` -- `upper`
       unset on the first pass, so this is "the newest `limit` commits".
    2. Ingest them (`INSERT OR IGNORE` on the sha, so overlap is free).
    3. **Short window** (fewer than `limit` came back): the window is fully
       drained. Jump `since` to `high` (the newest commit ever seen for this
       session), clear `upper`, and the session is caught up.
    4. **Full window** (a backlog is still owed): set `upper` to the
       *oldest* commit in this batch, so the next pass covers strictly
       older commits. `--until` is inclusive, so that boundary commit comes
       back once more and dedupes away; net progress is `limit - 1` commits
       per pass.

    Each pass therefore either moves `upper` strictly older or, in the one
    pathological case where it cannot -- `limit` or more commits sharing a
    single timestamp, so the same batch comes back with nothing new --
    doubles `limit` (bounded by `_GIT_ABSOLUTE_MAX_COMMITS`) to break out.
    Progress is guaranteed on every pass, so repeated passes always ingest
    the whole history rather than plateauing.
    """
    session_row = get(conn, session_id)
    if session_row is None:
        return 0

    repo = _repo_root_for_session(conn, session_row)
    if not repo:
        return 0

    base_limit = config.get("git.max_commits_per_reconcile", 50)
    try:
        base_limit = max(1, int(base_limit))
    except (TypeError, ValueError):
        base_limit = 50
    # A backfill window must hold at least two commits: with a window of one
    # the inclusive `--until` boundary would return that same commit every
    # pass and the cursor could never move.
    base_limit = max(base_limit, 2)

    state = git_watermark(conn, session_id)
    since = state["since"] or session_row["started_at"]
    upper = state["upper"]
    high = state["high"]
    limit = state["limit"] if isinstance(state["limit"], int) and state["limit"] >= 2 else base_limit
    limit = min(limit, _GIT_ABSOLUTE_MAX_COMMITS)

    authors = configured_git_identities(conn, repo)
    commits = gitevidence.commits_since(repo, since, authors=authors, limit=limit,
                                        until_iso=upper)
    created = _ingest_commits(conn, session_id, session_row, repo, commits, since,
                              hook_mode=hook_mode)

    next_state = _reconcile_git_state(
        commits, created, since=since, upper=upper, high=high,
        limit=limit, base_limit=base_limit,
    )
    # `capture_checkpoints` is append-only, so a pass that changed nothing
    # must write nothing: a quiet session at steady state appends no row, no
    # matter how many times a Stop hook fires.
    unchanged = next_state == {key: state[key] for key in next_state}
    first_pass = state["since"] is None
    if created or first_pass or not unchanged:
        _write_git_watermark(conn, session_id, next_state, ingested=created,
                             hook_mode=hook_mode)
    return created


def _reconcile_git_state(commits, created, *, since, upper, high, limit, base_limit) -> dict:
    """Pure watermark transition for one `reconcile_git` pass -- see that
    function's docstring for the algorithm. Kept separate so the state
    machine can be reasoned about (and tested) without a git repository.

    `len(commits) >= limit` alone cannot tell a genuine new backlog from
    `--since=high`'s inclusive boundary re-matching a cluster we already
    ingested, so `created` (how many of the batch were actually new -- the
    idempotency keys know) disambiguates:

    - **Steady state** (`upper is None`) with a full window and `created ==
      0`: every commit in it is already stored, so there is nothing behind
      it either -- at steady state everything up to `high` is contiguous,
      and anything newer than `high` would have been new. This is the
      boundary re-match. Collapse, do not escalate. Without this a session
      whose newest timestamp is shared by `limit`+ commits never settles:
      it flips `limit` 2 -> 4 -> 8 -> 16 -> reset forever, appends a
      watermark row on nearly every call, and reports `git_backlog=pending`
      for good.
    - **Mid-backfill** (`upper` set) with a full window and `created == 0`:
      the cursor genuinely cannot move -- `limit`+ commits share one
      timestamp -- so widen the window to get past them.
    """
    newest = _to_utc_iso(commits[0].get("authored_at")) if commits else None
    oldest = _to_utc_iso(commits[-1].get("authored_at")) if commits else None
    if newest and (high is None or newest > high):
        high = newest

    steady = {"since": high or since, "upper": None, "high": high,
              "limit": base_limit, "truncated": False}

    if len(commits) < limit:
        # Window fully drained. Everything from `since` up to `high` is now
        # contiguous, so collapse both watermarks and go back to steady state.
        return steady

    if created == 0:
        if upper is None:
            return steady           # boundary re-match, not a backlog
        if limit >= _GIT_ABSOLUTE_MAX_COMMITS:
            # Runaway stop: a cluster too large to widen past. Settle rather
            # than append a watermark row on every future pass forever.
            return steady
        # The cursor could not move: `limit`+ commits share one timestamp.
        # Widening the window is the only way past them.
        return {"since": since, "upper": oldest or upper, "high": high,
                "limit": min(limit * 2, _GIT_ABSOLUTE_MAX_COMMITS), "truncated": True}

    return {"since": since, "upper": oldest or upper, "high": high,
            "limit": limit, "truncated": True}


def _ingest_commits(conn, session_id, session_row, repo, commits, fallback_occurred_at,
                     *, hook_mode: bool = False) -> int:
    if not commits:
        return 0
    branch = session_row["git_branch"] or gitevidence.current_branch(repo)
    created = 0
    for commit in commits:
        idem = events.idempotency_key(session_id, "commit", commit["sha"])
        row_id = events.append_source_event(
            conn, session_id=session_id, event_type="commit",
            occurred_at=_to_utc_iso(commit.get("authored_at")) or fallback_occurred_at,
            payload={
                "sha": commit["sha"],
                "short_sha": commit["short_sha"],
                "subject": commit["subject"],
                "body": commit["body"],
                "files": commit["files"],
                "author_name": commit["author_name"],
                "author_email": commit["author_email"],
                "authored_at": commit["authored_at"],
                "repo_root": repo,
            },
            hook_event=GIT_HOOK_EVENT, cwd=repo, git_branch=branch,
            repository_id=session_row["repository_id"],
            idem=idem, hook_mode=hook_mode,
        )
        if row_id is not None:
            created += 1
    return created


def _missing_module(exc: ImportError, name: str) -> bool:
    """True only when `exc` is *this* module being absent -- not an
    ImportError raised from inside it. A broad `except ImportError` would
    turn a genuine bug in Stage A's own imports into a silent "not
    implemented yet", which is exactly the failure mode that hides for
    weeks."""
    return getattr(exc, "name", None) in (name, "work_ledger.infer")


def run_stage_a(conn, session_id: str, *, hook_mode: bool = False) -> dict:
    """Stage A (Task 26). Imported lazily and by name so this module works
    -- and its tests run -- before `work_ledger.infer` exists.

    `hook_mode=True` threads through to every write Stage A performs, so the
    Stop hook's Stage A pass gives up on lock contention inside the hook
    budget rather than the full non-hook retry schedule."""
    try:
        from work_ledger.infer import stage_a
    except ImportError as exc:
        if not _missing_module(exc, "work_ledger.infer.stage_a"):
            raise
        return {"ran": False, "reason": "infer.stage_a is not available yet (Task 26)"}
    return stage_a.run(conn, session_id, hook_mode=hook_mode)


def run_stage_b(conn, session_id: str) -> dict:
    """Stage B (Task 29), same lazy-import contract as `run_stage_a`.
    `force=True`: an explicit `worklog reprocess` is a deliberate user
    action and should not be silently swallowed by the rate limiter.
    `stage_b.merge` keys on the bundle hash, so re-running still creates no
    duplicate activities.

    This runs Stage B **in the foreground**, which is right for an explicit
    `worklog reprocess` and wrong for a hook. The Stop hook does not call
    this -- it calls `stage_b.spawn(session_id)` (detached, returns
    immediately) as its last statement, which is the interface Task 29 owns
    for the hook path.
    """
    try:
        from work_ledger.infer import stage_b
    except ImportError as exc:
        if not _missing_module(exc, "work_ledger.infer.stage_b"):
            raise
        return {"ran": False, "reason": "infer.stage_b is not available yet (Task 29)"}
    return stage_b.run(conn, session_id, force=True)


def reprocess(conn, session_id: str, *, stage: str = "both") -> dict:
    """Re-run reconciliation and interpretation for one session from the raw
    evidence (Spec §20.2). Creates no duplicate events and no duplicate
    activities: every write below is keyed by an idempotency key derived
    from the evidence itself, never from wall-clock time.

    `stage` is `"a"`, `"b"`, or `"both"`.
    """
    if stage not in ("a", "b", "both"):
        raise ValueError(f"sessions.reprocess: stage must be 'a', 'b', or 'both', not {stage!r}")

    result = {
        "session_id": session_id,
        "found": get(conn, session_id) is not None,
        "transcript_events": 0,
        "git_events": 0,
        "stage_a": None,
        "stage_b": None,
    }
    if not result["found"]:
        return result

    # from_start=True: reprocess re-reads the transcript from the top rather
    # than trusting the watermark, so "re-runs from raw evidence" (Spec
    # SS20.2) is literally true -- a watermark that ran ahead of a failed
    # ingest is exactly the state a user runs `reprocess` to repair.
    # INSERT OR IGNORE keeps the re-read free of duplicates.
    result["transcript_events"] = reconcile_transcript(conn, session_id, from_start=True)
    result["git_events"] = reconcile_git(conn, session_id)
    if stage in ("a", "both"):
        result["stage_a"] = run_stage_a(conn, session_id)
    if stage in ("b", "both"):
        result["stage_b"] = run_stage_b(conn, session_id)
    return result


def recent(conn, *, limit: int = 20) -> "list[dict]":
    """Recent sessions, newest `last_seen_at` first, with capture counts --
    what `worklog sessions` renders."""
    rows = conn.execute(
        "SELECT s.session_id, s.cwd, s.git_branch, s.started_at, s.last_seen_at, "
        "       s.ended_at, s.end_reason, s.transcript_path, s.repository_id, "
        "       (SELECT count(*) FROM source_events e WHERE e.session_id = s.session_id) AS event_count, "
        "       (SELECT count(*) FROM capture_checkpoints c WHERE c.session_id = s.session_id "
        "         AND c.status IN ('failed','partial')) AS problem_count "
        "FROM sessions s ORDER BY s.last_seen_at DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    return [dict(row) for row in rows]


def idle_finalize_minutes() -> int:
    """`session.idle_finalize_minutes` (default 30), read fresh on every
    call so a config edit takes effect without a restart."""
    value = config.get("session.idle_finalize_minutes", 30)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 30
