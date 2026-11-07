"""The Week view, through the Pilot: the timetable (days across, parts down, today a chip),
its cursor and time travel, and the panels under it: so far this week, the last 5 weeks and
the cursor's day."""

import re
import threading
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from helpers import (
    NOW,
    TODAY,
    add_agenda,
    keep_move,
    propose_move,
    report_and_collect,
    settle,
)
from test_frame import _assert_muted, _line_with, _rgb
from test_tui import _column, _counting_reloads, _rows, _until
from textual.color import Color
from textual.geometry import Region

from orrery import channels, db, ledger, ops, proposals, schedule, stats
from orrery.tui import week
from orrery.tui.app import Loaded, OrreryApp
from orrery.tui.chat import ChatPanel
from orrery.tui.cursor import hue
from orrery.tui.day import DayPanel, DayText
from orrery.tui.week import LastWeeks, SoFar, WeekPane, WeekView

WIDE = (120, 40)
BIG = (235, 60)  # the size orrery is used at
THU = TODAY
MON, TUE, WED, FRI, SAT, SUN = (THU + timedelta(days=n) for n in (-3, -2, -1, 1, 2, 3))
NEXT_THU = datetime(2026, 11, 19, 9, 0)  # a week on: the week of 11/9 is a past week
MATH_DARK, FINANCE_DARK, GEOGRAPHY_DARK = "#3987e5", "#d95926", "#199e70"
MATH_LIGHT = "#2a78d6"
BEFORE = "before orrery started"
DOTS = "weeks before orrery started show ·"
THEMES = ["gruvbox", "flexoki", "catppuccin-latte", "textual-light", "ansi-dark", "ansi-light"]


def _app(home, now: datetime = NOW) -> OrreryApp:
    return OrreryApp(root=lambda: home, now=lambda: now)


def _view(app) -> WeekView:
    return app.query_one(WeekView)


def _x(app, i: int) -> int:
    """Where day i's column starts."""
    return week.GUTTER + i * _view(app).col


def _find(app, text: str, i: int | None = None) -> tuple[int, int]:
    """(x, y) of text's first cell, in day i's column if given."""
    for y, row in enumerate(_rows(app)):
        start = 0 if i is None else _x(app, i)
        end = len(row) if i is None else start + _view(app).col
        if (at := row.find(text, start, end)) >= 0:
            return at, y
    raise AssertionError(f"{text!r} not drawn")


def _style(app, x: int, y: int):
    """The style of the cell at x, y of the timetable."""
    at = 0
    for seg in _view(app).render_line(y):
        if at <= x < at + seg.cell_length:
            return seg.style
        at += seg.cell_length
    raise AssertionError(f"nothing at {x}, {y}")


def _fg(app, text: str, i: int | None = None):
    return _style(app, *_find(app, text, i)).color.triplet


def _bg(app, text: str, i: int | None = None):
    return _style(app, *_find(app, text, i)).bgcolor.triplet


def _hex(h: str):
    return Color.parse(h).rgb


def _cells(view, y: int) -> list:
    """The style of each cell on row y of a view."""
    return [seg.style for seg in view.render_line(y) for _ in range(seg.cell_length)]


def _on(app, triplet) -> set[tuple[int, int]]:
    """(x, y) of every cell of the timetable on the background triplet."""
    view = _view(app)
    return {
        (x, y)
        for y in range(view.size.height)
        for x, s in enumerate(_cells(view, y))
        if s.bgcolor is not None and s.bgcolor.triplet == triplet
    }


def _block(app, i: int) -> set[tuple[int, int]]:
    """(x, y) of every cell of the focused block, in day i's column."""
    view = _view(app)
    return {(x, y) for y in view.item_rows() for x in range(_x(app, i), _x(app, i) + view.col)}


def _shown(style):
    """A cell's text and fill colours as the terminal shows them: swapped under reverse."""
    return (style.bgcolor, style.color) if style.reverse else (style.color, style.bgcolor)


def _look(style):
    return _shown(style), style.bold, style.underline


def _ink(color):
    """A colour as the terminal draws it: its RGB, or an ANSI colour's number, alpha dropped."""
    return color.number if color.triplet is None else color.triplet


def _contrast(a, b) -> float:
    """WCAG's contrast ratio of two RGB triplets, 1 to 21."""

    def lum(rgb) -> float:
        c = [v / 255 for v in rgb]
        r, g, b = (v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in c)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _assert_chip(style, plain) -> None:
    """style is today's chip: bold, filled apart from the view's own background (plain, a
    Color), its text apart from its fill: at least 4:1 where the theme's colours are RGB, a
    different colour in an ANSI theme, whose colours are the terminal's. 4, not WCAG's 4.5:
    solarized's violet takes white (Textual's contrast pick) at 4.38; every other theme 5.8 up."""
    text, fill = _shown(style)
    assert style.bold and fill != plain and text != fill, style
    if fill.triplet is not None:
        assert _contrast(text.triplet, fill.triplet) >= 4, style


def _muted(app):
    """$foreground-muted over the view's background, as a span's alpha colour draws."""
    over = Color.parse(app.get_css_variables()["foreground-muted"])
    return (_view(app).background_colors[1] + over).rgb


def _lines(widget) -> list[str]:
    """A panel's rows as drawn, inside its border."""
    return [widget.render_line(y).text for y in range(widget.size.height)]


def _find_in(widget, text: str) -> tuple[int, int]:
    """(x, y) of text's first cell in a panel."""
    for y, row in enumerate(_lines(widget)):
        if (x := row.find(text)) >= 0:
            return x, y
    raise AssertionError(f"{text!r} not drawn")


def _style_in(widget, x: int, y: int):
    """The style of the cell at x, y of a panel."""
    at = 0
    for seg in widget.render_line(y):
        if at <= x < at + seg.cell_length:
            return seg.style
        at += seg.cell_length
    raise AssertionError(f"nothing at {x}, {y}")


def _day(app) -> list[str]:
    """The day panel's rows, stripped on the right, blank ones dropped."""
    return [row.rstrip() for row in _lines(app.query_one(DayText)) if row.strip()]


def _widest(view) -> int:
    """The widest line the view draws, before render_line crops it to the panel."""
    return max(line.cell_length for line in view.render().split("\n"))


def _top(widget) -> str:
    """A panel's top border as drawn: its title and caption."""
    return widget.render_lines(Region(0, 0, *widget.outer_size))[0].text


