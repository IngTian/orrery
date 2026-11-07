"""Export and backup functions for orrery."""

import shutil
from contextlib import closing
from datetime import datetime
from pathlib import Path

from orrery import db, store


def export_all(root: Path, dest: Path, now: datetime) -> Path | store.Unavailable:
    """Export all orrery data to a new directory.

    Creates dest/orrery-export-{now:%Y%m%dT%H%M%S}/ containing:
    - orrery.db (via VACUUM INTO)
    - config.toml (if exists)
    - fronts.md (if exists)

    Setting ORRERY_HOME to that directory gives a working orrery.

    Args:
        root: The orrery data root directory.
        dest: The destination parent directory.
        now: The current datetime for timestamp.

    Returns:
        The created export directory path, or Unavailable (with nothing created) when the
        database can't be opened.
    """
    conn = db.open_existing(root)
    if isinstance(conn, store.Unavailable):
        return conn
    export_dir = dest / f"orrery-export-{now.strftime('%Y%m%dT%H%M%S')}"
    with closing(conn):
        export_dir.mkdir(parents=True, exist_ok=True)
        # bound, not formatted: a path may hold an apostrophe
        conn.execute("VACUUM INTO ?", (str(export_dir / "orrery.db"),))

    # Copy config.toml if it exists
    config_src = root / "config.toml"
    if config_src.exists():
        shutil.copy2(config_src, export_dir / "config.toml")

    # Copy fronts.md if it exists
    fronts_src = root / "fronts.md"
    if fronts_src.exists():
        shutil.copy2(fronts_src, export_dir / "fronts.md")

    return export_dir


def backup(root: Path, dest: Path, now: datetime) -> Path | store.Unavailable:
    """Backup just the orrery database.

    Creates dest/orrery-{now:%Y%m%dT%H%M%S}.db.

    Args:
        root: The orrery data root directory.
        dest: The destination directory.
        now: The current datetime for timestamp.

    Returns:
        The created backup file path, or Unavailable (with nothing created) when the database
        can't be opened.
    """
    conn = db.open_existing(root)
    if isinstance(conn, store.Unavailable):
        return conn
    backup_path = dest / f"orrery-{now.strftime('%Y%m%dT%H%M%S')}.db"
    with closing(conn):
        dest.mkdir(parents=True, exist_ok=True)
        conn.execute("VACUUM INTO ?", (str(backup_path),))
    return backup_path
