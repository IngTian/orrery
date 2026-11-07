"""Tests for orrery export and backup commands."""

import sqlite3

import pytest
from conftest import FROZEN_NOW

from orrery import db, export
from orrery.__main__ import main


def test_export_all_is_a_working_home(orrery_home, tmp_path, monkeypatch):
    """export --all creates a working orrery home."""
    monkeypatch.setattr("orrery.__main__.datetime", type("dt", (), {"now": lambda: FROZEN_NOW}))
    assert main(["init"]) == 0

    out = export.export_all(orrery_home, tmp_path / "x", FROZEN_NOW)
    assert out.name == "orrery-export-20261112T090000"
    assert out.exists()

    # Check it's a working home
    conn = db.open_existing(out)
    assert isinstance(conn, sqlite3.Connection)
    assert (out / "config.toml").exists()
    assert (out / "fronts.md").exists()


def test_export_all_via_cli(orrery_home, tmp_path, capsys, monkeypatch):
    """orrery export --all DIR prints the created directory."""
    monkeypatch.setattr("orrery.__main__.datetime", type("dt", (), {"now": lambda: FROZEN_NOW}))
    assert main(["init"]) == 0
    dest = tmp_path / "exports"
    dest.mkdir()

    assert main(["export", "--all", str(dest)]) == 0
    out = capsys.readouterr().out
    # Filter out "Created" lines from init
    lines = [line for line in out.splitlines() if not line.startswith("Created")]
    assert lines[-1].endswith("orrery-export-20261112T090000")
    assert (dest / "orrery-export-20261112T090000").exists()


def test_export_requires_all_flag(tmp_path, capsys):
    """export without --all is a usage error."""
    assert main(["export", str(tmp_path)]) != 0


def test_backup_writes_db(orrery_home, tmp_path, monkeypatch):
    """backup writes just the database."""
    monkeypatch.setattr("orrery.__main__.datetime", type("dt", (), {"now": lambda: FROZEN_NOW}))
    assert main(["init"]) == 0

    out = export.backup(orrery_home, tmp_path / "backups", FROZEN_NOW)
    assert out.name == "orrery-20261112T090000.db"
    assert out.exists()

    # Check it's a valid database
    conn = sqlite3.connect(out)
    version = conn.execute("pragma user_version").fetchone()[0]
    assert version == db.SCHEMA_VERSION
    conn.close()


def test_backup_via_cli(orrery_home, tmp_path, capsys, monkeypatch):
    """orrery backup DIR prints the created file."""
    monkeypatch.setattr("orrery.__main__.datetime", type("dt", (), {"now": lambda: FROZEN_NOW}))
    assert main(["init"]) == 0
    dest = tmp_path / "backups"
    dest.mkdir()

    assert main(["backup", str(dest)]) == 0
    out = capsys.readouterr().out
    # Filter out "Created" lines from init
    lines = [line for line in out.splitlines() if not line.startswith("Created")]
    assert lines[-1].endswith("orrery-20261112T090000.db")
    assert (dest / "orrery-20261112T090000.db").exists()


def test_export_and_backup_into_a_path_with_an_apostrophe(week_home, tmp_path):
    dest = tmp_path / "the user's backups"
    backup = export.backup(week_home, dest, FROZEN_NOW)
    home = export.export_all(week_home, dest, FROZEN_NOW)
    assert sqlite3.connect(backup).execute("select count(*) from subjects").fetchone() == (3,)
    assert isinstance(db.open_existing(home), sqlite3.Connection)


@pytest.mark.parametrize("argv", [["backup"], ["export", "--all"]])
def test_export_and_backup_without_a_db(orrery_home, tmp_path, capsys, argv):
    dest = tmp_path / "out"
    assert main([*argv, str(dest)]) == 1
    assert capsys.readouterr().out == "orrery: data unavailable (missing)\n"
    assert not dest.exists()


def test_today_prints(week_home, capsys, monkeypatch):
    """orrery today prints today's board."""
    monkeypatch.setattr("orrery.__main__.datetime", type("dt", (), {"now": lambda: FROZEN_NOW}))
    assert main(["today"]) == 0
    out = capsys.readouterr().out
    assert "Thu 11/12 · today" in out


def test_today_unavailable(orrery_home, capsys):
    """orrery today with no DB exits 1 and creates nothing."""
    assert main(["today"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("orrery: data unavailable (")
    assert list(orrery_home.iterdir()) == []
