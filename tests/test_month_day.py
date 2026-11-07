"""The Week tab's month view and its day panel, through the Pilot: the calendar of pips; the
cursor's day with its ribbon, blocks, notes and chat changes; and w, m and enter between the
week and the month."""

import threading
from contextlib import closing
from datetime import date, datetime, timedelta

import pytest
from helpers import (
    NOW,
    TODAY,
    add_agenda,
    chat_keys,
    keep_move,
    propose_move,
    report_and_collect,
    settle,
)
from test_frame import _assert_muted, _line_with, _rgb
from test_tui import _until
from test_week_view import THEMES, _assert_chip, _cells, _ink, _look, _shown, _spy_on_day_panel
from textual.color import Color
from textual.content import Content, Span
from textual.geometry import Region

from orrery import channels, db, ledger, proposals, settings, text
from orrery.organizer import sessions
from orrery.tui.app import OrreryApp
from orrery.tui.day import GUTTER, DayPanel, DayText
from orrery.tui.month import LINES, MonthView
from orrery.tui.views import Popup
from orrery.tui.week import SoFar, WeekView

WIDE = (120, 40)
BIG = (235, 60)  # the size orrery is used at: the day panel beside the lower left panels
THU = TODAY
MON, TUE, WED, FRI, SAT, SUN = (THU + timedelta(days=n) for n in (-3, -2, -1, 1, 2, 3))
MATH_DARK, FINANCE_DARK, GEOGRAPHY_DARK = "#3987e5", "#d95926", "#199e70"
BEFORE = "before orrery started"
EDGE = 1  # a panel's border, outside render_line's content
PAD = 1  # the month view's left padding, likewise


def _app(home, now: datetime = NOW) -> OrreryApp:
    return OrreryApp(root=lambda: home, now=lambda: now)


def _lines(v) -> list[str]:
    return [v.render_line(y).text for y in range(v.size.height)]


def _find(v, s: str) -> tuple[int, int]:
    """(x, y) of s's first cell in v."""
    for y, row in enumerate(_lines(v)):
        if (x := row.find(s)) >= 0:
            return x, y
    raise AssertionError(f"{s!r} not drawn")


def _style(v, x: int, y: int):
    at = 0
    for seg in v.render_line(y):
        if at <= x < at + seg.cell_length:
            return seg.style
        at += seg.cell_length
    raise AssertionError(f"nothing at {x}, {y}")


def _fg(v, x: int, y: int):
    return _style(v, x, y).color.triplet


def _bg(v, x: int, y: int):
    return _style(v, x, y).bgcolor.triplet


def _hex(h: str):
    return Color.parse(h).rgb


def _muted(v):
    over = Color.parse(v.app.get_css_variables()["foreground-muted"])
    return (v.background_colors[1] + over).rgb


def _controls(app) -> str:
    return str(app.query_one("#controls").content)


def _title(app) -> str:
    """The day panel's top border as drawn: the day, and a caption on the right."""
    panel = app.query_one(DayPanel)
    return panel.render_lines(Region(0, 0, *panel.outer_size))[0].text


def _drawn(v) -> list[str]:
    return [row.rstrip() for row in _lines(v) if row.strip()]


# The month view: rows are the header, a blank, then per week its dates, pips, topic and a blank.
def _date_y(i: int) -> int:
    return 2 + 4 * i


def _cell(v: MonthView, y: int, i: int) -> str:
    """Day i's cell on row y of the calendar, stripped."""
    x = i * v.col
    return _lines(v)[y][x : x + v.col].strip()


def _cell_x(v: MonthView, i: int) -> int:
    return i * v.col


async def _month(pilot, app) -> MonthView:
    await settle(pilot, app)
    await pilot.press("m")
    await settle(pilot, app)
    return app.query_one(MonthView)


async def _day(pilot, app, *keys: str) -> DayText:
    """The day panel's text, after keys in the week."""
    await settle(pilot, app)
    await pilot.press(*keys)
    await settle(pilot, app)
    return app.query_one(DayText)


