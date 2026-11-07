"""The frame, through the Pilot: the tabs, the docked chat and which keys reach it, its
scroll-back, and the sizes it adapts to (the strip under 110 columns, one line under 80×24)."""

import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
from fake_claude import INIT_OK, delta, make_fake, read_calls, result_ok
from helpers import (
    NOW,
    TODAY,
    chat_keys,
    keep_move,
    leave_chat,
    propose_move,
    remember_at,
    settle,
    state_of,
)
from test_tui import _counting_reloads, _text, _until
from test_tui_chat import _head, _organizer_done, _talk
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.color import Color
from textual.content import Content
from textual.geometry import Region
from textual.widgets import ContentSwitcher, Static, Tabs

from orrery import db, ledger, ops, proposals, schedule
from orrery.organizer import sessions
from orrery.tui import app as tui_app
from orrery.tui import chat as tui_chat
from orrery.tui import keymap
from orrery.tui.app import OrreryApp
from orrery.tui.chat import PAGE, ChatInput, ChatPanel
from orrery.tui.diff import chat_bar, render_diff
from orrery.tui.views import Confirm, Popup
from orrery.tui.week import WeekView

WIDE = (120, 40)
NARROW = (100, 32)
TOMORROW = TODAY + timedelta(days=1)
TOO_SMALL = "orrery needs at least 80×24"


def _app(home, fake=None) -> OrreryApp:
    return OrreryApp(
        root=lambda: home, now=lambda: NOW, claude_path=str(fake) if fake is not None else None
    )


class _Spy(OrreryApp):
    """up and down bound above the chat, as a view's arrows are: the chat must stop them."""

    CSS_PATH = Path(tui_app.__file__).with_name("app.tcss")  # else relative to this file
    BINDINGS = [*OrreryApp.BINDINGS, Binding("up", "spy('up')"), Binding("down", "spy('down')")]

    def action_spy(self, key: str) -> None:
        self.spied.append(key)


async def test_tab_switches_panes_but_not_from_the_chat_input(week_home):
    """tab and shift+tab walk the tabs (and wrap); from the chat input or over a popup
    they do nothing, and type nothing."""
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        panes, tabs = app.query_one("#tabs", ContentSwitcher), app.query_one(Tabs)
        assert panes.current == "week" and not tabs.can_focus
        assert {w.id for w in app.query("#tabs > *")} == {"week", "report"}
        await pilot.press("tab")
        await pilot.pause()
        assert panes.current == "report" and tabs.active == "tab-report"
        await pilot.press("tab")
        await pilot.pause()
        assert panes.current == "week"
        await pilot.press("shift+tab")
        await pilot.pause()
        assert panes.current == "report"
        await chat_keys(pilot, app, "tab", "shift+tab", "tab")
        await pilot.pause()
        chat = app.query_one(ChatInput)
        assert panes.current == "report" and chat.has_focus and chat.value == ""
        await leave_chat(pilot, app, "question_mark", "tab")
        await pilot.pause()
        assert isinstance(app.screen, Popup) and panes.current == "report"
        await pilot.press("escape")
        await pilot.click("#tab-week")
        await pilot.pause()
        assert panes.current == "week"


@pytest.mark.parametrize("size, width", [((235, 60), 70), ((120, 40), 37), ((300, 60), 72)])
async def test_chat_is_30_percent_clamped(week_home, size, width):
    """Docked, the chat is 30% of the window, from 37 to 72 columns, and follows a resize."""
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        chat = app.query_one(ChatPanel)
        assert chat.region.width == 37
        await pilot.resize_terminal(*size)
        await _until(pilot, lambda: chat.region.width == width)
        assert chat.region.right == size[0]


async def test_the_tabs_never_give_way_to_the_controls(week_home):
    """However long a tab's controls get, both tab names stay whole in the header."""
    app = _app(week_home)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(pilot, app)
        app.query_one("#controls", Static).update("x" * 80)
        await pilot.pause()
        assert " orrery  Week  Report " in _screen_lines(app)[0]


