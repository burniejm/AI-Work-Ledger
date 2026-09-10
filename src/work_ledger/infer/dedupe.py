"""Remaining-item deduplication: similarity scoring, material-difference
detection, and the DESIGN §7 create/reinforce/flag decision table.

Ruling 23: `textnorm.py` (Task 4) is the single owner of the stop-word list,
the stemming map, `normalize_key`/`normalize_title`, and `token_set`. This module
imports `normalize_key`/`token_set` directly and defines no stopword list,
stemming map, or normalization pipeline of its own -- only the similarity
metric and the banding logic below. `work_items` (Task 13) imports the same
functions from `textnorm`, so there is exactly one stopword list in the
codebase.

`resolve` is the single write path for a remaining item discovered by
inference: it looks up candidates scoped to the same work item (or, absent
a work item, the same project), then applies DESIGN §7 in order:

  1. equal `normalized_key`, any state       -> reinforce (reopening first
                                                 if the matched item is
                                                 completed/cancelled/
                                                 superseded -- spec §9's
                                                 `reopened` state exists
                                                 exactly for a mention that
                                                 resurfaces after the item
                                                 was closed)
  2. among candidates in `remaining.OPEN_STATES` only, best-scoring:
       a. `material_difference` is not None                    -> create
       b. `similarity >= REINFORCE_THRESHOLD`                   -> reinforce
       c. `DUPLICATE_BAND[0] <= similarity < DUPLICATE_BAND[1]` -> create + flag
  3. otherwise                                                  -> create

A resolved item (completed/cancelled/superseded) is deliberately excluded
from step 2: two mentions merely *resembling* a closed item must never
silently reinforce a row a report already treats as done and invisible to
open views. Only an exact-normalized-key re-mention is strong enough
evidence to reopen it; anything short of that becomes a new item instead,
leaving the resolved one untouched.
"""
import re

from .. import contradictions, remaining
from ..textnorm import normalize_key, token_set

__all__ = [
    "normalize_key",
    "token_set",
    "similarity",
    "REINFORCE_THRESHOLD",
    "DUPLICATE_BAND",
    "material_difference",
    "resolve",
]

# DESIGN §7 bands, named so the tests and the implementation cannot drift.
REINFORCE_THRESHOLD = 0.85
DUPLICATE_BAND = (0.60, 0.85)

_TICKET_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]*-\d+\b")
_FILE_PATH_RE = re.compile(r"\b[\w.-]+(?:/[\w.-]+)+\b")
_NEGATION_RE = re.compile(
    r"\b(?:not|no|never|cannot|can't|couldn't|doesn't|does not|didn't|did not|"
    r"isn't|is not|wasn't|was not|weren't|were not|won't|will not|aren't|are not|"
    r"hasn't|has not|haven't|have not|hadn't|had not|shouldn't|should not|"
    r"wouldn't|would not|mustn't|must not)\b",
    re.IGNORECASE,
)

# Reinforce is never enough on its own to bring a resolved item back into
# view -- these are the states `resolve` will reopen on an exact-key match.
_RESOLVED_STATES = ("completed", "cancelled", "superseded")


def similarity(a: str, b: str) -> float:
    """Jaccard similarity over `textnorm.token_set(a)` and `token_set(b)`."""
    tokens_a = token_set(a)
    tokens_b = token_set(b)
    union = tokens_a | tokens_b
    if not union:
        return 0.0
    return len(tokens_a & tokens_b) / len(union)


def material_difference(a: str, b: str) -> "str | None":
    """A reason two titles must never be merged into the same item, or None.

    Checked in order: different ticket key, different file path, negation
    mismatch. Each check only fires when *both* sides name something to
    compare -- one side naming a ticket key and the other naming none is not
    itself a material difference (that is what the similarity bands are for).
    """
    tickets_a = set(_TICKET_KEY_RE.findall(a))
    tickets_b = set(_TICKET_KEY_RE.findall(b))
    if tickets_a and tickets_b and tickets_a != tickets_b:
        return "different ticket key"

    paths_a = set(_FILE_PATH_RE.findall(a))
    paths_b = set(_FILE_PATH_RE.findall(b))
    if paths_a and paths_b and paths_a != paths_b:
        return "different file path"

    if bool(_NEGATION_RE.search(a)) != bool(_NEGATION_RE.search(b)):
        return "negation mismatch"

    return None


def _candidates(conn, *, work_item_id, project_id):
    """Remaining items (any state) scoped to `work_item_id`, or -- when the
    new mention has no work item -- to `project_id` instead."""
    if work_item_id is not None:
        return conn.execute(
            "SELECT id, title, state FROM remaining_items WHERE work_item_id=? "
            "AND deleted_at IS NULL",
            (work_item_id,),
        ).fetchall()
    return conn.execute(
        "SELECT id, title, state FROM remaining_items WHERE work_item_id IS NULL "
        "AND project_id IS ? AND deleted_at IS NULL",
        (project_id,),
    ).fetchall()