async def test_month_grid_six_rows_pips_today_and_before_starts_note(week_home):
    with closing(db.open_existing(week_home)) as conn:
        report_and_collect(conn, MON, "math", "learn", 1)
        report_and_collect(conn, MON, "math", "learn", 1, status="partial")
        add_agenda(conn, "math", [channels.AgendaEntry(FRI, "zorbs 3", "learn")])
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        v = await _month(pilot, app)
        assert app.view == "month" and app.focused is v and app.cursor_date == THU
        assert not app.query_one(WeekView).display and app.query_one(SoFar).display
        assert _controls(app) == "‹ November 2026 ›"
        top = v.render_lines(Region(0, 0, *v.outer_size))[0].text
        assert top.startswith("╭─ November 2026 ──") and top.endswith(" cursor on Thu 12 ─╮")
        rows = _lines(v)
        assert rows[0].split() == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        assert [rows[_date_y(i)].split() for i in range(6)] == [
            ["26", "27", "28", "29", "30", "31", "1"],
            ["2", "3", "4", "5", "6", "7", "8"],
            ["9", "10", "11", "12", "13", "14", "15"],
            ["16", "17", "18", "19", "20", "21", "22"],
            ["23", "24", "25", "26", "27", "28", "29"],
            ["30", "1", "2", "3", "4", "5", "6"],
        ]
        # days before starts empty, one note, in the last week of them
        assert [y for y, row in enumerate(rows) if BEFORE in row] == [_date_y(1) + 1]
        _assert_muted(v, v.render_line(_date_y(1) + 1))
        assert rows[_date_y(0) + 1].strip() == rows[_date_y(0) + 2].strip() == ""
        # pips per block, in the subject's hue, filled once reported; the topic muted
        pips = _date_y(2) + 1
        assert [_cell(v, pips, i) for i in range(7)] == [
            "○ ●◐ ○", "○○ ○", "○ ○ ○", "○○ ○", "○○ ○", "○ ○", "rest"
        ]  # fmt: skip
        x = rows[pips].index("●◐")
        assert _fg(v, x, pips) == _fg(v, x + 1, pips) == _hex(MATH_DARK)
        assert _fg(v, x + 3, pips) == _hex(GEOGRAPHY_DARK)
        assert _cell(v, pips + 1, 4) == "zorbs 3"
        _assert_muted(v, v.render_line(pips + 1))
        # future days from the template, the next month's day numbers muted
        assert _cell(v, _date_y(5) + 1, 1) == "○○ ○"
        assert _fg(v, _cell_x(v, 1), _date_y(5)) == _muted(v)
        assert _fg(v, _cell_x(v, 0), _date_y(5)) == _rgb(app, "foreground")
        # the cursor starts on today: its cell on the cursor's background, today's chip on it;
        # off it, the chip alone, the cell unlifted
        today, cursor = (_cell_x(v, 3), _date_y(2)), _rgb(app, "accent-muted")
        plain = _style(v, _cell_x(v, 1), _date_y(2)).bgcolor
        beside = today[0] + len(" 12 "), today[1]
        _assert_chip(_style(v, *today), plain)
        assert _shown(_style(v, *today))[1].triplet == _rgb(app, "accent")
        assert _bg(v, *beside) == _bg(v, today[0], pips) == cursor
        await pilot.press("right")
        await settle(pilot, app)
        _assert_chip(_style(v, *today), plain)
        assert _bg(v, *beside) == _bg(v, today[0], pips) == plain.triplet
        assert _bg(v, _cell_x(v, 4), pips) == cursor
        at = (EDGE + PAD + _cell_x(v, 1) + 1, EDGE + _date_y(3) + 1)
        await pilot.click(MonthView, offset=at)
        await settle(pilot, app)
        assert app.cursor_date == date(2026, 11, 17)


async def test_month_arrows_cross_month_edges(week_home):
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        v = await _month(pilot, app)
        await pilot.press("down", "down")
        await settle(pilot, app)
        assert app.cursor_date == date(2026, 11, 26) and _controls(app) == "‹ November 2026 ›"
        await pilot.press("down")
        await settle(pilot, app)
        assert app.cursor_date == date(2026, 12, 3) and _controls(app) == "‹ December 2026 ›"
        assert _lines(v)[_date_y(0)].split() == ["30", "1", "2", "3", "4", "5", "6"]
        await pilot.press("up")
        await settle(pilot, app)
        assert _controls(app) == "‹ November 2026 ›"
        await pilot.press("right", "right", "right", "right", "right")  # Mon 30, then Tue 1
        await settle(pilot, app)
        assert app.cursor_date == date(2026, 12, 1) and _controls(app) == "‹ December 2026 ›"
        await pilot.press("left")
        await settle(pilot, app)
        assert app.cursor_date == date(2026, 11, 30) and _controls(app) == "‹ November 2026 ›"


