"""AI-usage score: how much of a work item's code was AI-written, 1-5.

The scale (DESIGN "AI-usage score"):

- 5 -- essentially one-shot: nearly all edits made by AI tools, very few
  prompts relative to the volume of change, nothing human-only.
- 4 -- AI wrote most of it, the human steered: heavy AI edits with real
  prompt back-and-forth, still no human-only commits.
- 3 -- genuinely mixed: AI edits and human-only commits both carry weight.
- 2 -- mostly human: human-only commits dominate, AI edits are minor.
- 1 -- AI advised only: sessions touched the ticket read-only (or not at
  all); the code came from human-only commits.
- None -- no session or git evidence whatsoever: shown as unknown, never
  guessed.

Two layers, mirroring attribution:

- `score_from_signals` is the deterministic rubric -- pure, threshold-based,
  explainable, may be `high` confidence (Ruling 6: deterministic inference
  from direct signals is not capped).
- Stage B may later *refine* the stored score within +/-1 (see
  `infer.stage_b`); a model opinion is capped at `medium` confidence and
  never moves the score further than one step from this rubric's answer.

Granularity note: signals are counted over the ticket's contributing
sessions whole -- a session that touched two tickets contributes its edit
counts to both. That approximation is documented in the reason string's
session count, is corrected by `worklog correct` when it misleads, and
sharpens once human commits are captured directly (planned git-side
capture) rather than inferred from reconcile-time provenance.
"""

# Unit weights: an edit-tool call is 1 unit of authored work; a commit
# represents a *chunk* of work whose edits we did (AI) or did not (human)
# see individually. An AI commit's edits were already counted one by one,
# so it adds only a small bonus; a human-only commit stands in for all of
# its unseen edits, so it weighs more.
AI_COMMIT_UNITS = 3
HUMAN_COMMIT_UNITS = 5

# `prompts / ai_units` at or below this reads as "one-shot": the human
# barely steered relative to how much the AI changed.
ONE_SHOT_PROMPT_RATIO = 0.2

# ai_units / (ai_units + human_units) thresholds for the mixed bucket.
MIXED_HIGH_SHARE = 0.75   # >= this with human commits present -> 4
MIXED_LOW_SHARE = 0.3     # < this -> 2; between -> 3

# Evidence volume (authored units, both sides) -> confidence.
HIGH_VOLUME = 10
MEDIUM_VOLUME = 3


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def score_from_signals(*, ai_edits: int, ai_commits: int, human_commits: int,
                       prompts: int, sessions: int):
    """The deterministic rubric. Returns `(score, confidence, reason)`;
    `(None, None, reason)` when there is no evidence at all."""
    ai_units = ai_edits + AI_COMMIT_UNITS * ai_commits
    human_units = HUMAN_COMMIT_UNITS * human_commits

    counts = (f"{_plural(ai_edits, 'AI edit')}, "
              f"{_plural(ai_commits, 'AI commit')}, "
              f"{human_commits} human-only commit{'s' if human_commits != 1 else ''}, "
              f"{_plural(prompts, 'prompt')} across {_plural(sessions, 'session')}")

    if ai_units == 0 and human_units == 0:
        if sessions == 0:
            return None, None, "no evidence: no sessions or commits recorded"
        return 1, "low", f"advisory sessions only ({counts})"

    if ai_units == 0:
        score = 1
    elif human_units == 0:
        ratio = prompts / ai_units
        score = 5 if ratio <= ONE_SHOT_PROMPT_RATIO else 4
    else:
        share = ai_units / (ai_units + human_units)
        if share >= MIXED_HIGH_SHARE:
            score = 4
        elif share >= MIXED_LOW_SHARE:
            score = 3
        else:
            score = 2

    volume = ai_units + human_units
    if volume >= HIGH_VOLUME:
        confidence = "high"
    elif volume >= MEDIUM_VOLUME:
        confidence = "medium"
    else:
        confidence = "low"

    return score, confidence, counts


# The AI-side file-modifying tools -- `capture.events._ALWAYS_KEEP_TOOLS`
# minus `Task` (a subagent dispatch changes no files by itself).
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def _is_pinned_by_correction(conn, work_item_id: str) -> bool:
    """True when a live (not-undone) correction owns this item's score.

    Undo is append-only (Ruling 7): a correction row is "undone" when a later
    row's `undo_of_correction_id` points at it, so a pin is any correction on
    the score field that is neither an undo row itself nor the target of one.
    """
    row = conn.execute(
        "SELECT 1 FROM corrections c"
        " WHERE c.target_table='work_items' AND c.target_id=?"
        "   AND c.field='ai_usage_score'"
        "   AND c.undo_of_correction_id IS NULL"
        "   AND NOT EXISTS (SELECT 1 FROM corrections u WHERE u.undo_of_correction_id=c.id)"
        " LIMIT 1",
        (work_item_id,),
    ).fetchone()
    return row is not None