async def test_panels_class_at_160_and_not_159(week_home):
    """From 160 columns the frame's screen has -panels, beside -wide. Like -narrow and -wide
    it's set at the app's resize, so the screen already has it when it hears of the resize."""
    app = _app(week_home)
    async with app.run_test(size=(159, 45)) as pilot:
        await settle(pilot, app)
        base, heard = app.screen, []
        assert base.has_class("-wide") and not base.has_class("-panels")
        resized = base._screen_resized

        def spy(size) -> None:
            heard.append((size.width, base.has_class("-panels")))
            resized(size)

        base._screen_resized = spy
        await pilot.resize_terminal(160, 45)
        await _until(pilot, lambda: heard)
        assert set(heard) == {(160, True)} and base.has_class("-wide")
        heard.clear()
        await pilot.resize_terminal(159, 45)
        await _until(pilot, lambda: heard)
        assert set(heard) == {(159, False)} and base.has_class("-wide")


async def test_opened_at_235x60_has_panels_from_the_first_frame(week_home):
    """Opened straight at 235×60, no resize after: the screen has -panels from the first
    frame drawn, and the lower row is side by side from the first frame it's shown in."""
    app = _app(week_home)
    frames, display = [], app._display

    def spy(screen, renderable) -> None:
        if renderable is not None and not app._batch_count and screen is app.screen_stack[0]:
            lower, left, day = (app.query_one(f"#{x}") for x in ("lower", "lower-left", "day"))
            beside = day.region.x >= left.region.right > 0 and day.region.y == left.region.y
            frames.append((screen.has_class("-panels"), lower.display, beside))
        display(screen, renderable)

    app._display = spy
    async with app.run_test(size=(235, 60)) as pilot:
        await settle(pilot, app)
    shown = [beside for _, lower, beside in frames if lower]
    assert frames and all(panels for panels, *_ in frames), frames
    assert shown and all(shown), frames


@pytest.mark.parametrize("theme", ["gruvbox", "catppuccin-latte"])
async def test_panel_borders_stand_out_from_the_background(week_home, theme):
    """Dark or light, every panel's border is clearly lighter or darker than what it's drawn
    on, and the edge between a title and its caption is drawn in the border's colour, also
    after the theme changes under it."""
    app = _app(week_home)
    async with app.run_test(size=(235, 60)) as pilot:
        await settle(pilot, app)
        app.theme = "catppuccin-latte" if theme == "gruvbox" else "gruvbox"
        await settle(pilot, app)
        app.theme = theme
        await settle(pilot, app)
        week = {"chat-body", "week-view", "so-far", "last-weeks", "day"}
        for ids in (week, {"chat-body", "report-view", "your-stats", "evaluations"}):
            panels = [w for w in app.query(".panel") if w.region.area]  # those on screen
            assert {w.id for w in panels} == ids
            for w in panels:
                top = w.render_lines(Region(0, 0, *w.outer_size))[0]
                corner, *rest = (s for s in top if s.text.strip())
                # the edge between the title and the caption: ` ──── `, not the corner's `─`
                edge = next(s for s in rest if s.text.startswith(" ─") and s.text.endswith("─ "))
                fg, bg = Color.from_rich_color(corner.style.color), corner.style.bgcolor
                assert abs(fg.brightness - Color.from_rich_color(bg).brightness) > 0.15, (w.id, fg)
                assert edge.style.color == corner.style.color, w.id
            await pilot.press("tab")
            await settle(pilot, app)


@pytest.mark.parametrize("theme", ["gruvbox", "catppuccin-latte"])
async def test_popup_and_dialog_borders_stand_out_as_the_panels_do(week_home, theme):
    """The ? popup's and the new-chat dialog's borders are as clear as a panel's, dark or
    light."""
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        app.theme = theme
        await settle(pilot, app)
        for key, box in (("question_mark", "#popup-scroll"), ("x", "#popup-text")):
            await pilot.press(key)
            await pilot.pause()
            w = app.screen.query_one(box)
            top = w.render_lines(Region(0, 0, *w.outer_size))[0]
            corner = next(s for s in top if s.text.strip())
            fg, bg = (Color.from_rich_color(c) for c in (corner.style.color, corner.style.bgcolor))
            assert corner.text == "╭" and abs(fg.brightness - bg.brightness) > 0.15, (key, fg, bg)
            await pilot.press("escape")
            await pilot.pause()