async def test_month_edge_crossing_shows_the_day_once_per_move(week_home, monkeypatch):
    """Across the month's edge, as across the week's in the week: the day panel shows the
    cursor's day once per move, from the read of its month, whether or not the month on
    screen draws that day too."""
    shown = _spy_on_day_panel(monkeypatch)
    app = _app(week_home)
    async with app.run_test(size=BIG) as pilot:
        await _month(pilot, app)
        await pilot.press("down", "down")
        await settle(pilot, app)
        shown.clear()
        await pilot.press("down")  # Thu 3 December: November's grid draws it too
        await settle(pilot, app)
        assert shown == [date(2026, 12, 3)] and _controls(app) == "‹ December 2026 ›"
        await pilot.press("up")  # back to Thu 26 November: December's grid doesn't
        await settle(pilot, app)
        assert shown == [date(2026, 12, 3), date(2026, 11, 26)]
        assert _title(app).startswith("╭─ Thu 26 November ──")


async def test_month_brackets_jump_a_month(week_home):
    """A month before starts and one years away draw without errors."""
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        v = await _month(pilot, app)
        await pilot.press("right_square_bracket")
        await settle(pilot, app)
        assert app.cursor_date == date(2026, 12, 12) and _controls(app) == "‹ December 2026 ›"
        await pilot.press("right_square_bracket")
        await settle(pilot, app)
        assert app.cursor_date == date(2027, 1, 12) and _controls(app) == "‹ January 2027 ›"
        await pilot.press(*["left_square_bracket"] * 3)
        await settle(pilot, app)
        assert app.cursor_date == date(2026, 10, 12) and _controls(app) == "‹ October 2026 ›"
        rows = _lines(v)
        assert sum(BEFORE in row for row in rows) == 1
        assert not any(p in row for row in rows for p in "●◐○")
        app.cursor_date = date(2027, 1, 31)
        app.app_reload()
        await settle(pilot, app)
        await pilot.press("right_square_bracket")  # no Feb 31: its last day
        await settle(pilot, app)
        assert app.cursor_date == date(2027, 2, 28) and _controls(app) == "‹ February 2027 ›"
        app.cursor_date = date(2031, 6, 4)
        app.app_reload()
        await settle(pilot, app)
        assert _controls(app) == "‹ June 2031 ›" and "○○ ○" in _lines(v)[_date_y(1) + 1]
        await pilot.press("t")
        await settle(pilot, app)
        assert app.cursor_date == THU and _controls(app) == "‹ November 2026 ›"


async def test_month_shows_inactive_subjects_with_their_colour(week_home):
    with closing(db.open_existing(week_home)) as conn:
        report_and_collect(conn, TUE, "finance", "learn", 1)
        with ledger.change(conn, actor="orrery", kind="setup", now=NOW):
            conn.execute("update subjects set active = 0 where name = 'finance'")
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        v = await _month(pilot, app)
        pips = _date_y(2) + 1
        assert _cell(v, pips, 1) == "●○ ○"
        x = _cell_x(v, 1)
        assert _fg(v, x, pips) == _fg(v, x + 1, pips) == _hex(FINANCE_DARK)
        assert _fg(v, x + 3, pips) == _hex(GEOGRAPHY_DARK)  # its own slot, not finance's


async def test_month_says_rest_only_on_a_day_without_blocks(week_home):
    """A day whose blocks all moved away shows their pips struck, a day with only sessions off
    the plan says +N extra, and only a day with no blocks says rest."""
    later = datetime(2026, 11, 19, 9, 0)
    with closing(db.open_existing(week_home)) as conn:
        report_and_collect(conn, SUN, "finance", "rep", 1, now=later)  # on a rest day
        for subject, kind in (("math", "learn"), ("geography", "geography")):
            keep_move(conn, date(2026, 11, 20), subject, kind, date(2026, 11, 21))
    app = _app(week_home, later)
    async with app.run_test(size=WIDE) as pilot:
        v = await _month(pilot, app)
        y = _date_y(2) + 1
        assert _cell(v, y, 6) == "+1 extra" and _fg(v, _cell_x(v, 6), y) == _muted(v)
        y = _date_y(3) + 1
        assert [_cell(v, y, i) for i in (4, 5, 6)] == ["○○ ○", "○ ○○ ○ ○", "rest"]
        x = _cell_x(v, 4)
        for at in (x, x + 1, x + 3):
            assert _style(v, at, y).strike and _fg(v, at, y) == _muted(v)


def _timed(conn, d: date, subject: str, kind: str, n: int, start: str, end: str, **kw):
    report_and_collect(
        conn, d, subject, kind, n, started_at=f"{d}T{start}:00", ended_at=f"{d}T{end}:00", **kw
    )


