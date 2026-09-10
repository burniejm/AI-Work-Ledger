"""Secret redaction (DESIGN §12, Ruling 15/22, D10).

This module is the SOLE owner of credential-pattern knowledge in work-ledger
(Task 38's layering guard, `test_layering`, checks that no other source file
carries a literal like ``ATATT``/``ghp_``/``xox``/``AKIA``/``PRIVATE KEY``/``eyJ``).
Every other module that needs to know whether a string looks like a secret,
or needs to scrub one before it is persisted, logged, or emitted, calls into
``redact`` rather than growing its own regex.

Two references are never secrets and must survive redaction unchanged:
``keychain://...`` and ``env://...`` — these are the only forms a config file
or the database is allowed to hold for a credential (Global Constraints:
"Secrets ... never in ordinary YAML/JSON config ... Credentials live in the
OS keychain; config and SQLite hold only a reference").
"""
import re

_SAFE_REFERENCE_PREFIXES = ("keychain://", "env://")

# A *bare* reference -- the ENTIRE string is "keychain://..." or
# "env://...", nothing else. Fix round 2: `redact()`/`looks_like_secret()`
# used to skip scanning any string that merely STARTED WITH one of these
# prefixes, which let a real secret through untouched whenever it was
# concatenated after a reference in ordinary prose --
# `redact("env://JIRA_TOKEN resolved to ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")`
# returned the GitHub token verbatim. Only a string that IS the reference,
# in full, is exempt from scanning; a reference embedded in a longer
# string is scanned normally, same as any other text.
_BARE_REFERENCE_RE = re.compile(r"(?:keychain|env)://\S+")


def _is_bare_reference(text: str) -> bool:
    return bool(_BARE_REFERENCE_RE.fullmatch(text))

# Runtime registry of exact secret VALUES this process has actually resolved
# from a credential reference (populated by `credentials.resolve()`). A
# value only ever enters this set because something in this process
# legitimately handled it as a live credential -- never from guessing at a
# string's *shape* -- so matching against it can never mistake an ordinary
# identifier for a secret the way a shape-based pattern can. (Task 38
# review: a shape-only heuristic broad enough to catch a bare, human-chosen
# password like "hunter2hunter2" was found to be indistinguishable from an
# ordinary branch name, image tag, or commit subject like
# "release2026branch"/"acme2026"/"coredns7db6d8ff4d" -- there is no
# character-class rule that separates them. An exact match against a value
# this process is *known* to have resolved as a credential has no such
# false-positive mode.)
_KNOWN_SECRET_VALUES: "set[str]" = set()


# A credential this short is realistically a test/placeholder fixture, not
# a real secret -- and registered exact-match text is matched by plain
# substring below, so a short registered value (e.g. a fixture "tok" used
# across many *other* tests' throwaway `env://` credentials) would corrupt
# any ordinary word that happens to contain it (a "tok" fixture value
# mangling the plain English word "token" is exactly this failure, caught
# by Task 38's own review). Real API tokens/passwords are essentially never
# this short; this floor is a safety margin, not a claim about what "real"
# looks like.
_MIN_KNOWN_SECRET_LENGTH = 8


def register_known_secret(value: "str | None") -> None:
    """Remember `value` as a secret this process has resolved from a
    credential reference, so `redact()` catches it verbatim wherever it
    later appears, regardless of shape. A no-op for `None`/empty, for a
    bare `keychain://`/`env://` reference string (the reference itself is
    never a secret and must never be redacted), and for anything shorter
    than `_MIN_KNOWN_SECRET_LENGTH` (see above)."""
    if not value or len(value) < _MIN_KNOWN_SECRET_LENGTH or value.startswith(_SAFE_REFERENCE_PREFIXES):
        return
    _KNOWN_SECRET_VALUES.add(value)


def clear_known_secrets() -> None:
    """Reset the known-secret registry. Test-only: production never needs
    to forget a secret it has resolved."""
    _KNOWN_SECRET_VALUES.clear()