def gather_signals(conn, work_item_id: str) -> dict:
    """Count the rubric's inputs over the ticket's contributing sessions.

    Provenance rules:
    - `tool_use` rows whose `tool_name` is an edit tool -> AI edits.
    - `git_commit_signal` rows that really created a commit -> AI commits.
    - reconciled `commit` rows count as human-only ONLY in sessions with no
      AI edits: in an edit-active session a reconciled commit is either the
      AI's own commit re-found by the repo sweep or ambiguous, and the AI
      side of it was already counted edit by edit.
    - `prompt` rows count only when `_is_user_prompt` says the turn was the
      human's (transcript reconciliation stores Claude's turns as prompts
      too, distinguished by `payload.role`).
    """
    import json as _json

    from work_ledger.infer import stage_a as _stage_a

    session_ids = [r["session_id"] for r in conn.execute(
        "SELECT DISTINCT s.session_id FROM activities a"
        " JOIN sessions s ON s.id = a.session_row_id"
        " WHERE a.work_item_id=? AND a.deleted_at IS NULL", (work_item_id,))]

    ai_edits = ai_commits = human_commits = prompts = 0
    counted_shas = set()
    for sid in session_ids:
        # The IN list is a SQL literal (not built from EDIT_TOOLS) so the
        # security suite's AST prover can verify this statement; the guard
        # test below keeps the two in sync.
        edits = conn.execute(
            "SELECT COUNT(*) AS n FROM source_events"
            " WHERE session_id=? AND event_type='tool_use'"
            " AND tool_name IN ('Edit','Write','MultiEdit','NotebookEdit')",
            (sid,)).fetchone()["n"]
        ai_edits += edits

        for row in conn.execute(
                "SELECT payload_json FROM source_events"
                " WHERE session_id=? AND event_type='git_commit_signal'", (sid,)):
            payload = _json.loads(row["payload_json"])
            if _stage_a._creates_a_commit(payload.get("command"), payload.get("subject")):
                ai_commits += 1

        if edits == 0:
            for row in conn.execute(
                    "SELECT payload_json FROM source_events"
                    " WHERE session_id=? AND event_type='commit'", (sid,)):
                human_commits += 1
                sha = (_json.loads(row["payload_json"]) or {}).get("sha")
                if isinstance(sha, str) and sha:
                    counted_shas.add(sha)

        for row in conn.execute(
                "SELECT payload_json FROM source_events"
                " WHERE session_id=? AND event_type='prompt'", (sid,)):
            if _stage_a._is_user_prompt(_json.loads(row["payload_json"])):
                prompts += 1

    human_commits += _hook_captured_human_commits(conn, work_item_id, counted_shas)

    return {"ai_edits": ai_edits, "ai_commits": ai_commits,
            "human_commits": human_commits, "prompts": prompts,
            "sessions": len(session_ids)}


def _hook_captured_human_commits(conn, work_item_id: str, counted_shas: set) -> int:
    """Commits captured by the git post-commit hook (githooks.py) that
    belong to this ticket and are definitively human.

    Definitive means `session_active=0` -- made with no live Claude session
    covering the repo. A row whose sha reconcile already counted (the same
    commit swept into a read-only session) is skipped, and a row made
    during a live session stays ambiguous: the session-granularity rules
    above already own that case.
    """
    import re as _re

    row = conn.execute(
        "SELECT external_key, display_id FROM work_items WHERE id=?",
        (work_item_id,)).fetchone()
    if row is None:
        return 0
    keys = [k for k in (row["external_key"], row["display_id"]) if k]
    if not keys:
        return 0
    patterns = [_re.compile(r"\b" + _re.escape(key) + r"\b", _re.IGNORECASE)
                for key in keys]

    count = 0
    for commit in conn.execute(
            "SELECT sha, subject FROM human_commits WHERE session_active=0"):
        if commit["sha"] in counted_shas:
            continue
        subject = commit["subject"] or ""
        if any(pattern.search(subject) for pattern in patterns):
            count += 1
            counted_shas.add(commit["sha"])
    return count


def recompute_for_work_item(conn, work_item_id: str, *, hook_mode: bool = False) -> dict:
    """Recompute and persist the deterministic score for one work item.

    A live correction on `ai_usage_score` owns the value: recompute becomes
    a no-op (`{"skipped": "corrected"}`). A no-change recompute writes
    nothing (no `updated_at` churn).
    """
    from work_ledger import timeutil
    from work_ledger.db import connection as _dbc

    if _is_pinned_by_correction(conn, work_item_id):
        return {"skipped": "corrected"}

    signals = gather_signals(conn, work_item_id)
    score, conf, reason = score_from_signals(**signals)

    current = conn.execute(
        "SELECT ai_usage_score, ai_usage_confidence, ai_usage_reason"
        " FROM work_items WHERE id=? AND deleted_at IS NULL", (work_item_id,)).fetchone()
    if current is None:
        return {"skipped": "missing"}
    if (current["ai_usage_score"], current["ai_usage_confidence"],
            current["ai_usage_reason"]) == (score, conf, reason):
        return {"score": score, "confidence": conf, "changed": False}

    with _dbc.transaction(conn, hook_mode=hook_mode):
        conn.execute(
            "UPDATE work_items SET ai_usage_score=?, ai_usage_confidence=?,"
            " ai_usage_reason=?, updated_at=? WHERE id=?",
            (score, conf, reason, timeutil.utc_now_iso(), work_item_id))
    return {"score": score, "confidence": conf, "changed": True}
