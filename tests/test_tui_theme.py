"""Choosing a theme: `T`, a live preview you arrow through, and a line in config.toml.

The cases cover the theme at startup (configured, stale, missing or unreadable), the picker's
strip and its wrapping arrows, enter saving and esc restoring, and what may take the keyboard
mid-preview: the tabs, the chat input (by c or a click), a lost focus and a window too small.
"""

import pytest
from helpers import NOW, settle
from test_tui import _until
from textual.app import App
from textual.screen import ModalScreen
from textual.widgets import ContentSwitcher

from orrery import settings, store
from orrery.board import WeekBoard
from orrery.tui import themes
from orrery.tui.app import OrreryApp
from orrery.tui.chat import ChatInput
from orrery.tui.themes import ThemePicker
from orrery.tui.week import WeekView

SIZE = (120, 30)
STALE = "theme-that-was-removed"


def _app(home, theme: str | None = None) -> OrreryApp:
    if theme is not None:
        cfg = home / "config.toml"
        cfg.write_text(f'theme = "{theme}"\n' + cfg.read_text())
    return OrreryApp(root=lambda: home, now=lambda: NOW)


def _picker(app) -> ThemePicker:
    return app.query_one(ThemePicker)


async def _open(pilot, app) -> None:
    """T, then the keys after it once the picker has the keyboard: a key pressed before T's
    binding lands goes to the view, which had it."""
    await pilot.press("T")
    await _until(pilot, lambda: app.focused is _picker(app))


def _after(name: str, app) -> str:
    names = themes.names(app)
    return names[names.index(name) + 1]


# ── startup ──────────────────────────────────────────────────────────────
async def test_the_app_starts_on_the_configured_theme(week_home):
    app = _app(week_home, "nord")
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        assert app.theme == "nord"


@pytest.mark.parametrize("line", [None, f'theme = "{STALE}"', 'theme = ""', "theme = 3"])
async def test_missing_or_stale_theme_falls_back_and_settings_still_load(week_home, line):
    """A cosmetic setting never makes the data unavailable, nor raises
    InvalidThemeError."""
    cfg = week_home / "config.toml"
    if line is not None:
        cfg.write_text(line + "\n" + cfg.read_text())
    loaded = settings.load_settings(week_home)
    assert isinstance(loaded, settings.Settings)
    app = _app(week_home)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        assert app.theme == themes.DEFAULT
        assert isinstance(app.board, WeekBoard), app.board
        assert app.is_running


async def test_unreadable_settings_still_start_on_the_default(orrery_home):
    assert isinstance(settings.load_settings(orrery_home), store.Unavailable)
    app = _app(orrery_home)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        assert app.theme == themes.DEFAULT


async def test_the_picker_is_hidden_until_asked_for(week_home):
    app = _app(week_home)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        assert _picker(app).display is False


# ── opening ──────────────────────────────────────────────────────────────
async def test_T_opens_the_picker_on_the_current_theme(week_home):
    app = _app(week_home, "nord")
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await pilot.press("T")
        await pilot.pause()
        assert _picker(app).is_open
        assert _picker(app).selected == "nord"


async def test_it_opens_on_what_is_on_screen_not_on_what_config_says(week_home):
    app = _app(week_home, STALE)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await pilot.press("T")
        await pilot.pause()
        assert _picker(app).selected == themes.DEFAULT


async def test_the_picker_shows_the_options_and_where_you_are_in_them(week_home):
    app = _app(week_home, "nord")
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await pilot.press("T")
        await pilot.pause()
        p = _picker(app)
        body, title, sub = str(p.render()), str(p.border_title), str(p.border_subtitle)
        after = _after("nord", app)
        count = len(themes.names(app))
    assert "▸nord◂" in body
    assert "monokai" in body, f"nothing to the left of the cursor: {body!r}"
    assert after in body, f"nothing to the right of the cursor: {body!r}"
    assert f"of {count}" in title
    assert "restores nord" in sub


# ── previewing ───────────────────────────────────────────────────────────
async def test_right_applies_the_next_theme_immediately(week_home):
    app = _app(week_home, "nord")
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await _open(pilot, app)
        await pilot.press("right")
        await pilot.pause()
        assert app.theme == _after("nord", app)
        assert _picker(app).selected == app.theme


