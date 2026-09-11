"""Discovery and defensive streaming parsing of Claude Code transcript files.

Transcripts live at ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl where
encoded-cwd is the absolute cwd with every "/" replaced by "-".

The JSONL format is UNDOCUMENTED and may change at any time. Every function
here is defensive: unknown record types are ignored (summarize_record returns
None), malformed lines are skipped rather than raised on, and the file is
never loaded into memory in full.
"""
import json
import os
from pathlib import Path
from typing import Iterator, Optional

from work_ledger.timeutil import parse_iso

KNOWN_TYPES = frozenset({
    "user",
    "assistant",
    "system",
    "attachment",
    "mode",
    "permission-mode",
    "last-prompt",
    "ai-title",
    "file-history-snapshot",
    "file-history-delta",
    "bridge-session",
    "atis-latch",
    "queue-operation",
})


def projects_root() -> Path:
    """Root directory holding per-project transcript directories."""
    override = os.environ.get("CLAUDE_PROJECTS_DIR")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "projects"


def encode_cwd(cwd: str) -> str:
    """Encode an absolute cwd the way Claude Code names project directories."""
    return cwd.replace("/", "-")


def transcript_path_for(session_id: str, cwd: str) -> Optional[Path]:
    """Return the on-disk transcript path for a session, or None if absent."""
    if not session_id or not cwd:
        return None
    candidate = projects_root() / encode_cwd(cwd) / f"{session_id}.jsonl"
    if candidate.exists():
        return candidate
    return None


def _parse_line(line: str) -> Optional[dict]:
    stripped = line.strip()
    if not stripped:
        return None
    try:
        obj = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def _uuid_exists(path: Path, target_uuid: str, max_bytes: int) -> bool:
    bytes_read = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line_bytes = len(line.encode("utf-8", errors="replace"))
                if bytes_read + line_bytes > max_bytes:
                    break
                bytes_read += line_bytes
                obj = _parse_line(line)
                if obj is not None and obj.get("uuid") == target_uuid:
                    return True
    except OSError:
        return False
    return False


def iter_records(
    path,
    *,
    after_uuid: Optional[str] = None,
    max_bytes: int = 20_000_000,
) -> Iterator[dict]:
    """Stream dict records line by line from a transcript file.

    Skips lines that fail to parse or that parse to a non-dict value. Never
    raises on a missing file or on garbage lines. Stops once max_bytes have
    been consumed. When after_uuid is given, only records appearing strictly
    after the record with that uuid are yielded; if that uuid never appears
    in the file (it may have rotated or been truncated), everything is
    yielded instead.
    """
    path = Path(path)
    if not path.exists():
        return

    skip_until_found = after_uuid is not None and _uuid_exists(path, after_uuid, max_bytes)
    found = False

    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            bytes_read = 0
            for line in f:
                line_bytes = len(line.encode("utf-8", errors="replace"))
                if bytes_read + line_bytes > max_bytes:
                    break
                bytes_read += line_bytes

                obj = _parse_line(line)
                if obj is None:
                    continue

                if skip_until_found and not found:
                    if obj.get("uuid") == after_uuid:
                        found = True
                    continue

                yield obj
    except OSError:
        return


def _safe_timestamp(raw) -> Optional[str]:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parse_iso(raw)
    except (ValueError, TypeError):
        return None
    return raw


def _extract_content(content) -> tuple:
    """Return (text, tool_uses, tool_results) from a message.content value."""
    text_parts = []
    tool_uses = []
    tool_results = []

    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
            elif block_type == "tool_use":
                tool_uses.append({
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "input": block.get("input"),
                })
            elif block_type == "tool_result":
                tool_results.append({
                    "tool_use_id": block.get("tool_use_id"),
                    "content": block.get("content"),
                })
    # Any other shape (None, dict, number, ...) yields no text/tool evidence.

    return "\n".join(text_parts), tool_uses, tool_results


def summarize_record(rec: dict) -> Optional[dict]:
    """Normalize a raw transcript record, or None if it carries no work evidence.

    Only "user" and "assistant" records carry text/tool evidence in the
    observed format. Every other known type (system, attachment, mode, ...)
    and any unrecognized type return None so callers can count and skip them
    without special-casing an ever-growing list of record types.
    """
    if not isinstance(rec, dict):
        return None

    rtype = rec.get("type")
    if rtype not in ("user", "assistant"):
        return None

    message = rec.get("message")
    if not isinstance(message, dict):
        message = {}

    text, tool_uses, tool_results = _extract_content(message.get("content"))

    model = message.get("model")
    usage_raw = message.get("usage")
    usage = None
    if isinstance(usage_raw, dict):
        cleaned = {}
        for key in ("input_tokens", "output_tokens",
                    "cache_read_input_tokens", "cache_creation_input_tokens"):
            value = usage_raw.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                cleaned[key] = value
        usage = cleaned or None

    return {
        "uuid": rec.get("uuid"),
        "parent_uuid": rec.get("parentUuid"),
        "type": rtype,
        "role": message.get("role"),
        "timestamp": _safe_timestamp(rec.get("timestamp")),
        "cwd": rec.get("cwd"),
        "git_branch": rec.get("gitBranch"),
        "session_id": rec.get("sessionId"),
        "is_sidechain": bool(rec.get("isSidechain", False)),
        "text": text,
        "tool_uses": tool_uses,
        "tool_results": tool_results,
        "model": model if isinstance(model, str) and model else None,
        "usage": usage,
    }