@pytest.mark.parametrize("theme", ["gruvbox", "catppuccin-latte"])
async def test_chat_is_a_titled_panel(week_home, theme):
    """The chat is a bordered panel: Organizer in the accent on the left of its top border,
    since when this chat has run muted on the right, the edge drawn between them."""
    app = _app(week_home)
    async with app.run_test(size=(235, 60)) as pilot:
        app.theme = theme
        await settle(pilot, app)
        body, top = app.query_one("#chat-body"), _head(app)
        assert body.has_class("panel") and len(top.text) == 70
        assert top.text.startswith("╭─ Organizer ──") and top.text.endswith("── since Thu 11/12 ─╮")
        (title,) = (s for s in top if s.text.strip() == "Organizer")
        (since,) = (s for s in top if "since" in s.text)
        assert title.style.color.triplet == _rgb(app, "accent") and title.style.bold
        over = Color.parse(app.get_css_variables()["foreground-muted"])
        assert since.style.color.triplet == (body.background_colors[1] + over).rgb
        assert not since.style.bold


async def test_opens_on_starts_when_it_is_in_the_future(week_home):
    """Before orrery starts, the Week tab opens on the start day, its week read at once and
    only once; t still goes to today."""
    starts = TODAY + timedelta(days=9)  # Sat 11/21, next week
    with closing(db.open_existing(week_home)) as conn:
        with ledger.change(conn, actor="orrery", kind="setup", now=NOW):
            conn.execute("update settings set value = ? where key = 'starts'", (str(starts),))
    app = _app(week_home)
    reloads = _counting_reloads(app)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        view = app.query_one(WeekView)
        assert app.cursor_date == view.focus_date == starts and reloads == [1]
        assert view.board.monday == schedule.week_start(starts)
        await pilot.press("t")
        await settle(pilot, app)
        assert app.cursor_date == view.focus_date == TODAY
        assert view.board.monday == schedule.week_start(TODAY)


async def test_initial_focus_is_not_the_input_and_c_focuses_it(week_home):
    """The week view has the keyboard at start, so q quits rather than typing."""
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        assert app.focused is app.query_one(WeekView)
        await pilot.press("c")
        await pilot.pause()
        assert app.focused is app.query_one(ChatInput)


async def test_letters_and_up_down_stay_in_chat_and_q_does_not_quit(week_home):
    """With the input focused every letter types, and up and down go no further."""
    app = _Spy(root=lambda: week_home, now=lambda: NOW)
    app.spied = []
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        view = app.query_one(WeekView)
        app.set_focus(None)  # the view's own arrows would take it first
        await pilot.press("up")
        assert app.spied == ["up"], "the spy is live outside the chat"
        await chat_keys(pilot, app, *"quMhxfrTyn", "up", "down", "down")
        await settle(pilot, app)
        chat = app.query_one(ChatInput)
        assert chat.value == "quMhxfrTyn" and chat.has_focus
        assert app.spied == ["up"] and view.item == 0, "the timetable moved"
        assert app.is_running and len(app.screen_stack) == 1
        assert not app.query_one("#theme-picker").display


async def test_esc_cancels_a_running_turn_else_returns_focus(week_home, tmp_path):
    """esc in the chat cancels the turn and keeps the input; a second esc leaves it."""
    fake = make_fake(tmp_path, {"events": [INIT_OK, result_ok("too late")], "sleep": 30})
    app = _app(week_home, fake)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        await chat_keys(pilot, app, *"hello", "enter")
        await _until(pilot, lambda: read_calls(fake))
        chat = app.query_one(ChatInput)
        assert chat.has_focus and chat.value == ""
        await pilot.press("escape")
        await _until(pilot, lambda: _organizer_done(app))
        await settle(pilot, app)
        assert _talk(app)[-2:] == ["you: hello", "orrery: turn cancelled"]
        assert chat.has_focus, "the first esc only cancels the turn"
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is app.query_one(WeekView), "back to the view"