async def test_week_view_renders_layout_A(week_home):
    with closing(db.open_existing(week_home)) as conn:
        report_and_collect(conn, MON, "math", "learn", 1)
        report_and_collect(conn, MON, "math", "learn", 1, status="partial")
        add_agenda(conn, "math", [channels.AgendaEntry(FRI, "zorbs 3", "learn")])
        add_agenda(conn, "geography", [channels.AgendaEntry(MON, "recap", "geography")])
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        rows = _rows(app)
        assert [_column(app, i)[0] for i in range(7)] == [
            "Mon 9", "Tue 10", "Wed 11", "Thu 12", "Fri 13", "Sat 14", "Sun 15"
        ]  # fmt: skip
        # parts down, in order, each named once in the gutter
        labels = [row[: week.GUTTER].strip() for row in rows]
        assert [x for x in labels if x] == ["warm", "main", "after"]
        mon = [x for x in _column(app, 0) if x]
        assert mon == [
            "Mon 9", "▌math", "▌rep ○", "▌math", "▌learn ●◐", "▌geography", "▌○ recap"
        ]  # fmt: skip  # kind equal to subject: pips and topic share a line
        assert [x for x in _column(app, 4) if x][1:4] == ["▌math", "▌learn ○○", "▌zorbs 3"]
        assert [x for x in _column(app, 6) if x] == ["Sun 15", "rest"]
        assert _widest(_view(app)) <= _view(app).size.width  # never cropped by the panel
        # the edge in the subject's hue, the name in the text colour, the topic muted
        assert _fg(app, "▌", 0) == _hex(MATH_DARK)
        assert _fg(app, "●◐", 0) == _hex(MATH_DARK)
        assert _fg(app, "▌geography", 1) == _hex(GEOGRAPHY_DARK)
        assert _fg(app, "math", 0) == _rgb(app, "foreground")
        assert _fg(app, "zorbs 3", 4) == _muted(app)
        # today a chip, its column unlifted; the cursor on today's first block, and only there
        plain, cursor = _style(app, *_find(app, "Mon 9", 0)).bgcolor, _rgb(app, "accent-muted")
        _assert_chip(_style(app, *_find(app, "Thu 12", 3)), plain)
        assert _shown(_style(app, *_find(app, "Thu 12", 3)))[1].triplet == _rgb(app, "accent")
        assert _bg(app, "▌math", 3) == cursor and _on(app, cursor) == _block(app, 3)
        assert _bg(app, "▌geography", 3) == _bg(app, "▌geography", 2) == plain.triplet
        await pilot.resize_terminal(80, 24)  # the narrowest frame: still a timetable, unwrapped
        await _until(pilot, lambda: _view(app).size.width < 83)
        assert _widest(_view(app)) <= _view(app).size.width
        assert "▌learn ●◐" in _column(app, 0)


async def test_moved_sessions_show_struck_with_where_they_went(week_home):
    with closing(db.open_existing(week_home)) as conn:
        keep_move(conn, THU, "math", "learn", FRI)  # all of Thursday's
        keep_move(conn, SAT, "math", "fun", SUN)
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        thu = [x for x in _column(app, 3) if x]
        assert thu[1:4] == ["▌math", "▌learn ○○", "▌→ Fri 13"]
        name = _style(app, *_find(app, "math", 3))
        pips = _style(app, *_find(app, "○○", 3))
        assert name.strike and pips.strike and pips.color.triplet != _hex(MATH_DARK)
        fri = [x for x in _column(app, 4) if x]
        assert fri[1:4] == ["▌math", "▌learn", "▌○○○○ +2"]  # where they went: too many to share
        assert _fg(app, "+2", 4) == _muted(app)  # the sessions added past the week's baseline
        assert [x for x in _column(app, 5) if x][1:4] == ["▌math", "▌fun ○", "▌→ Sun 15"]
        assert _fg(app, "→ Sun 15", 5) == _muted(app)
        assert [x for x in _column(app, 6) if x][1:3] == ["▌math", "▌fun ○ +1"]
        await pilot.press("right")
        await settle(pilot, app)
        assert any("▌math learn ○○○○ +2" in row for row in _day(app))  # the day panel's


async def test_deadlines_show_under_their_day(week_home):
    with closing(db.open_existing(week_home)) as conn:
        add_agenda(conn, "math", [channels.AgendaEntry(FRI, "exam", deadline=True)])
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        x, y = _find(app, "▌exam", 4)
        assert _rows(app)[y][: week.GUTTER].strip() == "due"
        assert _fg(app, "▌exam", 4) == _hex(MATH_DARK)
        assert y > _find(app, "▌geography", 4)[1]  # under the day's blocks


async def test_extras_show_as_plus_n_extra(week_home):
    with closing(db.open_existing(week_home)) as conn:
        report_and_collect(conn, MON, "finance", "rep", 1)  # unplanned
        report_and_collect(conn, MON, "math", "learn", 3)  # one past the two planned
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        mon = [x for x in _column(app, 0) if x]
        assert "▌learn ●●" in mon
        assert mon[-6:] == ["▌math", "▌learn", "▌+1 extra", "▌finance", "▌rep", "▌+1 extra"]
        x, y = _find(app, "▌finance", 0)
        assert _rows(app)[y - 3][: week.GUTTER].strip() == "extra"


async def test_arrows_move_cursor_and_cross_week_edges(week_home):
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        view = _view(app)
        assert app.focused is view and app.view == "week"
        assert app.cursor_date == THU == view.focus_date and view.item == 0
        await pilot.press("down", "down")  # Thursday's math, then its geography: no further
        await pilot.pause()
        assert view.item == 1
        await pilot.press("up")
        await pilot.pause()
        assert view.item == 0
        await pilot.press("right", "right", "right", "right")  # past Sunday: next Monday
        await settle(pilot, app)
        assert app.cursor_date == date(2026, 11, 16) == view.focus_date
        assert str(app.query_one("#controls").content) == "‹ Nov 16 – 22, week 47 ›"
        assert _column(app, 0)[0] == "Mon 16"
        await pilot.press("left")  # before Monday: last week's Sunday
        await settle(pilot, app)
        assert app.cursor_date == SUN
        assert str(app.query_one("#controls").content) == "‹ Nov 9 – 15, week 46 ›"


async def test_arrow_keys_fire_once(week_home):
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        await pilot.press("left")
        await settle(pilot, app)
        assert app.cursor_date == WED
        await pilot.press("down")  # Wednesday has three blocks: one press, one step
        await pilot.pause()
        assert _view(app).item == 1
        await pilot.press("right")
        await settle(pilot, app)
        assert app.cursor_date == THU