async def test_day_panel_ribbon_places_timed_sessions(week_home):
    with closing(db.open_existing(week_home)) as conn:
        _timed(conn, MON, "math", "learn", 1, "09:00", "10:30")
        _timed(conn, MON, "math", "learn", 1, "13:00", "14:00", status="partial")
        _timed(conn, MON, "geography", "geography", 1, "20:00", "20:45")
        report_and_collect(conn, MON, "math", "rep", 1)  # untimed: not on the ribbon
    app = _app(week_home)
    async with app.run_test(size=BIG) as pilot:
        v = await _day(pilot, app, "left", "left", "left")
        assert app.cursor_date == MON and _title(app).startswith("╭─ Mon 9 November ──")
        assert v.per == 3  # cells an hour, 05:00 to 05:00 the next morning
        x, y = _find(v, "┄")
        assert x == GUTTER
        assert _lines(v)[y][x:].rstrip() == "".join(
            ["┄" * 12, "█" * 5, "┄" * 7, "█" * 3, "┄" * 18, "█" * 3, "┄" * 24]
        )
        assert _fg(v, x + 12, y) == _fg(v, x + 24, y) == _hex(MATH_DARK)
        assert _fg(v, x + 45, y) == _hex(GEOGRAPHY_DARK)
        assert _fg(v, x, y) == _muted(v)
        assert (
            _lines(v)[y + 1][x:].rstrip()
            == "   " + "".join(f"{h:02}".ljust(9) for h in (6, 9, 12, 15, 18, 21, 0)) + "03"
        )
        await pilot.resize_terminal(80, 24)
        await _until(pilot, lambda: v.per == 2)  # the frame narrowed: 2 cells an hour
        assert _lines(v)[y][x:].rstrip() == "".join(
            ["┄" * 8, "█" * 3, "┄" * 5, "█" * 2, "┄" * 12, "█" * 2, "┄" * 16]
        )


async def test_day_panel_ribbon_runs_past_midnight_to_the_day_boundary(week_home):
    """The ribbon is the sitting day, from the day boundary to the same hour the next
    morning: a sitting across midnight and those after it, still Wednesday's, all show."""
    thu = f"{THU}T"
    with closing(db.open_existing(week_home)) as conn:
        for subject, kind, start, end in (
            ("geography", "geography", f"{WED}T21:02", f"{thu}00:07"),
            ("math", "learn", f"{thu}00:08", f"{thu}01:44"),
            ("finance", "learn", f"{thu}01:50", f"{thu}02:40"),
            ("math", "learn", f"{thu}02:50", f"{thu}03:44"),
        ):
            report_and_collect(conn, WED, subject, kind, 1, started_at=start, ended_at=end)
    app = _app(week_home)
    async with app.run_test(size=BIG) as pilot:
        v = await _day(pilot, app, "left")
        assert app.cursor_date == WED and v.per == 3
        x, y = _find(v, "┄")
        # 21:02 is 16h02 after 05:00, cell 48; 03:44 ends in cell 68
        assert _lines(v)[y][x:].rstrip() == "┄" * 48 + "█" * 21 + "┄" * 3
        assert _fg(v, x + 50, y) == _hex(GEOGRAPHY_DARK)
        assert _fg(v, x + 60, y) == _fg(v, x + 67, y) == _hex(MATH_DARK)
        assert _fg(v, x + 63, y) == _hex(FINANCE_DARK)
        hours = _lines(v)[y + 1]
        assert hours[x + 48 : x + 50] == "21" and hours[x + 57 : x + 59] == "00"
        assert hours[x + 66 : x + 68] == "03"
        for times in ("21:02–00:07", "00:08–01:44", "01:50–02:40", "02:50–03:44"):
            assert any(times in row for row in _lines(v)), times


async def test_day_panel_ribbon_starts_at_the_settings_day_boundary(week_home):
    settings.update_config(week_home / "config.toml", {"day_boundary_hour": 3})
    with closing(db.open_existing(week_home)) as conn:
        _timed(conn, MON, "math", "learn", 1, "03:00", "04:00")
    app = _app(week_home)
    async with app.run_test(size=BIG) as pilot:
        v = await _day(pilot, app, "left", "left", "left")
        x, y = _find(v, "█")
        assert x == GUTTER and _lines(v)[y][x:].rstrip() == "█" * 3 + "┄" * 69
        assert _lines(v)[y + 1][x : x + 2] == "03"