async def test_y_n_act_on_shown_proposal_when_input_not_focused(week_home):
    """In the input y and n are letters; out of it they keep and discard, whichever
    tab is open."""
    with closing(db.open_existing(week_home)) as conn:
        p = propose_move(conn)
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        assert _text(app, "#proposal").startswith(f"proposal #{p.id} · organizer")
        await chat_keys(pilot, app, "y", "n")
        await settle(pilot, app)
        assert app.query_one(ChatInput).value == "yn"
        with closing(db.open_existing(week_home)) as conn:
            assert state_of(conn, p.id) == "waiting"
        await leave_chat(pilot, app, "tab", "y")
        await settle(pilot, app)
        assert app.query_one("#tabs", ContentSwitcher).current == "report"
        assert _text(app, "#note") == "kept" and not app.query_one("#proposal").display
    with closing(db.open_existing(week_home)) as conn:
        assert state_of(conn, p.id) == "kept"


def _three_sessions(home) -> None:
    """Two ended chats of 80 lines each, then the active one with three."""
    with closing(db.open_existing(home)) as conn:
        s = sessions.ensure(conn, NOW - timedelta(days=2))
        for n, size in enumerate((80, 80, 3)):
            for i in range(size):
                sessions.say(conn, s.id, "you", f"chat {n} line {i}", NOW)
            if n < 2:
                s = sessions.end_and_restart(conn, NOW - timedelta(days=1 - n))


async def test_scrollback_loads_older_sessions_with_dividers(week_home):
    _three_sessions(week_home)  # 166 rows: the first page leaves the oldest 66 unread
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        talk = _talk(app)
        assert len(talk) == PAGE and talk[-1] == "you: chat 2 line 2"
        assert [t for t in talk if t.startswith("──")] == [
            "── new chat, Wed 11/11 ──",
            "── new chat, Thu 11/12 ──",
        ]
        assert _head(app).text.endswith(" since Thu 11/12 ─╮")
        log = app.query_one("#log")
        assert log.scroll_y == log.max_scroll_y > 0  # the newest at the bottom
        top = app.query("#log > .row").first()
        log.scroll_to(y=0, animate=False)
        await _until(pilot, lambda: len(_talk(app)) == 166 and log.scroll_y > 0)
        talk = _talk(app)
        assert talk[0] == "── new chat, Tue 11/10 ──"
        assert talk[1:3] == ["you: chat 0 line 0", "you: chat 0 line 1"]
        assert log.scroll_y == top.virtual_region.y == 66, "the rows read before moved"
        log.scroll_to(y=0, animate=False)  # nothing older is left
        await settle(pilot, app)
        assert len(_talk(app)) == 166


async def test_scrollback_survives_a_failed_read(week_home, monkeypatch):
    """A read that fails (a locked database, say) leaves the older rows for the next scroll
    to the top, rather than switching scroll-back off for the run."""
    _three_sessions(week_home)
    real, failed = sessions.all_lines, []

    def flaky(conn, *, before_id=None, limit=PAGE):
        if before_id is not None and not failed:
            failed.append(before_id)
            raise sqlite3.OperationalError("database is locked")
        return real(conn, before_id=before_id, limit=limit)

    monkeypatch.setattr(sessions, "all_lines", flaky)
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        log = app.query_one("#log")
        log.scroll_to(y=0, animate=False)
        await _until(pilot, lambda: failed)
        await settle(pilot, app)
        assert len(_talk(app)) == PAGE
        log.scroll_end(animate=False)
        await pilot.pause()
        log.scroll_to(y=0, animate=False)
        await _until(pilot, lambda: len(_talk(app)) == 166)


async def test_scrollback_fills_a_log_the_first_page_leaves_short(week_home):
    """In a window taller than a page there is nothing to scroll, so the pages before are
    read until the log can scroll or none is left."""
    _three_sessions(week_home)
    app = _app(week_home)
    async with app.run_test(size=(120, 130)) as pilot:
        await settle(pilot, app)
        await _until(pilot, lambda: len(_talk(app)) == 166)
        assert _talk(app)[0] == "── new chat, Tue 11/10 ──"
        assert app.query_one("#log").max_scroll_y > 0