async def test_brackets_jump_a_week_and_t_returns_today(week_home):
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        await pilot.press("right_square_bracket")
        await settle(pilot, app)
        assert app.cursor_date == date(2026, 11, 19) and app.board.monday == date(2026, 11, 16)
        await pilot.press("left_square_bracket", "left_square_bracket")
        await settle(pilot, app)
        assert app.cursor_date == date(2026, 11, 5)
        await pilot.press("t")
        await settle(pilot, app)
        assert app.cursor_date == THU and app.board.monday == MON


async def test_reload_keeps_cursor(week_home):
    """r rereads the data and leaves the cursor where it was."""
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        await pilot.press("right_square_bracket", "right")
        await settle(pilot, app)
        await pilot.press("r")
        await settle(pilot, app)
        assert app.cursor_date == date(2026, 11, 20) == _view(app).focus_date
        assert app.board.monday == date(2026, 11, 16)


@pytest.mark.parametrize(
    "keys", [["right"] * 4 + ["left"], ["right_square_bracket", "left_square_bracket"]]
)
async def test_back_before_the_read_lands_never_draws_the_week_left(week_home, monkeypatch, keys):
    """Out of the week and back while next week's read is still in flight (a held arrow
    repeats every ~30 ms): that read is dropped, and only the cursor's week is drawn."""
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        entered, release = threading.Event(), threading.Event()
        real_rates, real_loaded, drawn = stats.week_rates, app._loaded, []

        def slow_rates(*args):
            entered.set()  # the cursor's week is read by now
            release.wait(5)
            return real_rates(*args)

        def spy(got):
            drawn.append(got.board.monday)
            real_loaded(got)

        app._loaded = spy
        monkeypatch.setattr(stats, "week_rates", slow_rates)
        try:
            await pilot.press(*keys[:-1])
            await _until(pilot, entered.is_set)  # next week's read is in flight
            await pilot.press(keys[-1])
        finally:
            release.set()
        await settle(pilot, app)
        assert drawn == [MON]
        assert app.board.monday == schedule.week_start(app.cursor_date) == MON
        assert str(app.query_one("#controls").content) == "‹ Nov 9 – 15, week 46 ›"


async def test_a_read_the_cursor_left_reads_its_week_again(week_home):
    """A read that passed its cancel check just before the cursor left its week still
    lands; the cursor's week is read straight after, so the cursor never stays off screen."""
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        await pilot.press("right_square_bracket")
        await settle(pilot, app)
        stale = Loaded(app.board, app.day, app.boundary, cursor=app.cursor_date)  # next week's
        await pilot.press("left_square_bracket")
        await settle(pilot, app)
        app._loaded(stale)
        await settle(pilot, app)
        assert app.board.monday == schedule.week_start(app.cursor_date) == MON
        assert str(app.query_one("#controls").content) == "‹ Nov 9 – 15, week 46 ›"


async def test_day_change_moves_only_today(week_home):
    """Past the day boundary the chip moves to the new today; the cursor stays, yesterday's
    head now underlined as any focused day's other than today."""
    clock = [datetime(2026, 11, 12, 23, 0)]
    app = OrreryApp(root=lambda: week_home, now=lambda: clock[0])
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        clock[0] = datetime(2026, 11, 13, 5, 0)
        app._check_day()
        await settle(pilot, app)
        assert app.day == FRI and app.cursor_date == THU == _view(app).focus_date
        plain = _style(app, *_find(app, "Mon 9", 0)).bgcolor
        _assert_chip(_style(app, *_find(app, "Fri 13", 4)), plain)
        thu = _style(app, *_find(app, "Thu 12", 3))
        assert thu.underline and thu.bgcolor == plain  # unfilled: not the chip
        assert _on(app, _rgb(app, "accent-muted")) == _block(app, 3)
        assert _bg(app, "▌geography", 4) == plain.triplet


async def test_before_starts_and_far_future_render_without_error(week_home):
    """A week before starts draws its days empty under their heads, with one muted note
    saying why, and nothing for the cursor to move to; one years away draws the template."""
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        await pilot.press(*["left_square_bracket"] * 3)
        await settle(pilot, app)
        assert app.board.monday == date(2026, 10, 19) and _column(app, 0)[0] == "Mon 19"
        drawn = [row.strip() for row in _rows(app) if row.strip()]
        assert drawn[1:] == [BEFORE], "no template blocks, pips or rest days before starts"
        assert _fg(app, BEFORE) == _muted(app)
        await pilot.press("down")
        await pilot.pause()
        assert _view(app).item == 0
        app.cursor_date = date(2031, 6, 4)
        app.app_reload()
        await settle(pilot, app)
        assert app.board.monday == date(2031, 6, 2) and _column(app, 2)[0] == "Wed 4"
        assert _view(app).focus_date == date(2031, 6, 4)
        assert app.query_one(SoFar).week.monday == MON  # still today's week
        assert app.is_running


async def test_the_week_orrery_starts_mid_week(week_home):
    """The days before starts are empty, with one muted note; so far this week counts the days
    from starts only, as its rates do, and the top panel says when orrery started."""
    with closing(db.open_existing(week_home)) as conn:
        with ledger.change(conn, actor="orrery", kind="setup", now=NOW):
            conn.execute("update settings set value = ? where key = 'starts'", (str(WED),))
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        rows = _rows(app)
        (y,) = [y for y, row in enumerate(rows) if BEFORE in row]
        assert _fg(app, BEFORE) == _muted(app)
        for i, head in ((0, "Mon 9"), (1, "Tue 10")):
            assert [x for j, x in enumerate(_column(app, i)) if x and j != y] == [head]
        assert [x for x in _column(app, 2) if x][1:3] == ["▌math", "▌rep ○"]
        so_far = app.query_one(SoFar)
        assert _top(so_far).endswith(" 0 of 6 planned, Wed to Thu ─╮")
        assert _top(_view(app)).endswith(" Wed to Sun; orrery started Wed 11 ─╮")
        rows = _lines(so_far)
        math, finance, geography = (next(x for x in rows if x.startswith(s)) for s in (
            "▌math", "▌finance", "▌geography"))  # fmt: skip
        assert "7 in the week" in math and "1 in the week" in finance
        assert "3 in the week" in geography


