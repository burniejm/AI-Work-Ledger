"""Confidence scoring policy: signal points, bucketing, contradiction
detection, and the inference inbox routing decision (Task 27; DESIGN §6).

Pure functions only -- nothing here touches the database or the filesystem.
`infer.stage_a` (Task 26) inlined this exact machinery ahead of extraction so
this module's landing would be a call-site swap, not a behaviour change;
`infer.stage_b` (Task 29) and later tasks share it from here.

**Ruling 6** governs the split between *attribution* confidence and
*narrative* confidence when Stage B does not run (crashed, timed out, or was
never invoked):

* `classify` never lowers the bucket because `stage_b_ran=False` -- a Stage A
  attribution backed by a direct signal (`DIRECT_SIGNALS`: a ticket key in
  the git branch name or in a commit message) plus enough other evidence can
  still reach `high`. `stage_b_ran=False` only appends a note to `reason`
  recording that the summary is templated.
* `narrative_policy` is the cap Stage B's absence *does* impose: it forbids
  model-authored `outcome`/`summary` text and any `source='stage_b'` row.
  Callers gate their narrative writes on this, not on the bucket.
"""

__all__ = [
    "SIGNAL_POINTS",
    "DIRECT_SIGNALS",
    "score",
    "bucket",
    "reason_text",
    "classify",
    "narrative_policy",
    "in_inbox",
]

SIGNAL_POINTS = {
    "manual_statement": 100,
    "ticket_in_branch": 35,
    "ticket_in_commit_message": 30,
    "ticket_in_prompt": 20,
    "ticket_in_tool_io": 8,
    "repo_mapping_persisted": 40,
    "repo_alias_persisted": 30,
    "repo_name_similarity": 12,
    "path_prefix_mapping": 15,
    "commit_by_known_identity": 15,
    "stage_b_agreed": 10,
}

# Ruling 6: the signals that entitle an attribution to `high` whether or not
# Stage B ran.
DIRECT_SIGNALS = ("ticket_in_branch", "ticket_in_commit_message")

# Human phrasing for `reason_text`, one entry per signal above.
_SIGNAL_PHRASES = {
    "manual_statement": "explicit manual statement",
    "ticket_in_branch": "ticket key in branch",
    "ticket_in_commit_message": "ticket key in commit message",
    "ticket_in_prompt": "ticket key in prompt",
    "ticket_in_tool_io": "ticket key in tool input/output",
    "repo_mapping_persisted": "repository mapping persisted",
    "repo_alias_persisted": "repository alias persisted",
    "repo_name_similarity": "repository name similarity only",
    "path_prefix_mapping": "files under a mapped path prefix",
    "commit_by_known_identity": "commit by known identity",
    "stage_b_agreed": "Stage B agreed",
}

# DESIGN §6 override 1: two ticket keys from *different* connections whose
# scores are this close are an ambiguity, not an attribution.
CONTRADICTORY_WITHIN_POINTS = 10

_STAGE_B_ABSENT_NOTE = "; Stage B did not run — summary is templated Stage A output"


def score(signals: dict) -> int:
    """Sum `SIGNAL_POINTS[name] * count`, capped at 100. Unknown names score 0."""
    total = 0
    for name, count in signals.items():
        total += SIGNAL_POINTS.get(name, 0) * int(count)
    return min(total, 100)


def bucket(value: int) -> str:
    """`>=70` -> high, `40..69` -> medium, else low (DESIGN §6)."""
    if value >= 70:
        return "high"
    if value >= 40:
        return "medium"
    return "low"


def reason_text(signals: dict) -> str:
    """A human sentence, e.g. `"repository mapping persisted (+40), ticket key
    in branch (+35)"`. Ordered by descending point value so the reader sees
    what carried the decision first."""
    parts = []
    for name, count in sorted(signals.items(),
                              key=lambda kv: (-SIGNAL_POINTS.get(kv[0], 0), kv[0])):
        points = SIGNAL_POINTS.get(name, 0) * int(count)
        if points == 0:
            continue
        parts.append(f"{_SIGNAL_PHRASES.get(name, name)} (+{points})")
    return ", ".join(parts) if parts else "no attribution signals"


def classify(signals: dict, *, competing=None, stage_b_only: bool = False,
             stage_b_ran: bool = True, manual: bool = False):
    """Return `(bucket, score, reason)`, applying the DESIGN §6 overrides in
    order: competing candidates within `CONTRADICTORY_WITHIN_POINTS` ->
    `"contradictory"`; `stage_b_only` caps at `medium`; `manual` forces
    `high`.

    Ruling 6: `stage_b_ran=False` does **not** cap the bucket -- it only
    appends a note to `reason` recording that the summary is templated. The
    cap Stage B's absence *does* impose lives in `narrative_policy`.
    """
    if manual:
        return "high", 100, _SIGNAL_PHRASES["manual_statement"]

    value = score(signals)
    reason = reason_text(signals)

    if competing and len(competing) >= 2:
        top_points = max(points for _key, points in competing)
        rivals = [(key, points) for key, points in competing
                  if top_points - points <= CONTRADICTORY_WITHIN_POINTS]
        if len(rivals) >= 2:
            listed = ", ".join(f"{key} ({points})" for key, points in competing)
            reason = (f"competing ticket keys within {CONTRADICTORY_WITHIN_POINTS} "
                      f"points: {listed}")
            if not stage_b_ran:
                reason += _STAGE_B_ABSENT_NOTE
            return "contradictory", value, reason

    resolved = bucket(value)
    if stage_b_only and resolved == "high":
        resolved = "medium"
        reason += "; capped at medium (Stage B only, DESIGN §6 override 2)"
    if not stage_b_ran:
        reason += _STAGE_B_ABSENT_NOTE
    return resolved, value, reason


def narrative_policy(*, stage_b_ran: bool) -> dict:
    """The cap Stage B's absence imposes on *narrative* output (Ruling 6).

    `{"summary": "templated"|"model", "allow_stage_b_fields": bool}`. When
    Stage B did not run, the caller may never write model-authored
    `outcome`/`summary` text and must not write a `source='stage_b'` row --
    it keeps the deterministic Stage A templates instead. This is
    independent of `classify`'s bucket: attribution confidence can be `high`
    while the narrative stays templated.
    """
    if not stage_b_ran:
        return {"summary": "templated", "allow_stage_b_fields": False}
    return {"summary": "model", "allow_stage_b_fields": True}


def in_inbox(resolved_bucket: str) -> int:
    """`1` only for `low` (DESIGN §6 behaviour table)."""
    return 1 if resolved_bucket == "low" else 0