async def test_left_goes_back(week_home):
    app = _app(week_home, "nord")
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await _open(pilot, app)
        await pilot.press("right", "right", "left")
        await pilot.pause()
        assert app.theme == _after("nord", app)


async def test_the_ends_wrap(week_home):
    first = themes.names(App())[0]
    app = _app(week_home, first)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await _open(pilot, app)
        await pilot.press("left")
        await pilot.pause()
        assert app.theme == themes.names(app)[-1]
        await pilot.press("right")
        await pilot.pause()
        assert app.theme == first


async def test_previewing_writes_nothing(week_home):
    app = _app(week_home, "nord")
    before = (week_home / "config.toml").read_text()
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await _open(pilot, app)
        await pilot.press("right", "right", "right")
        await pilot.pause()
    assert (week_home / "config.toml").read_text() == before


async def test_the_arrows_do_not_walk_the_tabs_while_previewing(week_home):
    """The tabs are never focused and the arrows are the views' and the picker's, so a
    preview can't move you off the tab you are looking at."""
    app = _app(week_home, "nord")
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await _open(pilot, app)
        await pilot.press("right", "right", "left", "left", "left")
        await pilot.pause()
        assert app.query_one("#tabs", ContentSwitcher).current == "week"


async def test_tabs_are_still_reachable_while_previewing(week_home):
    """The preview is for every tab: tab switches it under the open picker, which keeps the
    keyboard, so the arrows go on previewing."""
    app = _app(week_home, "nord")
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await _open(pilot, app)
        await pilot.press("tab")
        await pilot.pause()
        assert app.query_one("#tabs", ContentSwitcher).current == "report"
        assert _picker(app).is_open, "switching tabs closed the picker"
        assert app.focused is _picker(app)
        before = app.theme
        await pilot.press("right")
        await pilot.pause()
        assert app.theme != before, "the arrows stopped previewing after a tab switch"
        await pilot.press("escape")
        await pilot.pause()
        assert app.theme == "nord"


# ── keeping and cancelling ───────────────────────────────────────────────
async def test_enter_keeps_it_and_writes_it_to_config(week_home):
    """enter is also the views' `open` key: the picker takes it first."""
    app = _app(week_home, "nord")
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await _open(pilot, app)
        await pilot.press("right")
        chosen = _picker(app).selected
        await pilot.press("enter")
        await settle(pilot, app)
        assert app.theme == chosen
        assert not _picker(app).is_open
        assert not isinstance(app.screen, ModalScreen), "enter opened something too"
    assert settings.load_settings(week_home).theme == chosen
    lines = (week_home / "config.toml").read_text().splitlines()
    assert [x for x in lines if x.startswith("theme")] == [f'theme = "{chosen}"']


@pytest.mark.parametrize(
    "error", [store.RefusedWrite("unreadable config.toml"), OSError("No space left on device")]
)
async def test_a_failed_save_keeps_the_theme_for_this_run_and_says_so(
    week_home, error, monkeypatch
):
    def refuse(*args, **kw):
        raise error

    monkeypatch.setattr(store, "atomic_write", refuse)
    app = _app(week_home, "nord")
    before = (week_home / "config.toml").read_text()
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await _open(pilot, app)
        await pilot.press("right")
        chosen = _picker(app).selected
        await pilot.press("enter")
        await settle(pilot, app)
        assert app.theme == chosen and not _picker(app).is_open
        note = str(app.query_one("#note").content)
        assert note == f"theme {chosen}, not saved: {error}" and "\n" not in note
    assert (week_home / "config.toml").read_text() == before


@pytest.mark.parametrize("configured, in_effect", [("nord", "nord"), (STALE, themes.DEFAULT)])
async def test_picker_esc_restores_theme_in_effect(week_home, configured, in_effect):
    """esc puts back what was on screen when the picker opened, not config.toml's name,
    which may be stale; and writes nothing."""
    app = _app(week_home, configured)
    before = (week_home / "config.toml").read_text()
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await _open(pilot, app)
        await pilot.press(*["right"] * 6)
        await pilot.pause()
        assert app.theme != in_effect, "nothing was previewed, so this proves nothing"
        await pilot.press("escape")
        await pilot.pause()
        assert app.theme == in_effect
        assert not _picker(app).is_open
        assert app.focused is app.query_one(WeekView), app.focused  # back to the view
    assert (week_home / "config.toml").read_text() == before