# Order matters: more specific/longer patterns run first so, e.g., a JWT
# embedded in a `Bearer <jwt>` header is redacted once as bearer_token
# rather than twice (once as bearer_token, again as jwt).
_PATTERN_DEFS = [
    # Anchored to the Authorization header context (DESIGN §12) so bare prose
    # using the words "Bearer"/"Basic" is never mangled (fix: IMPORTANT 3).
    ("bearer_token", r"Authorization:\s*Bearer\s+\S+", re.IGNORECASE),
    ("basic_auth", r"Authorization:\s*Basic\s+[A-Za-z0-9+/=]+", re.IGNORECASE),
    ("atlassian_token", r"ATATT[A-Za-z0-9_\-=]{20,}", 0),
    ("github_token", r"gh[pousr]_[A-Za-z0-9]{20,}", 0),
    ("slack_token", r"xox[abpsr]-[A-Za-z0-9-]{10,}", 0),
    ("aws_access_key_id", r"(A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}", 0),
    ("aws_secret_key", r"aws_secret_access_key\s*=\s*\S+", 0),
    ("api_key_assignment", r"api[_-]?key\s*[:=]\s*\S+", 0),
    ("password_assignment", r"password\s*[:=]\s*\S+", 0),
    # Consume the rest of the line, not just the first whitespace-delimited
    # token, so later Cookie/Set-Cookie pairs (e.g. `sessionid=...`) never
    # survive in plaintext (fix: CRITICAL 1).
    ("cookie_header", r"(?:Set-Cookie|Cookie):\s*[^\r\n]+", 0),
    ("jwt", r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}", 0),
    ("pem_private_key",
     r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    ("url_credentials", r"(?<=://)[^\s:/@]+:[^\s:/@]+@", 0),
    # Keyword prefix is optional (`[A-Za-z0-9_]*`, zero or more) so a bare
    # all-caps assignment like `PASSWORD=...`/`TOKEN=...` fires too, not only
    # names with a leading segment like `JIRA_API_TOKEN=...` (fix: CRITICAL 2).
    # The variable name is captured and kept in the replacement — only the
    # value is redacted (DESIGN §12: "value replaced, name kept") (fix:
    # IMPORTANT 4).
    ("env_secret_assignment",
     r"\b(?P<envname>[A-Za-z0-9_]*(?:TOKEN|SECRET|KEY|PASSWORD|PASSWD|CREDENTIAL)"
     r"[A-Za-z0-9_]*)\s*=\s*\S+", re.IGNORECASE),
]

PATTERNS: "list[tuple[str, re.Pattern]]" = [
    (name, re.compile(pattern, flags)) for name, pattern, flags in _PATTERN_DEFS
]

# Per-pattern replacement template, keyed by pattern name. Patterns not
# listed here use the default `[REDACTED:<name>]` (the whole match is
# swallowed). env_secret_assignment keeps its captured variable name.
_REPLACEMENTS = {
    "env_secret_assignment": r"\g<envname>=[REDACTED:env_secret_assignment]",
}

_SECRET_KEY_RE = re.compile(
    r".*(token|secret|key|password|passwd|credential|authorization|cookie).*",
    re.IGNORECASE,
)


def redact(text: str) -> "tuple[str, list[str]]":
    """Replace every credential-shaped substring of `text` with
    `[REDACTED:<pattern_name>]`. Returns the redacted text and the sorted,
    unique names of patterns that fired.

    A BARE `keychain://...`/`env://...` reference -- the whole string,
    nothing else -- is never a secret and is returned unchanged, untouched
    by either the known-secret registry or the pattern list below (Global
    Constraints: these are the only forms a config file or the database is
    allowed to hold for a credential). A string that merely CONTAINS a
    reference alongside other text is scanned normally -- only the whole
    string being the reference is exempt.
    """
    if _is_bare_reference(text):
        return text, []

    fired = set()
    for value in _KNOWN_SECRET_VALUES:
        if value in text:
            text = text.replace(value, "[REDACTED:known_secret]")
            fired.add("known_secret")
    for name, pattern in PATTERNS:
        replacement = _REPLACEMENTS.get(name, f"[REDACTED:{name}]")
        text, count = pattern.subn(replacement, text)
        if count:
            fired.add(name)
    return text, sorted(fired)


def looks_like_secret(s: str) -> bool:
    """True if `s` contains anything redact() would redact. A BARE
    `keychain://...`/`env://...` reference is never a secret; a string
    that merely contains one alongside other text is judged normally."""
    if not isinstance(s, str):
        return False
    if _is_bare_reference(s):
        return False
    return bool(redact(s)[1])


def redact_obj(obj):
    """Recursively redact every string in a nested dict/list. A dict value
    whose key looks like a credential name (token/secret/key/password/
    passwd/credential/authorization/cookie, case-insensitive substring) is
    replaced wholesale with `[REDACTED:<key>]`, regardless of its content —
    the key itself is enough signal even when the value doesn't match a
    pattern. Returns the redacted object and the sorted, unique names of
    patterns (and keys) that fired."""
    fired = set()

    def walk(node):
        if isinstance(node, dict):
            result = {}
            for key, value in node.items():
                if isinstance(key, str) and _SECRET_KEY_RE.match(key):
                    result[key] = f"[REDACTED:{key}]"
                    fired.add(key)
                else:
                    result[key] = walk(value)
            return result
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, str):
            redacted, names = redact(node)
            fired.update(names)
            return redacted
        return node

    result = walk(obj)
    return result, sorted(fired)