def _open_possible_duplicate(conn, *, work_item_id, existing_id, existing_title,
                              new_id, new_title, occurred_at):
    """Opens a `possible_duplicate` contradiction through `contradictions`
    (Task 30) -- the single write path for every contradiction rule, so
    `uq_contradictions_open` is honored the same way here as everywhere
    else. `occurred_at` is accepted for call-site stability but no longer
    used: `contradictions.open_contradiction` stamps `detected_at` with the
    time the engine noticed the duplicate, not the mention's event time.
    """
    contradictions.open_contradiction(
        conn, rule="possible_duplicate", work_item_id=work_item_id,
        subject_table="remaining_items", subject_id=new_id,
        ledger_claim=f"existing remaining item {existing_id}: {existing_title!r}",
        external_claim=f"new remaining item {new_id}: {new_title!r}",
    )


def _reinforce_and_maybe_reopen(conn, row, *, occurred_at, confidence, source,
                                 source_event_id, activity_id, detail):
    """Reinforce `row`, reopening it first if it was resolved.

    A mention that exactly matches a completed/cancelled/superseded item's
    normalized key is strong enough evidence (spec §9) to bring it back via
    `remaining.transition(..., "reopened", ...)`, with full provenance, so
    `open_items` sees it again immediately.
    """
    if row["state"] in _RESOLVED_STATES:
        remaining.transition(
            conn, row["id"], "reopened", occurred_at=occurred_at, source=source,
            confidence=confidence, explanation="re-mentioned after resolution",
            source_event_id=source_event_id, activity_id=activity_id,
        )
    remaining.reinforce(conn, row["id"], occurred_at=occurred_at,
                         source_event_id=source_event_id, detail=detail)


def resolve(conn, *, title, work_item_id, client_id, project_id, occurred_at, confidence, source,
            detail=None, kind="remaining", state="open", source_event_id=None,
            activity_id=None):
    """Create, reinforce, or create-and-flag `title` as a remaining item.

    Returns `(remaining_item_id, action)` where `action` is `"created"`,
    `"reinforced"`, or `"created_with_duplicate_flag"`.

    `state` is the lifecycle state a *newly created* item starts in (one of
    `remaining.STATES`; `remaining.OPEN_STATES` includes `blocked` and
    `waiting`, so a blocked item is still an open one). It applies to the
    create paths only -- a mention that matches an existing item reinforces
    it, and moving a live item between states is `remaining.transition`'s
    job, with the provenance row that goes with it. Added in Task 29 so a
    caller that knows an item is blocked (Stage B reporting
    `state: "blocked"`, or a `Blocked:` commit trailer) does not have to
    create it as `open` and immediately correct itself.
    """
    key = normalize_key(title)
    rows = _candidates(conn, work_item_id=work_item_id, project_id=project_id)

    def _create():
        return remaining.create(
            conn, title=title, normalized_key=key, work_item_id=work_item_id,
            client_id=client_id, project_id=project_id, detail=detail, kind=kind,
            state=state, confidence=confidence, source=source, occurred_at=occurred_at,
            source_event_id=source_event_id, activity_id=activity_id,
        )

    # Step 1: an exact normalized-key match wins regardless of state -- the
    # unique index on (work_item_id, normalized_key) guarantees at most one
    # live row can match here.
    for row in rows:
        if normalize_key(row["title"]) == key:
            _reinforce_and_maybe_reopen(
                conn, row, occurred_at=occurred_at, confidence=confidence, source=source,
                source_event_id=source_event_id, activity_id=activity_id, detail=detail,
            )
            return row["id"], "reinforced"

    # Step 2: similarity-band matching considers only items still open --
    # a resolved item must never be pulled back in just by resembling a new
    # mention (see module docstring).
    best_row = None
    best_score = -1.0
    for row in rows:
        if row["state"] not in remaining.OPEN_STATES:
            continue
        score = similarity(title, row["title"])
        if score > best_score:
            best_score = score
            best_row = row

    if best_row is not None and material_difference(title, best_row["title"]) is None:
        if best_score >= REINFORCE_THRESHOLD:
            remaining.reinforce(conn, best_row["id"], occurred_at=occurred_at,
                                 source_event_id=source_event_id, detail=detail)
            return best_row["id"], "reinforced"

        if DUPLICATE_BAND[0] <= best_score < DUPLICATE_BAND[1]:
            new_id = _create()
            _open_possible_duplicate(
                conn, work_item_id=work_item_id, existing_id=best_row["id"],
                existing_title=best_row["title"], new_id=new_id, new_title=title,
                occurred_at=occurred_at,
            )
            return new_id, "created_with_duplicate_flag"

    return _create(), "created"