async def test_day_panel_shows_session_times_and_that_day_in_the_chat(week_home):
    """Each block after its part: its pips, its topic muted, then each report's times and
    sessions, muted, and the mentor's note; a planned block of a day gone by with none says
    so. Then the deadlines, and what the chat kept and noted that day."""
    with closing(db.open_existing(week_home)) as conn:
        _timed(conn, MON, "math", "learn", 1, "09:00", "10:30", note="lorem lemmas, two examples")
        _timed(conn, MON, "math", "learn", 1, "13:00", "14:00", status="partial")
        _timed(conn, MON, "geography", "geography", 1, "20:00", "20:45", note="two ipsum drills")
        report_and_collect(conn, MON, "finance", "rep", 1, note="a first look")  # unplanned
        add_agenda(conn, "math", [
            channels.AgendaEntry(MON, "glim rules", "learn"),
            channels.AgendaEntry(MON, "problem set 3", deadline=True),
        ])  # fmt: skip
        p = propose_move(conn, rationale="a lighter Thursday")
        proposals.keep(conn, p.id, now=NOW, today=TODAY)
        sessions.remember(conn, "protect Monday math", NOW)
    app = _app(week_home)
    async with app.run_test(size=BIG) as pilot:
        v = await _day(pilot, app, "left", "left", "left")
        drawn = _drawn(v)
        start = next(i for i, row in enumerate(drawn) if "▌" in row)
        assert drawn[start:] == [
            "warm   ▌math rep ○",
            "       ▌ nothing reported",
            "main   ▌math learn ●◐",
            "       ▌ glim rules",
            "       ▌ 09:00–10:30  1 session",
            "       ▌ lorem lemmas, two examples",
            "       ▌ 13:00–14:00  1 session, partial",
            "after  ▌geography ●",
            "       ▌ 20:00–20:45  1 session",
            "       ▌ two ipsum drills",
            "extra  ▌finance rep +1 extra",
            "       ▌ 1 session",
            "       ▌ a first look",
            "due    ▌problem set 3",
            "That day in the chat",
            "nothing kept or noted",
        ]
        x, y = _find(v, "▌math learn")
        assert _fg(v, x, y) == _hex(MATH_DARK) and _fg(v, x + 1, y) == _rgb(app, "foreground")
        assert _fg(v, *_find(v, "glim rules")) == _muted(v)
        assert _fg(v, *_find(v, "09:00")) == _muted(v)
        assert _fg(v, *_find(v, "lorem lemmas")) == _rgb(app, "foreground")
        assert _fg(v, *_find(v, "▌problem set 3")) == _hex(MATH_DARK)
        _assert_muted(v, _line_with(v, "nothing kept or noted"))
        assert not any(text.has_verdict(row) for row in drawn)
        await pilot.press("t")
        await settle(pilot, app)
        drawn = [" ".join(row.split()) for row in _drawn(v)]
        assert drawn[drawn.index("That day in the chat") + 1 :] == [
            f"09:00 kept proposal #{p.id} · a lighter Thursday",
            "09:00 noted: protect Monday math",
        ]
        assert "main ▌math learn ○○ → Fri 13" in drawn  # where Thursday's math went


async def test_day_panel_wraps_every_note_in_full(week_home):
    """A note longer than the panel is wrapped under its edge, every word of it kept, at the
    wide layout and the stacked ones alike; an untimed report shows its sessions alone."""
    note = " ".join(f"word{i}" for i in range(60))  # far wider than the panel
    with closing(db.open_existing(week_home)) as conn:
        report_and_collect(conn, THU, "math", "learn", 2, note=note)
        report_and_collect(conn, THU, "geography", "geography", 1, note="short and sweet")
    app = _app(week_home)
    async with app.run_test(size=BIG) as pilot:
        for size in (BIG, WIDE, (80, 24)):
            await pilot.resize_terminal(*size)
            await settle(pilot, app)
            v = app.query_one(DayText)
            rows = _drawn(v)
            at = rows.index("main   ▌math learn ●●")
            assert rows[at + 1] == "       ▌ 2 sessions"
            body = []
            for row in rows[at + 2 :]:
                if not row.startswith("       ▌ "):
                    break
                body.append(row.removeprefix("       ▌ "))
            # every word in full, in order: a row cropped at the panel's edge would lose one
            assert len(body) > 1 and " ".join(body) == note, size
            assert "       ▌ short and sweet" in rows
            assert not any("…" in row for row in rows), size


async def test_day_panel_zero_session_partial_shows_its_note(week_home):
    with closing(db.open_existing(week_home)) as conn:
        report_and_collect(
            conn, MON, "math", "learn", 0, status="partial", note="stopped at the first zorb"
        )
    app = _app(week_home)
    async with app.run_test(size=BIG) as pilot:
        v = await _day(pilot, app, "left", "left", "left")
        drawn = _drawn(v)
        at = drawn.index("main   ▌math learn ○○")  # no pip for it
        assert drawn[at + 1 : at + 3] == [
            "       ▌ 0 sessions, partial",
            "       ▌ stopped at the first zorb",
        ]