async def test_reopening_after_a_cancel_starts_where_you_are_again(week_home):
    app = _app(week_home, "nord")
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await _open(pilot, app)
        await pilot.press("right", "escape")
        await pilot.pause()
        await pilot.press("T")
        await pilot.pause()
        assert _picker(app).selected == "nord"


async def test_T_pressed_again_mid_preview_keeps_the_original_escape_target(week_home):
    app = _app(week_home, "nord")
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await _open(pilot, app)
        await pilot.press("right", "right")
        await pilot.pause()
        assert app.theme != "nord"
        await pilot.press("T")
        await pilot.pause()
        assert "restores nord" in str(_picker(app).border_subtitle)
        await pilot.press("escape")
        await pilot.pause()
        assert app.theme == "nord"


async def test_a_prompt_cancels_the_preview_rather_than_leaving_it_orphaned(week_home):
    """The picker's only exit is its own `on_key`, which needs focus. `c` opens the chat
    prompt, which takes focus, so the picker was left displayed but deaf: `esc` no longer
    restored the theme its subtitle named and `T` did nothing."""
    app = _app(week_home, "nord")
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await _open(pilot, app)
        await pilot.press("right")
        await pilot.pause()
        assert app.theme != "nord", "nothing was previewed, so this proves nothing"
        await pilot.press("c")
        await pilot.pause()
        prompt = app.query_one(ChatInput)
        assert _picker(app).is_open is False, "the picker stayed on screen behind the prompt"
        assert app.theme == "nord", "the preview was left applied with no way to undo it"
        # `cancel` posts no `Cancelled`: that would drop focus off the prompt just opened
        assert app.focused is prompt, f"the prompt lost focus to {app.focused}"
        await pilot.press("h", "i")
        assert prompt.value == "hi", f"typing did not reach the prompt: {prompt.value!r}"


async def test_a_click_on_the_chat_input_cancels_the_preview(week_home):
    """The input takes the keyboard by a click as by `c`: either way the preview is put back
    and the picker goes, so it's never left open and deaf behind the input."""
    app = _app(week_home, "nord")
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await _open(pilot, app)
        await pilot.press("right")
        await pilot.pause()
        assert app.theme != "nord", "nothing was previewed, so this proves nothing"
        await pilot.click(ChatInput)
        await pilot.pause()
        prompt = app.query_one(ChatInput)
        assert app.focused is prompt and not _picker(app).is_open and app.theme == "nord"
        await pilot.press("h", "i")
        assert prompt.value == "hi"


async def test_T_gives_the_keyboard_back_to_a_picker_that_lost_focus(week_home):
    """Anything else taking focus mid-preview (a view, say) leaves the picker open: `T`
    focuses it again rather than doing nothing, and esc still restores the original."""
    app = _app(week_home, "nord")
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await _open(pilot, app)
        await pilot.press("right")
        await pilot.pause()
        app.set_focus(None)
        await pilot.press("T")
        await pilot.pause()
        assert app.focused is _picker(app), app.focused
        await pilot.press("escape")
        await pilot.pause()
        assert app.theme == "nord"
        assert not _picker(app).is_open


async def test_going_below_the_minimum_size_cancels_the_preview(week_home):
    """The frame hidden by the minimum-size line takes the picker with it, and the keyboard:
    the preview is put back rather than left orphaned, and the picker is gone after."""
    app = _app(week_home, "nord")
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await _open(pilot, app)
        await pilot.press("right")
        await pilot.pause()
        assert app.theme != "nord", "nothing was previewed, so this proves nothing"
        await pilot.resize_terminal(79, 30)
        await pilot.pause()
        assert app.has_class("-tiny") and app.theme == "nord" and not _picker(app).is_open
        await pilot.resize_terminal(*SIZE)
        await pilot.pause()
        assert not _picker(app).is_open and app.theme == "nord"
        assert app.focused is app.query_one(WeekView)