async def test_a_reload_with_fewer_blocks_puts_the_cursor_on_the_last(week_home):
    """The focused day losing blocks to a reload (an undo here) leaves the cursor on one
    still drawn, not on nothing."""
    with closing(db.open_existing(week_home)) as conn:
        p = propose_move(conn, ops=[ops.Add(THU, "main", "finance", "rep", 1)])
        proposals.keep(conn, p.id, now=NOW, today=TODAY)
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        view, cursor = _view(app), _rgb(app, "accent-muted")
        await pilot.press("down", "down")
        await pilot.pause()
        assert view.item == 2 and _bg(app, "▌geography", 3) == cursor
        await pilot.press("u")
        await settle(pilot, app)
        assert "▌finance" not in _column(app, 3)
        assert view.item == 1 and _bg(app, "▌geography", 3) == cursor


async def test_a_partly_moved_block_shows_both_pips_and_where_one_went(week_home):
    with closing(db.open_existing(week_home)) as conn:
        keep_move(conn, FRI, "math", "learn", SAT, sessions=1)  # one of Friday's two
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        assert [x for x in _column(app, 4) if x][1:4] == ["▌math", "▌learn ○○", "▌→ Sat 14"]
        x, y = _find(app, "○○", 4)
        left, moved = _style(app, x, y), _style(app, x + 1, y)
        assert left.color.triplet == _hex(MATH_DARK) and not left.strike
        assert moved.strike and moved.color.triplet == _muted(app)
        assert not _style(app, *_find(app, "math", 4)).strike


def test_hue_folds_from_the_seventh_slot_and_for_an_unknown_subject():
    slots = {"math": 0, "seventh": 6}
    assert hue(slots, "math", True) == MATH_DARK and hue(slots, "math", False) == MATH_LIGHT
    assert hue(slots, "seventh", True) == hue(slots, "seventh", False) == "$foreground-muted"
    assert hue(slots, "unknown", True) == "$foreground-muted"


async def test_a_seventh_subject_and_an_unknown_one_are_drawn_muted(week_home):
    with closing(db.open_existing(week_home)) as conn:
        with ledger.change(conn, actor="orrery", kind="setup", now=NOW):
            for i, name in enumerate(("art", "biology", "chemistry", "drawing"), 3):
                conn.execute(
                    "insert into subjects (name, label, ord, active) values (?, ?, ?, 1)",
                    (name, name.title(), i),
                )
        report_and_collect(conn, MON, "drawing", "rep", 1)  # an extra, in the seventh slot
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        assert _fg(app, "▌drawing", 0) == _muted(app)
        assert _fg(app, "drawing", 0) == _rgb(app, "foreground")  # the name keeps its token
        assert _fg(app, "▌math", 0) == _hex(MATH_DARK)
        _view(app).show(app.board, {})  # no slot for any subject
        await pilot.pause()
        assert _fg(app, "▌math", 0) == _muted(app)


@pytest.mark.parametrize("theme", ["gruvbox", "catppuccin-latte"])
async def test_lower_panels_muted_text_is_dimmer_than_the_foreground(week_home, theme):
    """So far's week totals, and the last 5 weeks' numbers and note, in $foreground-muted,
    dark and light, never the full-contrast white or black of a span's auto colour."""
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        app.theme = theme
        await settle(pilot, app)
        so_far, last = app.query_one(SoFar), app.query_one(LastWeeks)
        _assert_muted(last, _line_with(last, "W46"))
        _assert_muted(last, _line_with(last, "weeks before orrery started show ·"))
        over = Color.parse(app.get_css_variables()["foreground-muted"])
        (count,) = [s for s in _line_with(so_far, "in the week") if "in the week" in s.text]
        assert count.style.color.triplet == (so_far.background_colors[1] + over).rgb


async def test_last_5_weeks_percent_and_dot_before_starts(week_home):
    """So far this week: per subject a bar, N of M and the week's total, the sessions done of
    those planned to date in its caption. The last 5 weeks: each week's share done to date,
    and · for the weeks before orrery started."""
    with closing(db.open_existing(week_home)) as conn:
        report_and_collect(conn, MON, "math", "learn", 2, note="lorem lemmas, two examples")
        for subject, day, kind, n, note in (
            ("math", date(2026, 11, 16), "learn", 1, ""),
            ("finance", date(2026, 11, 17), "learn", 2, "what a quib is"),
        ):
            channels.report(
                conn, channel=subject, date=day, kind=kind, sessions=n, status="done",
                note=note, key=f"{subject}-{day}", now=NEXT_THU, boundary_hour=5,
            )  # fmt: skip  # staged: the app collects them a week on
    app = _app(week_home, NEXT_THU)
    async with app.run_test(size=BIG) as pilot:
        await settle(pilot, app)
        so_far, last = app.query_one(SoFar), app.query_one(LastWeeks)
        assert _top(so_far).startswith("╭─ So far this week ──")
        assert _top(so_far).endswith(" 3 of 13 planned, Mon to Thu ─╮")
        rows = [" ".join(x.split()) for x in _lines(so_far) if x.strip()]
        assert [x.split()[0] for x in rows[:3]] == ["▌math", "▌finance", "▌geography"]
        assert rows[3].startswith("questions waiting: math ") and len(rows) == 4
        assert rows[0].endswith("1 of 6 10 in the week")
        assert rows[1].endswith("2 of 3 3 in the week")
        assert rows[2].endswith("0 of 4 5 in the week")
        bar = next(s for s in so_far.content.spans if so_far.content.plain[s.start] == "█")
        assert str(bar.style) == MATH_DARK
        assert _top(last).startswith("╭─ Last 5 weeks ──")
        assert _top(last).endswith(" planned sessions done ─╮")
        rows = [" ".join(x.split()) for x in _lines(last) if x.strip()]
        assert rows == [
            "W43 W44 W45 W46 W47",
            "▌math · · · 20% 17%",
            "▌finance · · · 0% 67%",
            "▌geography · · · 0% 0%",
            "weeks before orrery started show ·",
        ]
        x, y = _find_in(last, "▌finance")
        assert _style_in(last, x, y).color.triplet == _hex(FINANCE_DARK)