async def test_a_hidden_log_reads_one_page_until_it_shows(week_home, monkeypatch):
    """Folded to the strip the log is never laid out, so it has nothing to fill and
    reads only the newest page; once c shows it, it reads the pages before as it needs."""
    for module in (tui_app, tui_chat):
        monkeypatch.setattr(module, "PAGE", 10)
    _three_sessions(week_home)
    app = _app(week_home)
    async with app.run_test(size=NARROW) as pilot:
        await settle(pilot, app)
        for _ in range(10):  # a read the hidden log asked for would have landed by now
            await pilot.pause(0.01)
        assert len(_talk(app)) == 10
        await pilot.press("c")
        log = app.query_one("#log")
        await _until(pilot, lambda: log.max_scroll_y > 0)
        assert 10 < len(_talk(app)) < 166


async def test_header_and_the_hint_while_nothing_was_said(week_home):
    """The header says since when this chat has run; while the transcript is nothing but
    dividers, one muted line says what the chat is for, and it goes once anything is said."""
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        assert _head(app).text.endswith(" since Thu 11/12 ─╮")
        assert _talk(app) == ["── new chat, Thu 11/12 ──"]
        hint = app.query_one("#hint", Static)
        assert str(hint.content) == "press c, then ask orrery to move, add or report a session"
        assert hint.region.height > 0
        for y in range(hint.region.height):
            _assert_muted(hint, hint.render_line(y))
        with closing(db.open_existing(week_home)) as conn:
            sessions.say(conn, sessions.ensure(conn, NOW).id, "you", "hello", NOW)
        await pilot.press("r")
        await settle(pilot, app)
        assert _talk(app)[-1] == "you: hello" and not hint.display


async def test_a_long_memory_note_wraps_in_the_popup_at_80x24(week_home):
    """M's lines wrap inside the box, so a long note's end is on screen, not cut off."""
    remember_at(week_home, "keep the first hour for proofs " * 4 + "and end on a summary")
    app = _app(week_home)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(pilot, app)
        await pilot.press("M")
        await pilot.pause()
        assert isinstance(app.screen, Popup)
        assert any("end on a summary" in row for row in _screen_lines(app))


async def test_widening_closes_the_overlay_so_it_never_comes_back_without_c(week_home):
    """c's overlay ends when the window widens to the docked chat: narrowing again shows the
    strip, not an overlay nobody asked for."""
    app = _app(week_home)
    async with app.run_test(size=NARROW) as pilot:
        await settle(pilot, app)
        chat = app.query_one(ChatPanel)
        await chat_keys(pilot, app, "escape")  # out of the input: the overlay stays, for y and n
        await pilot.pause()
        assert chat.has_class("-overlay") and app.focused is app.query_one(WeekView)
        await pilot.resize_terminal(*WIDE)
        await _until(pilot, lambda: app.screen.has_class("-wide"))
        await pilot.resize_terminal(*NARROW)
        await _until(pilot, lambda: app.screen.has_class("-narrow"))
        await pilot.pause()
        assert not chat.has_class("-overlay") and chat.region.height == 1


async def test_widening_while_typing_in_the_overlay_keeps_the_keyboard(week_home):
    """Typing in c's overlay, the window widens past 110: the docked chat's input keeps the
    keyboard, so y and u go on typing rather than keeping or undoing."""
    with closing(db.open_existing(week_home)) as conn:
        keep_move(conn, TODAY, "math", "learn", TOMORROW)  # something to undo
        p = propose_move(conn, ops=[ops.Move(TODAY, "geography", "geography", TOMORROW)])
    app = _app(week_home)
    async with app.run_test(size=NARROW) as pilot:
        await settle(pilot, app)
        line = app.query_one(ChatInput)
        await chat_keys(pilot, app, "a")
        await pilot.resize_terminal(130, 40)
        await _until(pilot, lambda: app.screen.has_class("-wide"))
        await pilot.press("y", "u")
        await settle(pilot, app)
        assert line.value == "ayu" and line.has_focus
    with closing(db.open_existing(week_home)) as conn:
        assert state_of(conn, p.id) == "waiting"
        assert conn.execute("select count(*) from changes where kind = 'undo'").fetchone()[0] == 0


