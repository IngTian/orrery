import os
from pathlib import Path


def data_root() -> Path:
    """Return the data root directory, reading $ORRERY_HOME and expanding ~.

    If ORRERY_HOME is not set, defaults to ~/.local/share/orrery.
    """
    if env := os.environ.get("ORRERY_HOME"):
        return Path(env).expanduser()
    return Path.home() / ".local" / "share" / "orrery"


def log_dir() -> Path:
    """Return the log directory, creating it if needed.

    Defaults to ~/Library/Logs/orrery on macOS.
    """
    d = Path.home() / "Library" / "Logs" / "orrery"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_dir() -> Path:
    """Return the cache directory, creating it if needed.

    Defaults to ~/Library/Caches/orrery on macOS.
    """
    d = Path.home() / "Library" / "Caches" / "orrery"
    d.mkdir(parents=True, exist_ok=True)
    return d


def backup_dir() -> Path:
    """Return the backup directory, creating it if needed.

    Defaults to ~/Library/Application Support/orrery/backup on macOS.
    """
    d = Path.home() / "Library" / "Application Support" / "orrery" / "backup"
    d.mkdir(parents=True, exist_ok=True)
    return d
