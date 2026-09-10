"""Shared text normalization for work item titles and dedupe keys.

Ruling 23: STOPWORDS, STEM_MAP, normalize_key, and normalize_title live only
here. work_items imports normalize_title; infer/dedupe imports
normalize_key/token_set. Pure functions, no I/O, no imports from any other
work_ledger module.
"""
import re
import unicodedata

STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "to", "of", "for", "and", "or", "in", "on", "is", "be",
    "with", "that", "this", "it", "we", "i", "need", "needs", "should",
    "must", "still", "remaining", "left", "todo",
})

STEM_MAP: dict[str, str] = {
    "fixing": "fix", "fixed": "fix", "fixes": "fix",
    "testing": "test", "tested": "test",
    "retesting": "retest", "retested": "retest",
    "implementing": "implement", "implemented": "implement",
    "adding": "add", "added": "add",
    "updating": "update", "updated": "update",
    "removing": "remove", "removed": "remove",
}

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def tokens(text: str) -> list[str]:
    """Lowercase, strip accents, split on non-alphanumeric runs, drop
    stopwords, and stem via STEM_MAP."""
    normalized = unicodedata.normalize("NFKD", text.lower())
    without_marks = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    spaced = _NON_ALNUM_RE.sub(" ", without_marks)
    raw_tokens = spaced.split()
    result = []
    for tok in raw_tokens:
        if tok in STOPWORDS:
            continue
        result.append(STEM_MAP.get(tok, tok))
    return result


def normalize_key(title: str) -> str:
    """Sorted unique tokens joined with '-', truncated to 120 characters."""
    return "-".join(sorted(set(tokens(title))))[:120]


# normalize_title is the same pipeline as normalize_key: one owner, one function.
normalize_title = normalize_key


def token_set(text: str) -> frozenset[str]:
    """The input to every Jaccard computation."""
    return frozenset(tokens(text))