async def test_so_far_leaves_out_a_subject_with_nothing_this_week(week_home):
    """A subject planned in the last 5 weeks but not this one (finance, dropped from the
    template after the week of Nov 9) has no row in so far this week, which is about this
    week; the last 5 weeks keep it."""
    with closing(db.open_existing(week_home)) as conn:
        with ledger.change(conn, actor="orrery", kind="setup", now=NOW):
            schedule.pin(conn, MON, NOW)  # finance's blocks stay in the week of Nov 9
            kept = schedule.read_template(conn).items()
            schedule.write_template(
                conn, {d: [b for b in bs if b.subject != "finance"] for d, bs in kept}
            )
    app = _app(week_home, NEXT_THU)
    async with app.run_test(size=BIG) as pilot:
        await settle(pilot, app)
        so_far = [" ".join(x.split()) for x in _lines(app.query_one(SoFar)) if x.strip()]
        last = [" ".join(x.split()) for x in _lines(app.query_one(LastWeeks)) if x.strip()]
        assert [x.split()[0] for x in so_far[:2]] == ["▌math", "▌geography"], so_far
        assert not any("finance" in x for x in so_far), so_far
        assert "▌finance · · · 0% ·" in last, last


async def test_so_far_caption_with_two_digit_counts_fits_at_160_columns(week_home):
    """The caption is the Week tab's only all-subjects total: at the narrowest wide layout
    it still fits beside the title."""
    with closing(db.open_existing(week_home)) as conn:
        for d, subject, kind, n in (
            (MON, "math", "rep", 1), (MON, "math", "learn", 2), (MON, "geography", "geography", 1),
            (TUE, "finance", "learn", 2), (TUE, "geography", "geography", 1),
            (WED, "math", "rep", 1), (WED, "finance", "rep", 1), (WED, "geography", "geography", 1),
        ):  # fmt: skip
            report_and_collect(conn, d, subject, kind, n)
    app = _app(week_home)
    async with app.run_test(size=(160, 45)) as pilot:
        await settle(pilot, app)
        assert _top(app.query_one(SoFar)).endswith(" 10 of 13 planned, Mon to Thu ─╮")
        assert app.query_one(DayText).per == 2, "the day's ribbon keeps its half-hours"


async def test_so_far_ends_with_the_questions_waiting(week_home):
    """So far this week's last line, muted: each subject with open questions and how many,
    in slot order; with none open, no line."""
    with closing(db.open_existing(week_home)) as conn:
        channels.collect(conn, now=NOW, today=TODAY)  # its questions on the days unreported
        for (qid,) in conn.execute(
            "select id from messages where kind = 'question' and state = 'open'"
        ).fetchall():
            channels.answer(conn, question_id=qid, text="dolor", author="mentor", now=NOW)
    app = _app(week_home)
    async with app.run_test(size=BIG) as pilot:
        await settle(pilot, app)
        so_far = app.query_one(SoFar)
        assert not any("questions" in row for row in _lines(so_far))
        with closing(db.open_existing(week_home)) as conn:
            for subject in ("finance", "math", "finance"):
                channels.ask(conn, channel=subject, text="lorem?", author="orrery", now=NOW)
        app.app_reload()
        await settle(pilot, app)
        rows = [row.rstrip() for row in _lines(so_far)]
        assert rows[-1] == "questions waiting: math 1 · finance 2"
        assert rows[-2].startswith("▌geography")
        _assert_muted(so_far, _line_with(so_far, "questions waiting"))


async def test_lower_panels_fit_a_110_column_window(week_home):
    """The docked chat leaves the panels 73 columns at 110: their bars and week columns
    shrink so each row ends whole, never with an ellipsis."""
    app = _app(week_home, datetime(2026, 12, 10, 9, 0))  # five weeks since starts
    async with app.run_test(size=(110, 40)) as pilot:
        await settle(pilot, app)
        so_far = [x.rstrip() for x in _lines(app.query_one(SoFar)) if x.strip()]
        last = [x.rstrip() for x in _lines(app.query_one(LastWeeks)) if x.strip()]
        subjects, asked = so_far[:3], " ".join(so_far[3:])
        assert [x.split()[0] for x in subjects] == ["▌math", "▌finance", "▌geography"]
        assert all(x.endswith("in the week") for x in subjects), so_far
        assert re.fullmatch(r"questions waiting: math \d+ · finance \d+ · geography \d+", asked)
        assert all(re.search(r"(\d+%|·)$", x) for x in last[1:]), last
        assert not any("…" in x for x in so_far + last)


async def test_inactive_subject_in_a_past_week_keeps_its_colour(week_home):
    with closing(db.open_existing(week_home)) as conn:
        with ledger.change(conn, actor="orrery", kind="setup", now=NOW):
            conn.execute("update subjects set active = 0 where name = 'finance'")
    app = _app(week_home, NEXT_THU)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        await pilot.press("left_square_bracket")
        await settle(pilot, app)
        assert app.board.monday == MON
        assert _fg(app, "▌finance", 1) == _hex(FINANCE_DARK)
        assert _fg(app, "▌geography", 1) == _hex(GEOGRAPHY_DARK)  # its slot, not finance's


async def test_theme_change_repaints_subject_hexes(week_home):
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        assert app.theme == "gruvbox" and _fg(app, "▌", 0) == _hex(MATH_DARK)
        bar = app.query_one(SoFar)
        app.theme = "catppuccin-latte"
        await pilot.pause()
        assert _fg(app, "▌", 0) == _hex(MATH_LIGHT)
        assert _fg(app, "math", 0) == _rgb(app, "foreground")
        assert MATH_LIGHT in {str(s.style) for s in bar.content.spans}


async def test_click_focuses_a_day(week_home):
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        app.set_focus(None)
        x, y = _find(app, "▌geography", 1)  # Tuesday's second block
        await pilot.click(WeekView, offset=(x + 2, y + 1))  # past the panel's border
        await settle(pilot, app)
        assert app.cursor_date == TUE and _view(app).item == 1
        assert app.focused is _view(app)


@pytest.mark.parametrize("name", ["push_theme", "grid.STYLES", "STYLES"])
def test_no_style_names_left(name):
    """Every colour is a theme token or a subject hue: no Rich style names, nor their theme."""
    root = Path(__file__).parent.parent / "orrery"
    assert [p.name for p in root.rglob("*.py") if name in p.read_text()] == []


async def test_moving_the_cursor_scrolls_it_into_view(week_home):
    """At 80×24 the timetable is taller than its pane: ↓ to a block below the fold scrolls it
    into view, and ↑ back to the first scrolls the pane home, its day row in view."""
    with closing(db.open_existing(week_home)) as conn:
        for subject, kind in (
            ("finance", "rep"), ("finance", "learn"), ("finance", "fun"),
            ("math", "rep"), ("math", "fun"),
        ):  # fmt: skip
            report_and_collect(conn, THU, subject, kind, 1)  # off the plan: five extras
    app = _app(week_home)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(pilot, app)
        view, pane = _view(app), app.query_one(WeekPane)
        last = max(y for y, x in enumerate(_column(app, 3)) if x == "▌+1 extra")
        first = _find(app, "▌math", 3)[1]

        def shown(y: int, v=view) -> bool:
            return pane.region.y <= v.region.y + y < pane.region.bottom

        assert shown(first) and not shown(last)
        await pilot.press(*["down"] * 6)
        await _until(pilot, lambda: shown(last))
        assert view.item == 6 and not shown(first)
        await pilot.press(*["up"] * 6)
        head = _find(app, "Thu 12", 3)[1]
        await _until(pilot, lambda: shown(head))
        assert view.item == 0 and pane.scroll_y == 0