async def test_day_panel_today_empty_ribbon_hollow_pips(week_home):
    app = _app(week_home)
    async with app.run_test(size=BIG) as pilot:
        v = await _day(pilot, app)
        assert app.cursor_date == THU and _title(app).startswith("╭─ Thu 12 November ──")
        x, y = _find(v, "┄")
        assert _lines(v)[y][x:].rstrip() == "┄" * 72
        drawn = _drawn(v)
        assert "main   ▌math learn ○○" in drawn and "after  ▌geography ○" in drawn
        assert "       ▌ nothing reported" in drawn  # today counts as a day reached


async def test_day_panel_far_in_the_future_draws_its_template(week_home):
    """A day years away draws its template's blocks, hollow, on an empty ribbon, with nothing
    said about reports yet to come."""
    app = _app(week_home)
    async with app.run_test(size=BIG) as pilot:
        v = await _day(pilot, app)
        app.cursor_date = date(2031, 6, 4)
        app.app_reload()
        await settle(pilot, app)
        assert _title(app).startswith("╭─ Wed 4 June 2031 ──")
        drawn = _drawn(v)
        assert "main   ▌math rep ○" in drawn and "after  ▌geography ○" in drawn
        assert not any("nothing reported" in row for row in drawn)
        x, y = _find(v, "┄")
        assert _lines(v)[y][x:].rstrip() == "┄" * 72
        assert drawn[-1].strip() == "nothing kept or noted" and app.is_running


async def test_day_panel_before_starts_says_so(week_home):
    """A day before starts: the ribbon empty, and one muted note in place of its blocks."""
    app = _app(week_home)
    async with app.run_test(size=BIG) as pilot:
        v = await _day(pilot, app, *["left"] * 4)
        assert app.cursor_date == date(2026, 11, 8)
        assert BEFORE in "\n".join(_lines(v)) and not any("▌" in row for row in _lines(v))
        _assert_muted(v, _line_with(v, BEFORE))


@pytest.mark.parametrize("key", ["w", "m"])
async def test_day_change_in_the_week_and_month_moves_only_today(week_home, key):
    """Past the day boundary, today moves (the month's chip, the day panel's caption,
    and t) and the cursor stays on its date."""
    clock = [datetime(2026, 11, 12, 23, 0)]
    app = OrreryApp(root=lambda: week_home, now=lambda: clock[0])
    async with app.run_test(size=BIG) as pilot:
        await settle(pilot, app)
        await pilot.press(key)
        await settle(pilot, app)
        assert _title(app).endswith(" today ─╮")
        clock[0] = datetime(2026, 11, 13, 5, 0)
        app._check_day()
        await settle(pilot, app)
        assert app.day == FRI and app.cursor_date == THU
        assert _title(app).startswith("╭─ Thu 12 November ──")
        assert not _title(app).endswith(" today ─╮")
        if key == "m":
            v = app.query_one(MonthView)
            thu, fri = (_cell_x(v, 3), _date_y(2)), (_cell_x(v, 4), _date_y(2))
            plain = _style(v, _cell_x(v, 1), _date_y(2)).bgcolor
            _assert_chip(_style(v, *fri), plain)
            assert _bg(v, fri[0], fri[1] + 1) == plain.triplet  # its pips: no lift
            assert not _style(v, *thu).bold  # not the chip
            assert _bg(v, *thu) == _rgb(app, "accent-muted")
            assert _controls(app) == "‹ November 2026 ›"
        else:
            v = app.query_one(WeekView)
        assert app.focused is v
        await pilot.press("t")
        await settle(pilot, app)
        assert app.cursor_date == FRI