def _screen_lines(app: App) -> list[str]:
    """The rows of the top screen as drawn."""
    return [strip.text for strip in app.screen._compositor.render_strips()]


async def test_the_key_list_scrolls_to_its_last_line_at_80x24(week_home):
    """? lists more keys than 24 rows hold: the arrows and the page keys scroll it."""
    first, *_, last = keymap.help_text().splitlines()
    app = _app(week_home)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(pilot, app)
        await pilot.press("question_mark")
        await pilot.pause()
        assert any(first in row for row in _screen_lines(app))
        assert not any(last in row for row in _screen_lines(app)), "it fits: this proves nothing"
        await pilot.press("pagedown", "pagedown", "down")
        await pilot.pause()
        assert any(last in row for row in _screen_lines(app))
        await pilot.press("up", "pageup", "pageup")
        await pilot.pause()
        assert any(first in row for row in _screen_lines(app))
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1


async def test_keys_that_open_popups_do_nothing_below_the_minimum_size(week_home):
    """Nothing draws over the minimum-size line."""
    app = _app(week_home)
    async with app.run_test(size=(79, 40)) as pilot:
        await settle(pilot, app)
        for key in ("question_mark", "M", "h", "x", "enter"):
            await pilot.press(key)
            await settle(pilot, app)
            assert len(app.screen_stack) == 1, key
        assert any(TOO_SMALL in row for row in _screen_lines(app))


async def test_narrow_strip_says_proposal_waiting_and_y_does_nothing_until_overlay(week_home):
    """Under 110 columns the chat is one line; c opens it over the content, and only
    then do y and n act."""
    with closing(db.open_existing(week_home)) as conn:
        p = propose_move(conn)
    app = _app(week_home)
    async with app.run_test(size=NARROW) as pilot:
        await settle(pilot, app)
        chat, week = app.query_one(ChatPanel), app.query_one("#week")
        assert chat.region.height == 1 and chat.region.width == NARROW[0]
        assert _text(app, "#strip") == "proposal waiting: c shows it"
        width = week.region.width
        await pilot.press("y", "n")
        await settle(pilot, app)
        with closing(db.open_existing(week_home)) as conn:
            assert state_of(conn, p.id) == "waiting"
        await pilot.press("c")
        await pilot.pause()
        assert chat.has_class("-overlay") and app.query_one(ChatInput).has_focus
        assert app.query_one("#proposal").region.width > 0  # the diff is on screen
        assert chat.region.height > 1 and week.region.width == width  # over it, not beside
        await pilot.press("escape")  # out of the input; the overlay stays for y and n
        await pilot.pause()
        assert app.focused is app.query_one(WeekView) and chat.has_class("-overlay")
        await pilot.press("y")
        await settle(pilot, app)
        assert _text(app, "#note") == "kept"
        await pilot.press("escape")
        await pilot.pause()
        assert not chat.has_class("-overlay") and chat.region.height == 1
        assert _text(app, "#strip") == "── new chat, Thu 11/12 ──"
    with closing(db.open_existing(week_home)) as conn:
        assert state_of(conn, p.id) == "kept"


@pytest.mark.parametrize("size", [(79, 40), (120, 23)])
async def test_minimum_size_at_79x40_and_120x23(week_home, size):
    """Below 80 columns or 24 rows, one line says so, and no key but q acts on the
    frame it hides (not even q if you were typing: the test after this); at 80×24 the frame
    is back."""
    with closing(db.open_existing(week_home)) as conn:
        p = propose_move(conn)
    app = _app(week_home)
    async with app.run_test(size=size) as pilot:
        await settle(pilot, app)
        line = app.query_one("#too-small", Static)
        assert str(line.content) == TOO_SMALL and line.region.height == 1
        assert [app.query_one(s).region.width for s in ("#header", "#body", "#bottom")] == [0] * 3
        await pilot.press("y", "n", "c", "h", "i", "T", "tab")
        await settle(pilot, app)
        with closing(db.open_existing(week_home)) as conn:
            assert state_of(conn, p.id) == "waiting"
        assert app.focused is None and app.query_one(ChatInput).value == ""
        assert len(app.screen_stack) == 1 and not app.query_one("#theme-picker").display
        assert app.query_one("#tabs", ContentSwitcher).current == "week"
        await pilot.resize_terminal(80, 24)
        await _until(pilot, lambda: app.query_one("#body").region.width == 80)
        assert line.region.width == 0
        await pilot.resize_terminal(*size)
        await pilot.press("q")
        await pilot.pause()
        assert not app.is_running