async def test_page_keys_scroll_the_stacked_pane_to_the_day_panel(week_home):
    """Below 160 columns the day panel stacks last, below the fold: pgdn scrolls the pane to
    it, the cursor kept, and pgup back."""
    app = _app(week_home)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot, app)
        pane, day = app.query_one(WeekPane), app.query_one(DayPanel)
        assert day.region.bottom > pane.region.bottom, "it all fits: this proves nothing"
        await pilot.press("pagedown", "pagedown")
        await _until(pilot, lambda: day.region.bottom <= pane.region.bottom)
        assert app.cursor_date == THU and app.focused is _view(app)
        await pilot.press("pageup", "pageup")
        await _until(pilot, lambda: pane.scroll_y == 0)


async def test_focus_back_from_the_chat_leaves_the_pane_where_it_was(week_home):
    """The pane's scroll is the views' own (a block kept in sight, a switch from the top):
    focus coming back to a view taller than its pane never scrolls it, so the focused block
    stays in view."""
    with closing(db.open_existing(week_home)) as conn:
        for subject, kind in (
            ("finance", "rep"), ("finance", "learn"), ("finance", "fun"),
            ("math", "rep"), ("math", "fun"),
        ):  # fmt: skip
            report_and_collect(conn, THU, subject, kind, 1)  # off the plan: five extras
    app = _app(week_home)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(pilot, app)
        pane = app.query_one(WeekPane)
        await pilot.press(*["down"] * 6)
        await _until(pilot, lambda: pane.scroll_y > 0)
        await settle(pilot, app)
        at = pane.scroll_y
        await pilot.press("c")
        await settle(pilot, app)
        await pilot.press("escape")
        await settle(pilot, app)
        await pilot.wait_for_scheduled_animations()  # past any focus scroll's
        assert isinstance(app.focused, WeekView) and pane.scroll_y == at


async def test_left_right_keep_the_focused_block_in_view(week_home):
    """At 80×24 with main parts taller than the pane: → to a day whose block of the same
    index sits lower brings it into view, and so does → onto a day whose only block, the
    first, is below the fold, rather than going home."""
    from orrery.model import Block

    tall = tuple(
        Block("main", s, k, 1)
        for s in ("math", "finance")
        for k in ("learn", "rep", "fun", "warmup", "geography")
    )
    low = tuple(Block("residual", "geography", k, 1) for k in ("geography", "rep", "learn"))
    with closing(db.open_existing(week_home)) as conn:
        with ledger.change(conn, actor="orrery", kind="setup", now=NOW):
            schedule.write_template(
                conn,
                {0: tall, 1: tall, 2: tall, 3: tall, 4: tall[:2] + low, 5: tall, 6: low[:1]},
            )
    app = _app(week_home)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(pilot, app)
        view, pane = _view(app), app.query_one(WeekPane)

        def in_view() -> bool:
            rows = view.item_rows()
            return bool(rows) and all(
                pane.region.y <= view.region.y + y < pane.region.bottom for y in rows
            )

        await pilot.press("down", "down", "down")
        await _until(pilot, in_view)
        assert (app.cursor_date, view.item, pane.scroll_y) == (THU, 3, 0)
        await pilot.press("right")  # Friday's fourth block: its second after the main part
        await settle(pilot, app)
        await _until(pilot, in_view)
        assert (app.cursor_date, view.item) == (FRI, 3) and pane.scroll_y > 0
        await pilot.press("right", "right")  # Sunday's one block, under Saturday's tall main
        await settle(pilot, app)
        await _until(pilot, in_view)
        assert (app.cursor_date, view.item) == (SUN, 0) and pane.scroll_y > 0


@pytest.mark.parametrize("theme", THEMES)
async def test_today_is_a_chip_in_every_theme(week_home, theme):
    """Today's head is a chip, ` Thu 12 ` filled in the accent: its text apart from its fill
    and the fill from the view's background, in the ANSI themes too (reverse video there:
    their $text is the terminal's default, which on the accent is the month cursor's look).
    Drawn the same with the cursor on today or not."""
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        app.theme = theme
        await pilot.pause()
        x, y = _find(app, "Thu 12", 3)
        row = _cells(_view(app), y)
        chip, plain = row[x], _style(app, *_find(app, "Wed 11", 2)).bgcolor
        _assert_chip(chip, plain)
        if not theme.startswith("ansi"):
            assert _shown(chip)[1].triplet == _rgb(app, "accent")
        assert bool(chip.reverse) == theme.startswith("ansi") and not chip.underline
        assert {_look(s) for s in row[x - 1 : x + len("Thu 12 ")]} == {_look(chip)}  # padded
        await pilot.press("left")
        await settle(pilot, app)
        assert _look(_style(app, x, y)) == _look(chip)


async def test_the_chip_reads_in_every_theme(week_home):
    """In every theme the app has, today's chip's text reads on its fill, as _assert_chip
    measures it. Its text as the theme's background read 1.49:1 in textual-light."""
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        for theme in sorted(app.available_themes):
            app.theme = theme
            await pilot.pause()
            plain = _style(app, *_find(app, "Wed 11", 2)).bgcolor
            _assert_chip(_style(app, *_find(app, "Thu 12", 3)), plain)


async def test_no_lift_on_todays_column_or_cell(week_home):
    """Nothing under today's head is on a background of its own: with the cursor elsewhere its
    column is the view's background throughout; with it on today, only the focused block's
    lines are the cursor's."""
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        view, cursor = _view(app), _rgb(app, "accent-muted")
        plain = _bg(app, "Mon 9", 0)
        thu = range(_x(app, 3), _x(app, 3) + view.col)

        def fills(y: int) -> set:
            return {_cells(view, y)[x].bgcolor.triplet for x in thu}

        rows = range(1, view.size.height)  # under the head
        on = view.item_rows()
        assert on and [fills(y) for y in rows] == [{cursor} if y in on else {plain} for y in rows]
        await pilot.press("left")
        await settle(pilot, app)
        assert [fills(y) for y in rows] == [{plain}] * len(rows)