async def test_enter_in_week_opens_block_reports_and_in_month_opens_its_week(week_home):
    """enter on a week's block opens its reports and agenda (an extra, its block's); a day
    before starts has none to open. enter on a month's day shows its week, on that day."""
    with closing(db.open_existing(week_home)) as conn:
        _timed(conn, MON, "math", "learn", 1, "09:00", "10:30", note="lorem lemmas, two examples")
        report_and_collect(conn, MON, "math", "learn", 1, status="partial")
        report_and_collect(conn, MON, "math", "learn", 1)  # one past the plan
        add_agenda(conn, "math", [
            channels.AgendaEntry(MON, "glim rules", "learn"),
            channels.AgendaEntry(MON, "problem set 3", deadline=True),
        ])  # fmt: skip
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        week = app.query_one(WeekView)
        await pilot.press("left", "left", "left", "down", "enter")
        await pilot.pause()
        assert isinstance(app.screen, Popup)
        assert app.screen.text.splitlines() == [
            "Mon 11/9 main: math · learn ×2",
            "topic: glim rules",
            "+1 extra",
            "due: problem set 3",
            "",
            "09:00–10:30  1 session · lorem lemmas, two examples",
            "1 session, partial",
            "1 session",
        ]
        await pilot.press("escape", "down", "enter")  # the geography: nothing reported
        await pilot.pause()
        assert app.screen.text.splitlines() == [
            "Mon 11/9 after: geography ×1", "", "no reports filed"
        ]  # fmt: skip
        await pilot.press("escape", "down", "enter")  # math learn's extra: that block's
        await pilot.pause()
        assert app.screen.text.splitlines()[0] == "Mon 11/9 main: math · learn ×2"
        await pilot.press("escape", "left", "enter")  # Sunday 8th, before starts
        await settle(pilot, app)
        assert not isinstance(app.screen, Popup) and app.focused is week
        await pilot.press("right", "m", "down", "enter")
        await settle(pilot, app)
        assert app.view == "week" and app.focused is week and week.display
        assert app.cursor_date == date(2026, 11, 16)
        assert _controls(app) == "‹ Nov 16 – 22, week 47 ›"
        assert not app.query_one(MonthView).display


async def test_d_is_gone_and_w_m_switch(week_home):
    """The Week tab's top panel is the week or the month, on the same date; d does nothing,
    and in the chat d, w and m are typed."""
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        views = app.query_one("#views")
        assert str(views.content) == "week  month"
        await pilot.press("d")
        await settle(pilot, app)
        assert app.view == "week" and app.query_one(WeekView).display
        await pilot.press("right", "right", "m")
        await settle(pilot, app)
        assert app.view == "month" and app.cursor_date == SAT
        assert app.focused is app.query_one(MonthView) and _controls(app) == "‹ November 2026 ›"
        (active,) = [s for s in views.content.spans if s.style == "bold $accent"]
        assert views.content.plain[active.start : active.end] == "month"
        assert app.query_one(SoFar).display and app.query_one(DayPanel).display
        await pilot.press("d")
        await settle(pilot, app)
        assert app.view == "month"
        await pilot.press("w")
        await settle(pilot, app)
        assert app.view == "week" and app.cursor_date == SAT == app.query_one(WeekView).focus_date
        assert app.focused is app.query_one(WeekView)
        assert _controls(app) == "‹ Nov 9 – 15, week 46 ›"
        await pilot.press("m", "right_square_bracket", "w")
        await settle(pilot, app)
        assert app.cursor_date == date(2026, 12, 14) and app.board.monday == date(2026, 12, 14)
        await chat_keys(pilot, app, "d", "w", "m")  # typed in the chat, never a view's
        await pilot.pause()
        assert app.view == "week" and app.query_one("#chat-input").value == "dwm"


async def test_t_before_the_month_first_read_lands_stays_on_today(week_home):
    """t in the month shown for the first time, its read still in flight: today is the
    app's, so the cursor stays on it."""
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        release, real = threading.Event(), app._read

        def held():
            release.wait(5)
            return real()

        app._read = held
        try:
            await pilot.press("m", "t")
            await pilot.pause()
            assert app.cursor_date == THU
        finally:
            release.set()
        await settle(pilot, app)
        assert app.cursor_date == THU and _controls(app) == "‹ November 2026 ›"


