"""The theme list is Textual's, not ours: app.tcss uses only design tokens, so setting
`App.theme` re-themes the frame. The risk that buys is that the names belong to a dependency,
which is what `test_the_default_theme_exists` guards."""

from pathlib import Path

import pytest
from textual.app import App
from textual.theme import Theme

from orrery import settings
from orrery.tui import themes


def test_the_default_theme_exists():
    """Fails on a Textual upgrade that drops the default, not on a first run."""
    assert themes.DEFAULT in themes.names(App())


def test_settings_default_is_the_theme_default():
    """settings.py duplicates the literal to stay free of textual; this ties the two."""
    assert settings.Settings(root=Path()).theme == themes.DEFAULT


def test_names_are_sorted_and_non_empty():
    names = themes.names(App())
    assert names
    assert list(names) == sorted(names)


def test_the_well_known_themes_are_offered():
    names = set(themes.names(App()))
    for expected in ("gruvbox", "nord", "tokyo-night", "dracula", "catppuccin-latte"):
        assert expected in names


def test_names_follow_the_apps_registered_themes():
    app = App()
    app.register_theme(Theme(name="synthetic-test-theme", primary="#336699"))
    assert "synthetic-test-theme" in themes.names(app)


def test_resolve_accepts_a_known_name():
    assert themes.resolve("nord", App()) == "nord"


def test_resolve_falls_back_rather_than_raising():
    """A stale name in config.toml must never stop the app starting."""
    app = App()
    assert themes.resolve("no-such-theme", app) == themes.DEFAULT
    assert themes.resolve("", app) == themes.DEFAULT
    assert themes.resolve(None, app) == themes.DEFAULT


# ── the picker's window ──────────────────────────────────────────────────

NAMES = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf")


def test_the_cursor_is_marked_with_characters():
    """Not colour: the cursor sits on a name whose own colours change under it."""
    assert "▸charlie◂" in themes.strip(NAMES, 2, 80)


def test_neighbours_on_both_sides_are_offered():
    out = themes.strip(NAMES, 3, 80)
    assert "charlie" in out and "echo" in out


@pytest.mark.parametrize("width", [20, 30, 40, 60, 80, 110, 200])
@pytest.mark.parametrize("index", [0, 3, 6])
def test_the_window_never_exceeds_the_width(width, index):
    out = themes.strip(NAMES, index, width)
    assert len(out) <= width, f"{len(out)} columns in {width}: {out!r}"


def test_a_width_too_small_for_even_one_name_still_shows_the_selection():
    assert "charlie" in themes.strip(NAMES, 2, 4)


def test_more_room_shows_more_options():
    assert len(themes.strip(NAMES, 3, 30)) < len(themes.strip(NAMES, 3, 80))


def test_the_full_list_needs_no_ellipsis():
    out = themes.strip(NAMES, 3, 200)
    assert "…" not in out
    assert all(n in out for n in NAMES)


def test_a_truncated_side_says_so():
    out = themes.strip(NAMES, 0, 30)
    assert out.endswith("…") and not out.startswith("…")
    out = themes.strip(NAMES, 6, 30)
    assert out.startswith("…") and not out.endswith("…")


def test_an_empty_list_renders_nothing_rather_than_raising():
    assert themes.strip((), 0, 80) == ""


@pytest.mark.parametrize("index", [-5, -1, 7, 999])
def test_an_index_outside_the_list_is_clamped(index):
    assert "▸" in themes.strip(NAMES, index, 80)
