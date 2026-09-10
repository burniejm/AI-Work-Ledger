"""yamlite: a minimal, pure-stdlib YAML subset reader and writer.

Supports (deliberately small — Design D2):
- nested block mappings (2-space indent)
- block sequences (``- item``)
- sequences of mappings (``- key: value`` with following lines aligned)
- scalars of type str/int/bool/null
- single- and double-quoted strings
- full-line and trailing ``#`` comments
- blank lines

Not supported: anchors, aliases, multi-line scalars, flow collections,
multi-document streams, floats, dates. Any of these raise YamlLiteError.

``dumps`` has one further limitation worth calling out: the subset has no
block-style way to write an *empty* list or mapping (that would require
flow syntax ``[]``/``{}``, which is out of scope), so ``dumps`` raises
``YamlLiteError`` rather than silently emitting something that would read
back as ``null``. Callers should omit a key instead of passing ``[]``/``{}``.

This module must never import PyYAML and must remain pure stdlib.
"""
import re

__all__ = ["loads", "dumps", "YamlLiteError"]


class YamlLiteError(Exception):
    """Raised when input text uses syntax outside the yamlite subset."""


_INT_RE = re.compile(r"^-?\d+$")
_QUOTE_CHARS = set("-?*&!|>%@`")
_BLOCK_SCALAR_RE = re.compile(r"^[|>][+-]?\d*$")


def _quote_opens_here(s: str, i: int) -> bool:
    """Is s[i] the legitimate start of a quoted scalar?

    A quote character only opens a quoted region when it is the first
    character of a value/key token: at the very start of the string, or
    immediately after a mapping colon (``": "``) or a sequence dash
    (``"- "``). A quote character appearing anywhere else (e.g. an
    apostrophe inside an unquoted plain scalar like ``don't``) is just a
    literal character.
    """
    if s[i] not in ("'", '"'):
        return False
    return i == 0 or s[i - 2:i] in (": ", "- ")


def _find_quote_end(s: str, i: int) -> int:
    """Return the index just past the closing quote that opens at s[i].

    Honors backslash-escaping inside double-quoted strings and YAML's
    doubled-single-quote (``''``) escape inside single-quoted strings.
    Raises YamlLiteError if the quote is never closed.
    """
    quote = s[i]
    n = len(s)
    j = i + 1
    while j < n:
        c = s[j]
        if quote == '"':
            if c == "\\":
                j += 2
                continue
            if c == '"':
                return j + 1
        else:
            if c == "'":
                if j + 1 < n and s[j + 1] == "'":
                    j += 2
                    continue
                return j + 1
        j += 1
    raise YamlLiteError(f"unterminated quoted string: {s!r}")


def _strip_trailing_comment(s: str) -> str:
    """Strip a trailing ``#`` comment, honoring quotes.

    A ``#`` only starts a comment when it is preceded by whitespace (or is
    at the very start of the remaining text) and is not inside a quoted
    string. Quote characters that are not a legitimate quote-open (see
    ``_quote_opens_here``) — e.g. an apostrophe inside an unquoted plain
    scalar — are treated as literal text, not as toggling quote state.
    """
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if _quote_opens_here(s, i):
            i = _find_quote_end(s, i)
            continue
        if c == "#" and (i == 0 or s[i - 1].isspace()):
            return s[:i]
        i += 1
    return s


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        inner = s[1:-1]
        if "\\" in inner:
            # Support only the simple escapes we might emit ourselves.
            out = []
            i = 0
            while i < len(inner):
                c = inner[i]
                if c == "\\" and i + 1 < len(inner):
                    nxt = inner[i + 1]
                    if nxt == "n":
                        out.append("\n")
                    elif nxt == "t":
                        out.append("\t")
                    elif nxt in ('"', "\\"):
                        out.append(nxt)
                    else:
                        out.append(nxt)
                    i += 2
                else:
                    out.append(c)
                    i += 1
            return "".join(out)
        return inner
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        return s[1:-1].replace("''", "'")
    return s


def _is_quoted(s: str) -> bool:
    return (len(s) >= 2 and s[0] == '"' and s[-1] == '"') or (
        len(s) >= 2 and s[0] == "'" and s[-1] == "'"
    )