async def test_week_cursor_fills_only_the_focused_block(week_home):
    """The cursor is the focused block's background, $accent-muted, and nothing else's: no head
    is filled, and the focused day's head is underlined in its own colour unless it's today's,
    which stays the chip."""
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        cursor = _rgb(app, "accent-muted")
        names = ["Mon 9", "Tue 10", "Wed 11", "Thu 12", "Fri 13", "Sat 14", "Sun 15"]

        def heads():
            return [_style(app, *_find(app, n, i)) for i, n in enumerate(names)]

        plain = heads()[0].bgcolor
        assert not any(h.underline for h in heads())  # today focused: its chip only
        assert _on(app, cursor) == _block(app, 3)
        await pilot.press("right")
        await settle(pilot, app)
        assert [bool(h.underline) for h in heads()] == [i == 4 for i in range(7)]
        fri = heads()[4]
        assert fri.bold and fri.color.triplet == _rgb(app, "foreground")
        assert all(h.bgcolor == plain for i, h in enumerate(heads()) if i != 3)
        assert _on(app, cursor) == _block(app, 4)
        await pilot.press("down")
        await settle(pilot, app)
        assert _view(app).item == 1 and _on(app, cursor) == _block(app, 4)
        assert _bg(app, "▌math", 4) != cursor  # the first block let go


async def test_cursor_on_a_day_without_blocks_still_shows(week_home):
    """On a day with no block to fill (a rest day, one before starts, an empty future one),
    the cursor shows as its underlined head. On today with no block, today's chip is the
    only mark, as at every start: nothing is on the cursor's background, no head underlined,
    and the day panel names today."""
    app = _app(week_home, now=datetime(2026, 11, 15, 9, 0))  # Sunday, a rest day
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        view, cursor = _view(app), _rgb(app, "accent-muted")
        assert view.focus_date == SUN and "rest" in _column(app, 6)
        plain = _style(app, *_find(app, "Sat 14", 5)).bgcolor
        _assert_chip(_style(app, *_find(app, "Sun 15", 6)), plain)
        assert not _on(app, cursor)
        assert not any(s.underline for s in _cells(view, 0))
        assert _top(app.query_one(DayPanel)).endswith(" today ─╮")
        await pilot.press("left_square_bracket")  # before starts: nothing under the heads
        await settle(pilot, app)
        assert view.focus_date == date(2026, 11, 8) and not any(_column(app, 6)[1:])
        head = _style(app, *_find(app, "Sun 8", 6))
        assert head.underline and head.bgcolor == plain and not _on(app, cursor)
        await pilot.press("right_square_bracket", "right_square_bracket")  # empty, the future
        await settle(pilot, app)
        assert view.focus_date == date(2026, 11, 22) and "rest" in _column(app, 6)
        head = _style(app, *_find(app, "Sun 22", 6))
        assert head.underline and head.bgcolor == plain and not _on(app, cursor)


@pytest.mark.parametrize("size", [(80, 24), (120, 40), (160, 48), BIG])
async def test_heads_keep_their_columns_with_the_chip(week_home, size):
    """Every head starts at its column's left edge, a space then its name, today's too: the
    chip's padding is that space and one after the name, inside its column."""
    app = _app(week_home)
    async with app.run_test(size=size) as pilot:
        await settle(pilot, app)
        view = _view(app)
        names = ["Mon 9", "Tue 10", "Wed 11", "Thu 12", "Fri 13", "Sat 14", "Sun 15"]
        head = _rows(app)[0]
        assert [head[_x(app, i) : _x(app, i + 1)] for i in range(7)] == [
            f" {n}".ljust(view.col) for n in names
        ]
        chip = _on(app, _rgb(app, "accent"))
        assert chip == {(x, 0) for x in range(_x(app, 3), _x(app, 3) + len(" Thu 12 "))}
        assert len(" Thu 12 ") <= view.col and _widest(view) <= view.size.width


async def test_week_and_day_headers_name_another_year(week_home):
    """The header says the year only when it isn't this one: not for week 53, which is
    2026's, but for the week after it; and the day panel likewise."""
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        await pilot.press(*["right_square_bracket"] * 7)
        await settle(pilot, app)
        assert str(app.query_one("#controls").content) == "‹ Dec 28 – Jan 3, week 53 ›"
        await pilot.press("right_square_bracket")
        await settle(pilot, app)
        assert str(app.query_one("#controls").content) == "‹ Jan 4 – 10, week 1 of 2027 ›"
        assert _top(app.query_one(DayPanel)).startswith("╭─ Thu 7 January 2027 ──")
        await pilot.press(*["left"] * 7)
        await settle(pilot, app)
        assert str(app.query_one("#controls").content) == "‹ Dec 28 – Jan 3, week 53 ›"
        assert _top(app.query_one(DayPanel)).startswith("╭─ Thu 31 December ──")


async def test_week_blocks_are_one_line_with_pips_or_extra(week_home):
    """At 235×60 each block is one line, `▌subject kind ●○` (no kind when it is the subject)
    or `▌subject kind +N extra`, and its topic, muted, goes on a second line only when the
    agenda has one. The top panel is titled This week."""
    with closing(db.open_existing(week_home)) as conn:
        report_and_collect(conn, MON, "math", "learn", 1)
        report_and_collect(conn, MON, "math", "learn", 1, status="partial")
        report_and_collect(conn, MON, "geography", "geography", 2)  # one past the plan
        report_and_collect(conn, MON, "finance", "rep", 1)  # off the plan
        add_agenda(conn, "math", [channels.AgendaEntry(FRI, "zorbs 3", "learn")])
    app = _app(week_home)
    async with app.run_test(size=BIG) as pilot:
        await settle(pilot, app)
        assert _view(app).col == 22
        mon = [x for x in _column(app, 0) if x]
        assert mon[:4] == ["Mon 9", "▌math rep ○", "▌math learn ●◐", "▌geography ●"]
        assert sorted(mon[4:]) == ["▌finance rep +1 extra", "▌geography +1 extra"]
        assert [x for x in _column(app, 4) if x] == [
            "Fri 13", "▌math learn ○○", "▌zorbs 3", "▌geography ○"
        ]  # fmt: skip
        assert _fg(app, "zorbs 3", 4) == _muted(app)
        assert _fg(app, "learn", 4) == _rgb(app, "foreground")
        assert _fg(app, "○○", 4) == _hex(MATH_DARK)
        labels = [row[: week.GUTTER].strip() for row in _rows(app)]
        assert [x for x in labels if x] == ["warm", "main", "after", "extra"]
        assert _top(_view(app)).startswith("╭─ This week ──")