async def test_month_while_the_data_is_unreadable(orrery_home):
    """The first read failed, so there's no date to move from: the keys do nothing, the app
    stays up, the line saying why shows in the month, and the panels under it stay hidden."""
    (orrery_home / "config.toml").write_text(settings.CONFIG_TEMPLATE)
    app = _app(orrery_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        await pilot.press("m")
        await settle(pilot, app)
        await pilot.press("left_square_bracket", "right_square_bracket", "t", "left", "down")
        await settle(pilot, app)
        v = app.query_one(MonthView)
        assert app.is_running and app.cursor_date is None and app.focused is v
        assert [row.strip() for row in _drawn(v)] == ["orrery: data unavailable (missing)"]
        assert not app.query_one("#lower").display


async def test_multi_line_notes_topics_and_deadlines_draw_on_one_line(week_home):
    """Mentors' text may hold newlines; the day panel draws each note, topic and deadline as
    one paragraph under its edge, and the month's weeks keep their rows."""
    with closing(db.open_existing(week_home)) as conn:
        _timed(conn, MON, "math", "learn", 1, "09:00", "10:30", note="line one\nline two\n")
        add_agenda(conn, "math", [
            channels.AgendaEntry(MON, "topic a\ntopic b", "learn"),
            channels.AgendaEntry(MON, "problem\nset 3", deadline=True),
        ])  # fmt: skip
    app = _app(week_home)
    async with app.run_test(size=BIG) as pilot:
        v = await _day(pilot, app, "left", "left", "left")
        drawn = _drawn(v)
        start = drawn.index("main   ▌math learn ●○")
        assert drawn[start : start + 7] == [
            "main   ▌math learn ●○",
            "       ▌ topic a topic b",
            "       ▌ 09:00–10:30  1 session",
            "       ▌ line one line two",
            "after  ▌geography ○",
            "       ▌ nothing reported",
            "due    ▌problem set 3",
        ]
        m = await _month(pilot, app)
        rows = _lines(m)
        assert [rows[_date_y(i)].split()[0] for i in range(6)] == ["26", "2", "9", "16", "23", "30"]
        assert _cell(m, _date_y(2) + 2, 0).startswith("topic a")


async def test_ansi_cursor_leaves_only_the_gap_even_with_wide_text(week_home):
    """In an ANSI theme the month cursor fills its cell up to the gap, counted in characters:
    a cell of wide text (two cells a character) is filled to its last character but the gap."""
    from orrery.tui.week import CURSOR

    app = _app(week_home)
    async with app.run_test(size=BIG) as pilot:
        v = await _month(pilot, app)
        app.theme = "ansi-dark"
        await pilot.pause()
        w = next(w for w in v.weeks if any(d.date == v.focus_date for d in w.days))
        i = [d.date for d in w.days].index(v.focus_date)
        wide = Content("数学" * 40)
        n = len(v._fit(wide).plain)  # characters a cell takes, its gap the last
        row = v._row(w, lambda d: wide)
        assert Span(i * n, i * n + n - 1, CURSOR) in row.spans, (n, row.spans)


@pytest.mark.parametrize("theme", THEMES)
async def test_today_is_a_chip_in_every_theme(week_home, theme):
    """As the week view's head: today's date is a chip, ` 12 `, its text apart from its fill
    and the fill from the calendar's background, ANSI themes included. With the cursor on
    today it sits on the cursor's background, its edges apart from it as the terminal draws
    them: an ANSI theme's cursor is drawn in its accent, the chip's fill, so a cell of the
    terminal's own background follows the chip there. With the cursor elsewhere, the chip
    alone, drawn the same."""
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        v = await _month(pilot, app)
        app.theme = theme
        await pilot.pause()
        y, x = _date_y(2), _cell_x(v, 3)  # Thu 12, in the third week row
        assert _cell(v, y, 3) == "12"
        row = _cells(v, y)
        chip, plain = row[x], row[_cell_x(v, 1)].bgcolor
        _assert_chip(chip, plain)
        assert {_look(s) for s in row[x : x + len(" 12 ")]} == {_look(chip)}
        assert row[x + len(" 12 ") + 1].bgcolor != plain  # the cursor's, around the chip
        fill = _ink(_shown(chip)[1])
        assert fill not in {_ink(row[x - 1].bgcolor), _ink(row[x + len(" 12 ")].bgcolor)}
        await pilot.press("left")  # the cursor on Wed 11, its cell's gap the chip's left edge
        await settle(pilot, app)
        row = _cells(v, y)
        assert _look(row[x]) == _look(chip) and row[x + len(" 12 ")].bgcolor == plain
        assert row[x - 2].bgcolor != plain and fill != _ink(row[x - 1].bgcolor)


async def test_no_lift_on_todays_column_or_cell(week_home):
    """Today's cell has no background of its own: the cursor's when the cursor is on it, else
    the calendar's, on every line of it, its chip aside."""
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        v = await _month(pilot, app)
        y0, x0 = _date_y(2), _cell_x(v, 3)
        plain = _bg(v, _cell_x(v, 1), y0)

        def fills() -> set:
            lines = [_cells(v, y)[x0 : x0 + v.col] for y in range(y0, y0 + LINES - 1)]
            lines[0] = lines[0][len(" 12 ") :]  # the chip's own
            return {s.bgcolor.triplet for line in lines for s in line}

        assert fills() == {_rgb(app, "accent-muted")}
        await pilot.press("left")
        await settle(pilot, app)
        assert fills() == {plain}