def _coerce_scalar(raw: str):
    raw = raw.strip()
    if raw == "":
        return None
    if _is_quoted(raw):
        return _unquote(raw)
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw in ("null", "~"):
        return None
    if _INT_RE.match(raw):
        return int(raw)
    if raw.startswith("&") or raw.startswith("*"):
        raise YamlLiteError(f"anchors/aliases are not supported: {raw!r}")
    if raw.startswith(("[", "{")):
        raise YamlLiteError(f"flow collections are not supported: {raw!r}")
    if _BLOCK_SCALAR_RE.match(raw):
        raise YamlLiteError(f"multi-line block scalars are not supported: {raw!r}")
    return raw


def _split_key_value(line: str) -> tuple:
    """Split 'key: value' respecting quotes. Returns (key, value_str_or_None).

    Uses the same quote-open/quote-end rules as ``_strip_trailing_comment``
    so a stray apostrophe in an unquoted value (``note: don't``) never gets
    mistaken for the start of a quoted string, and a legitimately quoted
    key or value (including one containing a backslash-escaped quote, or a
    literal ``:`` ) is skipped over intact rather than split on an internal
    colon.
    """
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if _quote_opens_here(line, i):
            i = _find_quote_end(line, i)
            continue
        if c == ":" and (i + 1 == n or line[i + 1].isspace()):
            key = line[:i].strip()
            value = line[i + 1:].strip()
            return key, value
        i += 1
    return None, None


def _tokenize(text: str):
    """Yield (indent, content) for non-blank, non-full-comment lines."""
    tokens = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip("\n")
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            continue
        if "\t" in line[: len(line) - len(line.lstrip(" \t"))]:
            raise YamlLiteError(f"tabs are not supported for indentation (line {lineno})")
        indent = len(line) - len(line.lstrip(" "))
        content = _strip_trailing_comment(line[indent:]).rstrip()
        if content == "":
            continue
        if "---" == content or "..." == content:
            raise YamlLiteError(f"multi-document streams are not supported (line {lineno})")
        tokens.append((indent, content, lineno))
    return tokens


class _Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return None

    def parse(self):
        if not self.tokens:
            return {}
        indent, content, lineno = self.peek()
        if content.startswith("- "):
            return self._parse_sequence(indent)
        return self._parse_mapping(indent)

    def _parse_mapping(self, indent):
        result = {}
        while True:
            tok = self.peek()
            if tok is None or tok[0] != indent:
                break
            cur_indent, content, lineno = tok
            if content.startswith("- "):
                raise YamlLiteError(f"unexpected sequence item (line {lineno})")
            key_raw, value_raw = _split_key_value(content)
            if key_raw is None:
                raise YamlLiteError(f"expected 'key: value' (line {lineno}): {content!r}")
            key = _coerce_scalar(key_raw)
            if not isinstance(key, str):
                key = key_raw.strip()
            self.pos += 1
            if value_raw == "" or value_raw is None:
                # value is a nested block on subsequent, deeper-indented lines
                nxt = self.peek()
                if nxt is not None and nxt[0] > cur_indent:
                    child_indent, child_content, _ = nxt
                    if child_content.startswith("- "):
                        result[key] = self._parse_sequence(child_indent)
                    else:
                        result[key] = self._parse_mapping(child_indent)
                else:
                    result[key] = None
            else:
                result[key] = _coerce_scalar(value_raw)
        return result

    def _parse_sequence(self, indent):
        result = []
        while True:
            tok = self.peek()
            if tok is None or tok[0] != indent:
                break
            cur_indent, content, lineno = tok
            if not content.startswith("- "):
                break
            item_text = content[2:]
            # Could be a scalar, or the start of an inline mapping entry
            # ("- key: value") whose siblings are indented to align with
            # the first key (i.e. at indent + 2).
            key_raw, value_raw = _split_key_value(item_text)
            self.pos += 1
            if key_raw is not None:
                # sequence of mappings: "- key: value"
                mapping = {}
                key = _coerce_scalar(key_raw)
                if not isinstance(key, str):
                    key = key_raw.strip()
                if value_raw == "":
                    nxt = self.peek()
                    sub_indent = cur_indent + 2
                    if nxt is not None and nxt[0] > cur_indent and nxt[0] == sub_indent:
                        nxt_indent, nxt_content, _ = nxt
                        if nxt_content.startswith("- "):
                            mapping[key] = self._parse_sequence(nxt_indent)
                        else:
                            mapping[key] = self._parse_mapping(nxt_indent)
                    else:
                        mapping[key] = None
                else:
                    mapping[key] = _coerce_scalar(value_raw)
                # remaining keys of this same mapping item, aligned at
                # indent + 2 (same as the first inline key's continuation)
                sub_indent = cur_indent + 2
                while True:
                    nxt = self.peek()
                    if nxt is None or nxt[0] != sub_indent:
                        break
                    if nxt[1].startswith("- "):
                        break
                    rest = self._parse_mapping(sub_indent)
                    mapping.update(rest)
                    break
                result.append(mapping)
            else:
                result.append(_coerce_scalar(item_text))
        return result