def _spy_on_day_panel(monkeypatch) -> list[date]:
    """The days DayPanel.show is called with, in order."""
    shown, real = [], DayPanel.show

    def spy(self, day, *args, **kw):
        shown.append(day)
        real(self, day, *args, **kw)

    monkeypatch.setattr(DayPanel, "show", spy)
    return shown


async def test_day_panel_follows_the_cursor_once_per_move(week_home, monkeypatch):
    """The day panel is the cursor's day: each move inside the week shows it once, from the
    week already read, with no reload; ↑/↓ between blocks leave it be. It never takes the
    keyboard, not even from a click."""
    shown = _spy_on_day_panel(monkeypatch)
    app = _app(week_home)
    reloads = _counting_reloads(app)
    async with app.run_test(size=BIG) as pilot:
        await settle(pilot, app)
        panel = app.query_one(DayPanel)
        assert shown == [THU] and not panel.can_focus and app.focused is _view(app)
        assert _top(panel).startswith("╭─ Thu 12 November ──")
        assert _top(panel).endswith(" today ─╮")
        first = reloads[0]
        await pilot.press("left")
        await settle(pilot, app)
        assert shown == [THU, WED] and _top(panel).startswith("╭─ Wed 11 November ──")
        assert "main   ▌finance rep ○" in _day(app)
        await pilot.press("down", "up", "right", "right")
        await settle(pilot, app)
        assert shown == [THU, WED, THU, FRI] and reloads[0] == first
        await pilot.click(DayPanel)
        await settle(pilot, app)
        assert app.focused is _view(app)


async def test_day_panel_after_crossing_a_week_shows_the_new_week_day(week_home, monkeypatch):
    """Past Sunday the cursor's Monday isn't read yet: the panel keeps Sunday until next
    week's read lands, then shows Monday, once."""
    shown = _spy_on_day_panel(monkeypatch)
    app = _app(week_home)
    async with app.run_test(size=BIG) as pilot:
        await settle(pilot, app)
        await pilot.press("right", "right", "right")
        await settle(pilot, app)
        release, real = threading.Event(), app._read

        def held():
            release.wait(5)
            return real()

        app._read = held
        try:
            await pilot.press("right")
            await pilot.pause()
            assert app.cursor_date == date(2026, 11, 16) and shown[-1] == SUN
        finally:
            release.set()
        await settle(pilot, app)
        assert shown == [THU, FRI, SAT, SUN, date(2026, 11, 16)]
        assert _top(app.query_one(DayPanel)).startswith("╭─ Mon 16 November ──")
        assert "warm   ▌math rep ○" in _day(app)


@pytest.mark.parametrize("size", [(235, 60), (160, 45), (159, 45)])
async def test_lower_row_side_by_side_at_panels_and_stacked_below(week_home, size):
    """From 160 columns, the top panel, and under it so far this week above the last 5 weeks
    on the left and the day panel on the right, all in the window beside the chat. Narrower,
    the four stack in one column."""
    app = _app(week_home)
    async with app.run_test(size=size) as pilot:
        await settle(pilot, app)
        top, so_far, last, day = (
            app.query_one(w).region for w in (WeekView, SoFar, LastWeeks, DayPanel)
        )
        chat = app.query_one(ChatPanel).region
        if size[0] >= 160:
            assert top.x == so_far.x == last.x and top.right == day.right == chat.x
            assert top.bottom <= so_far.y == day.y and so_far.bottom <= last.y
            assert last.right <= day.x and last.bottom == day.bottom <= size[1] - 3  # key line
        else:
            assert top.x == so_far.x == last.x == day.x
            assert top.width == so_far.width == last.width == day.width
            assert top.bottom <= so_far.y < so_far.bottom <= last.y < last.bottom <= day.y


NOTE = (  # lorem-style: long enough to wrap, about nothing real
    "Lorem drills on the quib tables: zorbs 1 to 4 read, 5 half read. Ipsum recap of the"
    " flarn rules next, then a dolor set on the glim lemmas once the recap is through."
)


@pytest.mark.parametrize("size", [(235, 60), (160, 45), (120, 40)])
async def test_no_required_text_cut_at_235x60_and_120x40(week_home, size):
    """Nothing is cut: no row of the timetable, so far this week, the last 5 weeks or the
    day panel ends in an ellipsis, each one's required text is drawn in full, and the day
    panel's note wraps in full. (render_line crops to the panel, so a row's length can't
    tell.)"""
    with closing(db.open_existing(week_home)) as conn:
        report_and_collect(
            conn, THU, "geography", "geography", 1, note=NOTE,
            started_at=f"{THU}T13:00:00", ended_at=f"{THU}T13:50:00",
        )  # fmt: skip
        report_and_collect(conn, THU, "math", "learn", 3)  # one past the plan
        report_and_collect(conn, THU, "finance", "rep", 1)  # off the plan
    required = {
        WeekView: [*(f"{d:%a} {d.day}" for d in (MON, TUE, WED, THU, FRI, SAT, SUN))]
        + ["▌math", "▌finance", "▌geography", "●●", "rest"],
        SoFar: ["▌math", "2 of 6", "10 in the week", "▌finance", "0 of 3", "3 in the week"]
        + ["▌geography", "1 of 4", "5 in the week", "questions waiting: math 3"],
        LastWeeks: ["W42", "W46", "33%", "0%", "25%", DOTS],
        DayText: ["main   ▌math learn ●● +1 extra", "after  ▌geography ●"]
        + ["       ▌ 13:00–13:50  1 session", "extra  ▌finance rep +1 extra"],
    }
    app = _app(week_home)
    async with app.run_test(size=size) as pilot:
        await settle(pilot, app)
        for cls, texts in required.items():
            rows = [row.rstrip() for row in _lines(app.query_one(cls))]
            assert rows and not any("…" in row for row in rows), (cls, rows)
            for text in texts:
                assert any(text in row for row in rows), (cls, text, rows)
        drawn = "\n".join(_lines(_view(app)))
        assert drawn.count("+1 extra") == 2 and drawn.count("▌geography") == 5
        said = " ".join(row.split("▌ ", 1)[1] for row in _day(app) if "▌ " in row)
        assert NOTE in said
