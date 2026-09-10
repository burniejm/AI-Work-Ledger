"""Configuration loader, defaults, and validation for work-ledger.

Load order — CONTROLLER RULING, supersedes the Global Constraints' loader-order
sentence: ``_read_file`` tries ``work_ledger.yamlite`` FIRST, not PyYAML. yamlite
defines the canonical config subset and is always present, so every machine
parses config.yaml identically. Only when yamlite raises ``YamlLiteError`` (the
file uses syntax outside the yamlite subset) AND PyYAML is importable does it
fall back to ``yaml.safe_load``, so a hand-authored file richer than the subset
still loads somewhere rather than failing outright.

This order matters because PyYAML resolves YAML 1.1 idioms that yamlite
deliberately does not: for example ``enabled: no`` loads as the string ``"no"``
under yamlite but as the boolean ``False`` under PyYAML, and ``rollover_hour:
010`` loads as the int ``10`` under yamlite but as octal ``8`` under PyYAML
(``nominal_start_hour: 08`` similarly loads as int ``8`` under yamlite but as
the string ``"08"`` under PyYAML, since ``08`` is not valid octal). Without a
fixed, PyYAML-independent order, the same config file would silently mean a
different working day on a machine with PyYAML installed versus one without
it. ``save()`` always uses ``yamlite.dumps``, so a file this module wrote
itself never exercises the fallback — both loaders agree on it by construction
(the writer emits only the yamlite subset).

Ruling 29: no ``connections:``/``repositories:`` lists live in this file —
the SQLite ``connections``/``repositories`` tables are the single source of
truth (written by the Task 36 setup wizard). ``jira.enabled`` does not
exist here either: a Jira connection is enabled/disabled through
``connections.enabled`` in the database. ``nominal_start_hour`` is kept but
is informational only — no code branches on it.
"""
import copy
import os
import tempfile
from zoneinfo import ZoneInfo

from work_ledger import paths, redact, yamlite

DEFAULTS = {
    "timezone": "America/Chicago",
    "rollover_hour": 3,
    # informational only (Spec §7): the nominal start of the working day, shown in
    # reports and the setup completion report. No query or bucketing uses it.
    "nominal_start_hour": 8,
    # consumed by entities.current_device (Task 12) as the devices.hostname override
    "device_name": None,
    "session": {"idle_finalize_minutes": 30},
    "capture": {
        # consumed by capture.events.append_source_event (Task 22): every string leaf
        # of a payload is truncated to this many characters before the byte cap runs
        "excerpt_max_chars": 512,
        "payload_max_bytes": 8192,
        # THE authoritative drop list consumed by capture.events.is_interesting (Task 22)
        "noise_tools": ["Read", "Glob", "Grep", "TodoWrite", "NotebookRead",
                        "WebFetch", "WebSearch", "LS"],
    },
    "stage_b": {
        "enabled": True,
        "model": "sonnet",
        "timeout_seconds": 120,
        "min_interval_minutes": 20,
        "max_bundle_bytes": 60000,
    },
    "jira": {
        "max_refresh_on_start": 10,
        "max_refresh_per_turn": 20,
        "stale_after_hours": 12,
        "request_timeout_seconds": 10,
    },
    "git": {"subprocess_timeout_seconds": 2, "max_commits_per_reconcile": 50},
    "reports": {"status_workdays": 7, "stale_after_days": 14},
}

def _deep_merge(base: dict, override: dict) -> dict:
    """Dicts merge recursively; lists and scalars replace."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _read_file(text: str) -> dict:
    """Parse config.yaml text. yamlite first, always — see the module docstring
    for why PyYAML cannot be allowed to go first. Falls back to PyYAML only when
    yamlite rejects the text as outside its subset and PyYAML is importable."""
    try:
        parsed = yamlite.loads(text)
    except yamlite.YamlLiteError as yamlite_err:
        try:
            import yaml  # PyYAML, when importable
        except ImportError:
            raise yamlite_err from None  # no PyYAML: surface yamlite's own error
        parsed = yaml.safe_load(text)
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise yamlite.YamlLiteError("config file must be a top-level mapping")
    return parsed


def load() -> dict:
    """Deep-merge the config file over DEFAULTS. Creates the file with
    defaults when missing."""
    path = paths.config_path()
    if not path.exists():
        save(copy.deepcopy(DEFAULTS))
        return copy.deepcopy(DEFAULTS)
    file_cfg = _read_file(path.read_text())
    return _deep_merge(copy.deepcopy(DEFAULTS), file_cfg)


def save(cfg: dict) -> None:
    """Write cfg with mode 0600, atomically (tmp + os.replace).

    The temp file is created with mode 0600 from the moment it exists
    (`tempfile.mkstemp`'s default) rather than written first and chmod'd
    after, so there is never a window where the temp file is readable by
    anyone but the owner.

    The temp filename is unique per call (`tempfile.mkstemp`, not a fixed
    `config.yaml.tmp`): Task 38's concurrency hardening test found that
    several processes racing to call `save()` against the same brand-new
    `WORK_LEDGER_HOME` (each independently discovering no config.yaml
    exists yet and writing the defaults) would collide on a shared fixed
    tmp path -- the second process's `os.replace()` raised
    `FileNotFoundError` because the first had already consumed it.
    """
    paths.ensure_dirs()
    path = paths.config_path()
    text = yamlite.dumps(cfg)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp_path = path.parent / os.path.basename(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def get(path: str, default=None):
    """Dotted lookup against the loaded (merged) config, e.g. 'stage_b.model'."""
    cfg = load()
    node = cfg
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _iter_string_leaves(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield from _iter_string_leaves(value, child_path)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _iter_string_leaves(value, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def validate(cfg: dict) -> "list[str]":
    """Return human-readable problems. Does not validate connections or
    repositories — those live in SQLite (Ruling 29)."""
    problems = []

    timezone = cfg.get("timezone")
    try:
        ZoneInfo(timezone)
    except Exception:
        problems.append(f"unknown timezone: {timezone!r}")

    rollover_hour = cfg.get("rollover_hour")
    if isinstance(rollover_hour, bool) or not isinstance(rollover_hour, int) \
            or not (0 <= rollover_hour <= 23):
        problems.append(f"rollover_hour must be an integer between 0 and 23: {rollover_hour!r}")

    nominal_start_hour = cfg.get("nominal_start_hour")
    if isinstance(nominal_start_hour, bool) or not isinstance(nominal_start_hour, int) \
            or not (0 <= nominal_start_hour <= 23):
        problems.append(
            f"nominal_start_hour must be an integer between 0 and 23: {nominal_start_hour!r}"
        )

    capture = cfg.get("capture")
    capture = capture if isinstance(capture, dict) else {}

    payload_max_bytes = capture.get("payload_max_bytes")
    if isinstance(payload_max_bytes, bool) or not isinstance(payload_max_bytes, int):
        problems.append(
            f"capture.payload_max_bytes must be an integer: {payload_max_bytes!r}"
        )

    excerpt_max_chars = capture.get("excerpt_max_chars")
    if isinstance(excerpt_max_chars, bool) or not isinstance(excerpt_max_chars, int):
        problems.append(
            f"capture.excerpt_max_chars must be an integer: {excerpt_max_chars!r}"
        )

    noise_tools = capture.get("noise_tools")
    if not isinstance(noise_tools, list):
        problems.append(f"capture.noise_tools must be a list: {noise_tools!r}")

    for value_path, value in _iter_string_leaves(cfg):
        if redact.looks_like_secret(value):
            problems.append(
                f"{value_path} looks like a secret; use a keychain:// or env:// "
                "reference instead of a literal value"
            )

    return problems