def loads(text: str) -> dict:
    """Parse a yamlite-subset YAML document into a dict.

    Raises YamlLiteError on syntax outside the supported subset.
    """
    tokens = _tokenize(text)
    parser = _Parser(tokens)
    result = parser.parse()
    if not isinstance(result, dict):
        raise YamlLiteError("top-level document must be a mapping")
    return result


def _needs_quote(s: str) -> bool:
    if s == "":
        return True
    if s != s.strip():
        return True
    if ":" in s or "#" in s:
        return True
    if s[0] in _QUOTE_CHARS:
        return True
    if s.startswith('"') or s.startswith("'"):
        return True
    # Would the loader coerce this to a non-string, or reject it outright
    # (e.g. a plain string that looks like a flow collection or a block
    # scalar indicator, such as "[DRAFT] x" or "|weird")? Either way it
    # needs quoting rather than being emitted as-is.
    try:
        coerced = _coerce_scalar(s)
    except YamlLiteError:
        return True
    if not isinstance(coerced, str):
        return True
    return False


def _quote(s: str) -> str:
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _format_scalar(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        if _needs_quote(value):
            return _quote(value)
        return value
    raise YamlLiteError(f"unsupported value type for yamlite: {type(value)!r}")


def _dump_mapping(data: dict, indent: int, lines: list):
    pad = " " * indent
    for key, value in data.items():
        if not isinstance(key, str):
            raise YamlLiteError(f"unsupported key type for yamlite: {type(key)!r}")
        key_out = _quote(key) if _needs_quote(key) else key
        if isinstance(value, dict):
            if not value:
                raise YamlLiteError(
                    "empty mapping has no block representation in the yamlite "
                    "subset (flow collections are not supported)"
                )
            lines.append(f"{pad}{key_out}:")
            _dump_mapping(value, indent + 2, lines)
        elif isinstance(value, list):
            if not value:
                raise YamlLiteError(
                    "empty sequence has no block representation in the yamlite "
                    "subset (flow collections are not supported)"
                )
            lines.append(f"{pad}{key_out}:")
            _dump_sequence(value, indent + 2, lines)
        else:
            lines.append(f"{pad}{key_out}: {_format_scalar(value)}")


def _dump_sequence(data: list, indent: int, lines: list):
    pad = " " * indent
    for item in data:
        if isinstance(item, dict):
            if not item:
                raise YamlLiteError("empty mapping in sequence is not supported")
            entries = list(item.items())
            first_key, first_value = entries[0]
            first_key_out = _quote(first_key) if _needs_quote(first_key) else first_key
            if isinstance(first_value, (dict, list)):
                raise YamlLiteError(
                    "nested collections as first key of a sequence-of-mappings "
                    "entry are not supported by dumps"
                )
            lines.append(f"{pad}- {first_key_out}: {_format_scalar(first_value)}")
            rest = dict(entries[1:])
            if rest:
                _dump_mapping(rest, indent + 2, lines)
        elif isinstance(item, list):
            raise YamlLiteError("nested sequences-of-sequences are not supported by dumps")
        else:
            lines.append(f"{pad}- {_format_scalar(item)}")


def dumps(data: dict) -> str:
    """Serialize a dict to yamlite-subset YAML text.

    Emits only the supported subset; raises YamlLiteError for value types
    it cannot represent (e.g. float), and — because the subset has no
    block-style syntax for an empty collection — for any ``[]`` or ``{}``
    value anywhere in the structure. Omit the key instead of passing an
    empty list/dict.
    """
    if not isinstance(data, dict):
        raise YamlLiteError("dumps only supports a top-level mapping")
    lines = []
    _dump_mapping(data, 0, lines)
    if not lines:
        return ""
    return "\n".join(lines) + "\n"
