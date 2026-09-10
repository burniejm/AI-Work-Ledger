"""Path resolution for work-ledger's on-disk state.

Every path resolution goes through this module, which honors
WORK_LEDGER_HOME so tests never touch the real home directory.
"""
import os
from pathlib import Path


def home() -> Path:
    override = os.environ.get("WORK_LEDGER_HOME")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "work-ledger"


def db_path() -> Path:
    return home() / "work-ledger.sqlite"


def config_path() -> Path:
    return home() / "config.yaml"


def log_dir() -> Path:
    return home() / "logs"


def backup_dir() -> Path:
    return home() / "backups"


def state_dir() -> Path:
    return home() / "state"


def export_dir() -> Path:
    return home() / "exports"


def ensure_dirs() -> None:
    for p in (home(), backup_dir(), log_dir(), state_dir(), export_dir()):
        p.mkdir(parents=True, exist_ok=True)
        os.chmod(p, 0o700)