@pytest.mark.parametrize(
    "tiny, back", [((120, 23), WIDE), ((79, 40), NARROW), ((120, 23), NARROW), ((79, 40), WIDE)]
)
async def test_typing_survives_a_trip_below_the_minimum_size(week_home, tiny, back):
    """Hiding the frame takes the input's keyboard. While it's hidden no key acts, q
    included, as you were typing; once it's back the input has the keyboard again, so the next
    letters type rather than undo or quit."""
    with closing(db.open_existing(week_home)) as conn:
        keep_move(conn, TODAY, "math", "learn", TODAY + timedelta(days=1))  # something to undo
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        line = app.query_one(ChatInput)
        await chat_keys(pilot, app, "a")
        await pilot.resize_terminal(*tiny)
        await _until(pilot, lambda: app.has_class("-tiny") and app.focused is None)
        await pilot.resize_terminal(70, 20)  # still too small: the keyboard stays owed
        await pilot.press("u", "q")
        await settle(pilot, app)
        assert app.is_running and line.value == "a"
        await pilot.resize_terminal(*back)
        await _until(pilot, lambda: line.has_focus)
        await pilot.press("u", "q")
        await settle(pilot, app)
        assert line.value == "auq" and line.has_focus and app.is_running
        assert line.region.width > 0, "the input with the keyboard is on screen"
    with closing(db.open_existing(week_home)) as conn:
        assert conn.execute("select count(*) from changes where kind = 'undo'").fetchone()[0] == 0


async def test_resize_mid_turn_keeps_stream(week_home, tmp_path):
    """The reply streaming in survives the chat folding to a strip and back."""
    fake = make_fake(
        tmp_path,
        {"events": [INIT_OK, delta("Let me look."), result_ok("Let me look.")], "linger": 30},
    )
    app = _app(week_home, fake)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        await chat_keys(pilot, app, *"first", "enter")
        await _until(pilot, lambda: "organizer: Let me look." in _talk(app))
        chat, line = app.query_one(ChatPanel), app.query_one(ChatInput)
        app.set_focus(None)  # out of the input, as a click elsewhere leaves it
        await pilot.resize_terminal(*NARROW)  # the screen hears of it a timer's tick later
        await _until(pilot, lambda: chat.region.height == 1)
        assert _text(app, "#strip") == "organizer: Let me look."
        await pilot.resize_terminal(*WIDE)
        await _until(pilot, lambda: app.screen.has_class("-wide"))
        assert _talk(app)[-2:] == ["you: first", "organizer: Let me look."]
        await pilot.press("c")
        await pilot.resize_terminal(*NARROW)  # typing as it folds: it opens over the content
        await _until(pilot, lambda: app.screen.has_class("-narrow") and chat.region.height > 1)
        assert chat.has_class("-overlay") and line.has_focus
        await pilot.press("u", "q")
        await pilot.pause()
        assert line.value == "uq" and app.is_running, "a letter fired the app's key"
        assert _talk(app)[-2:] == ["you: first", "organizer: Let me look."]
        assert app.turn_cancel is not None
        await pilot.press("escape")
        await _until(pilot, lambda: _organizer_done(app))
        await settle(pilot, app)
        assert _talk(app)[-1] == "orrery: turn cancelled"


async def test_M_opens_memory(week_home):
    """m is the month view's now; memory moved to M."""
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        await pilot.press("m")
        await pilot.pause()
        assert len(app.screen_stack) == 1
        await pilot.press("M")
        await pilot.pause()
        assert isinstance(app.screen, Popup)
        assert _text(app, "#popup-text") == "no notes\n\nask the organizer to forget a note"


