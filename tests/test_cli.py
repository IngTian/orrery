"""Tests for the orrery CLI commands."""

import sqlite3
from datetime import datetime

import pytest

from orrery import __version__, db, paths, schedule, settings
from orrery.__main__ import main

FROZEN_NOW = datetime(2026, 11, 12, 9, 0)


def test_version(capsys):
    """orrery --version prints version string."""
    assert main(["--version"]) == 0
    assert capsys.readouterr().out == f"orrery {__version__}\n"


def test_init_creates_and_seeds(orrery_home):
    """orrery init creates config, fronts.md, and a database seeded with no subjects."""
    assert main(["init"]) == 0
    conn = db.open_existing(orrery_home)
    assert isinstance(conn, sqlite3.Connection)
    assert schedule.subjects(conn) == []
    assert schedule.kinds(conn) == frozenset(settings.SEED_KINDS)


def test_init_idempotent(orrery_home):
    """orrery init doesn't re-seed existing database."""
    # First init
    assert main(["init"]) == 0
    conn = db.open_existing(orrery_home)
    assert isinstance(conn, sqlite3.Connection)

    # Second init should not fail
    assert main(["init"]) == 0
    conn2 = db.open_existing(orrery_home)
    assert isinstance(conn2, sqlite3.Connection)


def test_init_unavailable_settings_exit_1(orrery_home, capsys):
    """orrery init with unreadable settings after config write exits 1."""
    # Write broken config
    (orrery_home / "config.toml").write_text("day_boundary_hour = 'bad'\n")
    assert main(["init"]) == 1
    out = capsys.readouterr().out
    assert "orrery: data unavailable" in out


def test_init_with_a_bad_config_leaves_no_db(orrery_home, capsys):
    (orrery_home / "config.toml").write_text("day_boundary_hour = 30\n")
    assert main(["init"]) == 1
    assert capsys.readouterr().out.splitlines()[-1].startswith("orrery: data unavailable (")
    assert not db.db_path(orrery_home).exists()


def test_init_seeds_a_db_an_earlier_init_left_unseeded(orrery_home):
    (orrery_home / "config.toml").write_text(
        '[seed]\nsubjects = [["math", "Math"], ["finance", "Finance"]]\n'
    )
    db.open_db(orrery_home).close()  # as if an earlier init stopped before seeding
    assert main(["init"]) == 0
    conn = db.open_existing(orrery_home)
    assert [s.name for s in schedule.subjects(conn)] == ["math", "finance"]


@pytest.mark.parametrize("argv", [[], ["mcp"], ["today"]])
def test_tui_mcp_and_today_log_to_the_log_dir(week_home, tmp_path, monkeypatch, argv):
    monkeypatch.setattr("orrery.tui.app.OrreryApp.run", lambda self: None)
    monkeypatch.setattr("orrery.mcp_server.serve", lambda role: None)
    assert main(argv) == 0
    assert (tmp_path / "logs" / "orrery.log").exists()  # conftest's log_dir


def test_tui_default(orrery_home, monkeypatch):
    """orrery with no command tries to launch TUI."""
    # Mock the TUI app
    launched = []

    class MockApp:
        def __init__(self, *, root=paths.data_root, now=datetime.now):
            launched.append((root, now))

        def run(self):
            pass

    monkeypatch.setattr("orrery.tui.app.OrreryApp", MockApp)
    assert main([]) == 0
    assert len(launched) == 1


def test_mcp_command_serves(orrery_home, monkeypatch):
    """orrery mcp starts the MCP server."""
    served = []

    def mock_serve(role):
        served.append(role)

    monkeypatch.setattr("orrery.mcp_server.serve", mock_serve)
    assert main(["mcp"]) == 0
    assert served == ["mentor"]


def test_mcp_role_organizer(orrery_home, monkeypatch):
    """orrery mcp --role organizer."""
    served = []

    def mock_serve(role):
        served.append(role)

    monkeypatch.setattr("orrery.mcp_server.serve", mock_serve)
    assert main(["mcp", "--role", "organizer"]) == 0
    assert served == ["organizer"]


def test_today_before_starts_says_nothing_is_planned(week_home, capsys):
    """Before starts, as the TUI and the MCP tools say: none of the template's blocks."""
    assert main(["today", "--date", "2026-11-05"]) == 0  # a Thursday, before Mon 11/9
    assert capsys.readouterr().out == (
        "Thu 11/5 · today\nnothing planned: orrery starts Mon 11/9\n"
    )
    assert main(["today", "--date", "2026-11-12"]) == 0
    assert "math · learn ×2" in capsys.readouterr().out