class _Plain(App):
    """No orrery stylesheet and no Rich theme: only Textual's own tokens."""

    def __init__(self, diff) -> None:
        super().__init__()
        self.diff = diff

    def compose(self) -> ComposeResult:
        yield Static(Content("\n").join(keymap.key_rows("week", 78)), id="footer")
        yield Static(render_diff(self.diff), id="diff")
        yield Static(chat_bar(keymap.footer("proposal"), 30), id="bar")


def _segments(widget: Static, row: int, text: str):
    """The segments of the widget's rendered row that hold text."""
    return [s for s in widget.render_line(row) if s.text.strip() and s.text.strip() in text]


def _rgb(app: App, token: str):
    return Color.parse(app.get_css_variables()[token]).rgb


def _assert_muted(widget, row) -> None:
    """Every printed segment of row is $foreground-muted laid over the widget's background:
    dimmer than $foreground, never the full-contrast white or black of a span's `auto`."""
    over = Color.parse(widget.app.get_css_variables()["foreground-muted"])
    muted = (widget.background_colors[1] + over).rgb
    assert muted not in {_rgb(widget.app, "foreground"), (0, 0, 0), (255, 255, 255)}
    assert {s.style.color.triplet for s in row if s.text.strip()} == {muted}, row.text


def _line_with(widget, text: str):
    """The widget's rendered row holding text."""
    return next(r for r in map(widget.render_line, range(widget.size.height)) if text in r.text)


@pytest.mark.parametrize("theme", ["gruvbox", "catppuccin-latte"])
async def test_muted_rows_are_dimmer_than_the_foreground(week_home, theme):
    """The diff's − and context lines, the organizer's rows and the dividers are muted in the
    real app, dark and light, never the brightest text on screen."""
    with closing(db.open_existing(week_home)) as conn:
        propose_move(conn, rationale="a reason")
        sessions.say(conn, sessions.ensure(conn, NOW).id, "orrery", "a review line", NOW)
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        app.theme = theme
        await settle(pilot, app)
        diff = app.query_one("#proposal", Static)
        _assert_muted(diff, _line_with(diff, "a reason"))
        _assert_muted(diff, _line_with(diff, "− Thu 11/12"))
        rows = list(app.query("#log > .row"))
        divider, said = (next(w for w in rows if t in str(w.content)) for t in ("──", "review"))
        _assert_muted(divider, divider.render_line(0))
        _assert_muted(said, said.render_line(0))


async def test_footer_confirm_and_diff_render_without_a_rich_theme_under_textual_light(week_db):
    p = propose_move(week_db, rationale="a reason")
    app = _Plain(proposals.diff(week_db, p, TODAY))
    async with app.run_test(size=(80, 20)) as pilot:
        app.theme = "textual-light"
        await pilot.pause()
        accent, panel = _rgb(app, "accent"), _rgb(app, "panel")
        footer = app.query_one("#footer", Static)
        assert footer.render_line(0).text.startswith(keymap.key_rows("week", 78)[0].plain)
        (chat,) = _segments(footer, 0, "c")  # a write key's glyph: the accent, tinted for text
        assert chat.style.color.triplet == _rgb(app, "text-accent") and chat.style.bold
        diff = app.query_one("#diff", Static)
        rows = diff.render_line(1), diff.render_line(2), diff.render_line(3)
        assert [r.text[0] for r in rows] == [" ", "−", "+"]  # under the header, the reason
        _assert_muted(diff, rows[0])
        _assert_muted(diff, rows[1])
        assert all(s.style.strike for s in rows[1] if s.text.strip() not in ("", "−"))
        assert {s.style.color.triplet for s in rows[2] if s.text.strip()} == {accent}
        bar = app.query_one("#bar", Static).render_line(0)
        assert {s.style.bgcolor.triplet for s in bar} == {panel}
        app.push_screen(Confirm("start over?"))
        await pilot.pause()
        text = app.screen.query_one(Static)
        (y,) = _segments(text, 2, "y")
        assert text.render_line(2).text.startswith(keymap.footer("confirm").plain)
        assert y.style.color.triplet == accent
