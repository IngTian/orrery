"""The Report tab, through the Pilot: one completion heatmap with its subject strip over a
range (z) that ←/→ and [/] move, your stats for the range, and each mentor's newest weekly
evaluation, with enter listing them all."""

from contextlib import closing
from datetime import date, datetime, timedelta

import pytest
from helpers import report_and_collect, settle
from test_frame import _rgb, _screen_lines
from test_tui import _counting_reloads, _until
from textual.color import Color
from textual.geometry import Region

from orrery import channels, db, ledger, schedule, text
from orrery.model import Block
from orrery.tui import heatmap, report
from orrery.tui.app import OrreryApp
from orrery.tui.chat import ChatPanel
from orrery.tui.cursor import CursorView
from orrery.tui.heatmap import Heatmap
from orrery.tui.report import Evaluations, EvaluationsText, ReportView, YourStats
from orrery.tui.views import Popup
from orrery.tui.week import SoFar, WeekView

WIDE = (120, 40)
LATER = datetime(2026, 12, 10, 9, 0)  # a Thursday, the fifth week since orrery started on 11/9
TODAY = LATER.date()
W46, W47, W48, W49, W50 = (date(2026, 11, 9) + timedelta(weeks=i) for i in range(5))
MATH_DARK, FINANCE_DARK, GEOGRAPHY_DARK = "#3987e5", "#d95926", "#199e70"
STRIP = " all  ▌ math  ▌ finance  ▌ geography     ↑↓ subject"
AT = {"all": 1, "math": 8, "finance": 16, "geography": 27}  # a cell of each in the strip


def _app(home, now: datetime = LATER) -> OrreryApp:
    return OrreryApp(root=lambda: home, now=lambda: now)


async def _report(pilot, app) -> ReportView:
    await settle(pilot, app)
    await pilot.press("tab")
    await settle(pilot, app)
    v = app.query_one(ReportView)
    assert app.focused is v
    return v


def _lines(w) -> list[str]:
    return [w.render_line(y).text for y in range(w.size.height)]


def _drawn(w) -> list[str]:
    """The widget's printed rows, runs of spaces as one."""
    return [" ".join(row.split()) for row in _lines(w) if row.strip()]


def _style(w, x: int, y: int):
    at = 0
    for seg in w.render_line(y):
        if at <= x < at + seg.cell_length:
            return seg.style
        at += seg.cell_length
    raise AssertionError(f"nothing at {x}, {y}")


def _fg(w, x: int, y: int):
    return _style(w, x, y).color.triplet


def _find(w, s: str) -> tuple[int, int]:
    for y, row in enumerate(_lines(w)):
        if (x := row.find(s)) >= 0:
            return x, y
    raise AssertionError(f"{s!r} not drawn")


def _hex(h: str):
    return Color.parse(h).rgb


def _top(w) -> str:
    """A panel's top border as drawn: its title and caption."""
    return w.render_lines(Region(0, 0, *w.outer_size))[0].text


def _controls(app) -> str:
    return str(app.query_one("#controls").content)


def _views(app) -> tuple[str, str]:
    """The header's names, and the one in the accent."""
    c = app.query_one("#views").content
    (on,) = [s for s in c.spans if s.style == "bold $accent"]
    return c.plain, c.plain[on.start : on.end]


def _strip(app) -> str:
    return _lines(app.query_one("#subjects"))[0].rstrip()


def _cell(app, d: date) -> tuple[int, int]:
    """Where the heatmap draws d."""
    h = app.query_one(Heatmap)
    return heatmap.GUTTER + 2 * h.weeks.index(d - timedelta(days=d.weekday())), 1 + d.weekday()


def _shade(app, toward: str, k: int):
    h = app.query_one(Heatmap)
    return _hex(heatmap.ramp(toward, h.background_colors[1].hex, k))


def _starts(home, d: date) -> None:
    with closing(db.open_existing(home)) as conn:
        with ledger.change(conn, actor="orrery", kind="setup", now=LATER):
            conn.execute("update settings set value = ? where key = 'starts'", (str(d),))


def _did(conn, d: date, subject: str, kind: str, n: int, **kw) -> None:
    report_and_collect(conn, d, subject, kind, n, now=LATER, **kw)


def _assess(conn, subject: str, week: date, at: datetime, words: str = "") -> None:
    channels.assess(
        conn,
        channel=subject,
        week=week,
        strong=f"{subject} {week:%m/%d} strong{words}",
        shaky=f"{subject} {week:%m/%d} shaky",
        next=f"{subject} {week:%m/%d} next",
        today=at.date(),
        now=at,
    )


def _assessments(conn) -> None:
    _assess(conn, "math", W48, datetime(2026, 11, 30, 9, 0))
    _assess(conn, "math", W49, datetime(2026, 12, 8, 2, 0))  # before the boundary: Monday's
    _assess(conn, "finance", W49, datetime(2026, 12, 8, 10, 0))


def _five_weeks(conn) -> None:
    """Every block of W46's Mon to Wed (math learn one past its 2), W47 Monday's geography,
    W48 Tuesday's finance, and 2 math reps off the plan on W50's Tuesday."""
    for d, subject, kind, n in (
        (W46, "math", "rep", 1),
        (W46, "math", "learn", 3),
        (W46, "geography", "geography", 1),
        (W46 + timedelta(days=1), "finance", "learn", 2),
        (W46 + timedelta(days=1), "geography", "geography", 1),
        (W46 + timedelta(days=2), "math", "rep", 1),
        (W46 + timedelta(days=2), "finance", "rep", 1),
        (W46 + timedelta(days=2), "geography", "geography", 1),
        (W47, "geography", "geography", 1),
        (W48 + timedelta(days=1), "finance", "learn", 2),
        (W50 + timedelta(days=1), "math", "rep", 2),
    ):
        _did(conn, d, subject, kind, n)


async def test_z_cycles_ranges_and_window_never_passes_today_or_starts(week_home):
    """z goes month, quarter, half, year and round, for the whole tab; ←/→ and [/] move the
    window by its own length, never past this week, and never to start before starts' week:
    the earliest window starts there, unless the range is longer than orrery has run, when
    it ends with this week and draws the days before starts blank. t brings it back."""
    _starts(week_home, date(2026, 9, 7))  # W37: 14 weeks to this one
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        v = await _report(pilot, app)
        h = app.query_one(Heatmap)

        async def press(*keys: str) -> str:
            await pilot.press(*keys)
            await settle(pilot, app)
            return _controls(app)

        assert v.range == "month" and _views(app) == ("month  quarter  half  year", "month")
        assert _controls(app) == "‹ last 5 weeks, Nov 9 – Dec 13 ›" and h.weeks[0] == W46
        assert await press("right") == "‹ last 5 weeks, Nov 9 – Dec 13 ›"  # not past this week
        assert await press("right_square_bracket") == "‹ last 5 weeks, Nov 9 – Dec 13 ›"
        assert await press("left") == "‹ 5 weeks, Oct 5 – Nov 8 ›"
        assert await press("left_square_bracket") == "‹ 5 weeks, Sep 7 – Oct 11 ›"
        assert await press("left") == "‹ 5 weeks, Sep 7 – Oct 11 ›"  # starts' week first
        assert h.weeks == [date(2026, 9, 7) + timedelta(weeks=i) for i in range(5)]
        assert await press("right") == "‹ 5 weeks, Oct 12 – Nov 15 ›"
        assert await press("t") == "‹ last 5 weeks, Nov 9 – Dec 13 ›"
        assert await press("left", "z") == "‹ 13 weeks, Sep 7 – Dec 6 ›"
        assert v.range == "quarter" and _views(app)[1] == "quarter" and len(h.weeks) == 13
        half = "‹ last 26 weeks, Jun 15 – Dec 13 ›"  # longer than orrery has run
        assert await press("z") == half and _views(app)[1] == "half" and len(h.weeks) == 26
        x, y = _cell(app, date(2026, 9, 6))  # the Sunday before starts: blank
        assert h.render_line(y).text[x] == " "
        assert app.query_one(YourStats).caption() == "Sep 7 – Dec 13", "the stats' own days"
        for key in ("left", "right", "left_square_bracket", "right_square_bracket"):
            assert await press(key) == half, key
        assert await press("z") == "‹ last 53 weeks, Dec 8 2025 – Dec 13 2026 ›"
        assert _views(app)[1] == "year" and len(h.weeks) == 53
        assert await press("z") == "‹ last 5 weeks, Nov 9 – Dec 13 ›"
        assert v.range == "month" and app.report_week == W50
        assert app.cursor_date == TODAY, "the Week tab's cursor stayed put"
        await press("left", "tab")
        assert app.view == "week" and _controls(app) == "‹ Dec 7 – 13, week 50 ›"
        assert _views(app) == ("week  month", "week")
        assert await press("tab") == "‹ 5 weeks, Oct 5 – Nov 8 ›", "the window is kept"


async def test_up_down_cycle_the_strip_and_the_heatmap_takes_the_subject_hue(week_home):
    """The strip is all, then the subjects by slot; ↑/↓ cycle it and wrap, a click picks one,
    and the heatmap shades the picked one's days in its hue (all: toward the text colour)."""
    with closing(db.open_existing(week_home)) as conn:
        _did(conn, W50, "math", "learn", 1)  # Monday: math 1 of 3, all 1 of 4
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        v = await _report(pilot, app)
        assert _strip(app) == STRIP and v.subject == "all"
        strip = app.query_one("#subjects")
        cursor = _rgb(app, "accent-muted")
        assert _style(strip, AT["all"], 0).bgcolor.triplet == cursor
        assert _style(strip, AT["math"], 0).bgcolor.triplet != cursor
        assert _fg(strip, AT["math"] - 2, 0) == _hex(MATH_DARK)  # its edge
        h, monday = app.query_one(Heatmap), _cell(app, W50)
        assert _fg(h, *monday) == _shade(app, _rgb_hex(app, "foreground"), 1)
        await pilot.press("down")
        await pilot.pause()
        assert v.subject == "math" and _style(strip, AT["math"], 0).bgcolor.triplet == cursor
        assert _fg(h, *monday) == _shade(app, MATH_DARK, 2)
        await pilot.press("up", "up")  # past all, round to the last
        await pilot.pause()
        assert v.subject == "geography" and _lines(h)[1][monday[0]] == "□"
        await pilot.press("down")
        await pilot.pause()
        assert v.subject == "all"
        await pilot.click("#subjects", offset=(AT["finance"], 0))
        await pilot.pause()
        assert v.subject == "finance" and app.focused is v
        assert _lines(h)[1][monday[0]] == "·"  # no finance on Mondays
        await pilot.press("r")
        await settle(pilot, app)
        assert v.subject == "finance", "a reload keeps the pick"
        v.show(v.days, W50, 5, ["math"], {}, v.slots, v.starts, 5)  # finance left the strip
        await pilot.pause()
        assert v.subject == "all", "a pick that left the strip goes back to all"
        v.show(v.days, W50, 5, ["math", "geography"], {}, v.slots, v.starts, 5)
        await pilot.press("up")
        assert v.subject == "geography"
        days = v.days
        v.show("can't read")
        await pilot.press("up", "down", "down")  # no strip on screen: nothing to cycle
        assert v.subject == "geography" and _strip(app) == "can't read"
        v.show(days, W50, 5, ["math", "finance", "geography"], {}, v.slots, v.starts, 5)
        await pilot.pause()
        assert v.subject == "geography", "a read that couldn't be done loses no pick"
        assert _strip(app).startswith(" all  ▌ math  ▌ finance  ▌ geography")


def _rgb_hex(app, token: str) -> str:
    return app.get_css_variables()[token]


async def test_inactive_subject_with_planned_blocks_in_range_appears(week_home):
    """A subject no longer active, with blocks planned in the range, is in the strip and the
    stats in its own colour; one with none isn't. An active one with none is in the strip
    only."""
    with closing(db.open_existing(week_home)) as conn:
        _did(conn, W46 + timedelta(days=1), "finance", "learn", 1)
        with ledger.change(conn, actor="orrery", kind="setup", now=LATER):
            conn.execute("update subjects set active = 0 where name = 'finance'")
            conn.execute("insert into subjects (name, label, ord, active) values ('m', 'M', 3, 0)")
            conn.execute("insert into subjects (name, label, ord, active) values ('a', 'A', 4, 1)")
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await _report(pilot, app)
        assert _strip(app) == " all  ▌ math  ▌ finance  ▌ geography  ▌ a     ↑↓ subject"
        strip, stats = app.query_one("#subjects"), app.query_one(YourStats)
        assert _fg(strip, AT["finance"] - 2, 0) == _hex(FINANCE_DARK)
        assert _fg(strip, AT["geography"] - 2, 0) == _hex(GEOGRAPHY_DARK)  # its own slot
        rows = [row.split()[0] for row in _drawn(stats)[1:5]]
        assert rows == ["all", "▌math", "▌finance", "▌geography"]
        assert _fg(stats, *_find(stats, "▌finance")) == _hex(FINANCE_DARK)
        app.theme = "catppuccin-latte"
        await pilot.pause()
        await pilot.pause()
        assert _fg(strip, AT["finance"] - 2, 0) == _hex("#eb6834")
        assert _fg(stats, *_find(stats, "▌finance")) == _hex("#eb6834")


async def test_the_pick_stays_its_subject_as_an_inactive_one_comes_and_goes(week_home):
    """The strip's pick is a subject, not a place: when ←/→ or z bring an inactive subject
    ahead of it into the strip or take it out, the pick stays on the same subject."""
    _starts(week_home, date(2026, 9, 7))
    with closing(db.open_existing(week_home)) as conn:
        with ledger.change(conn, actor="orrery", kind="setup", now=LATER):
            schedule.pin(conn, W46, LATER)  # finance's blocks stay in W46 alone
            conn.execute("update subjects set active = 0 where name = 'finance'")
            kept = schedule.read_template(conn).items()
            schedule.write_template(
                conn, {d: [b for b in bs if b.subject != "finance"] for d, bs in kept}
            )
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        v = await _report(pilot, app)
        assert _strip(app) == STRIP
        await pilot.press("up")  # round to the last
        assert v.subject == "geography"
        for key, finance in (("left", False), ("right", True), ("left", False), ("z", True)):
            await pilot.press(key)
            await settle(pilot, app)
            assert ("finance" in v.strip) == finance, key
            assert v.subject == "geography" and _lines(app.query_one(Heatmap)), key
        assert _style(app.query_one("#subjects"), AT["geography"], 0).bgcolor.triplet == _rgb(
            app, "accent-muted"
        )


async def test_stats_table_and_sittings_best_week_this_week(week_home):
    """For the range: all and each subject's done of planned (capped per block), rate, study
    days, longest run of full ones and extras; then the sittings, the best week (ties to the
    latest) and this week so far. The heatmap counts the same way: extras never raise it."""
    with closing(db.open_existing(week_home)) as conn:
        _five_weeks(conn)
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await _report(pilot, app)
        stats = app.query_one(YourStats)
        assert _drawn(stats) == [
            "subject done rate study days longest run extras",
            "all 13 of 85 15% 28 3 days +3",
            "▌math 4 of 46 9% 23 2 days +3",
            "▌finance 5 of 15 33% 10 2 days +0",
            "▌geography 4 of 24 17% 24 3 days +0",
            "Sittings",
            "Sitting times show once 5 reports carry start and end times (5 more to go).",
            "Best week: W46 (Nov 9 – 15), 10 of 18 planned sessions done.",
            "This week",
            "0 of 13 planned, Mon to Thu",
        ]
        assert _top(stats).startswith("╭─ Your stats ─") and "Nov 9 – Dec 13 ─╮" in _top(stats)
        assert _fg(stats, *_find(stats, "13 of 85")) == _rgb(app, "foreground")
        h, fg = app.query_one(Heatmap), _rgb_hex(app, "foreground")
        assert _fg(h, *_cell(app, W46)) == _shade(app, fg, 4)  # its surplus learn changes nothing
        assert _fg(h, *_cell(app, W47)) == _shade(app, fg, 1)
        x, y = _cell(app, W50 + timedelta(days=1))
        assert _lines(h)[y][x] == "□"  # the math reps off the plan don't count
        assert _top(app.query_one(ReportView)).startswith("╭─ Completion ─")


def _timed(conn, d: date, subject: str, kind: str, n: int, start: str, end: str) -> None:
    _did(conn, d, subject, kind, n, started_at=f"{d}T{start}:00", ended_at=f"{d}T{end}:00")


@pytest.mark.parametrize(
    ("minutes", "says"), [(45, "45 min"), (60, "1 h"), (80, "1 h 20"), (125, "2 h 05")]
)
def test_a_sitting_length_reads_in_hours_and_minutes(minutes, says):
    assert report._length(minutes) == says


async def test_stats_sittings_once_enough_reports_carry_times(week_home):
    """Short of 5 timed reports, Sittings says how many more; from the fifth, when half of
    them start and how long the median one runs. An untimed report doesn't count."""
    with closing(db.open_existing(week_home)) as conn:
        _timed(conn, W50, "math", "rep", 1, "09:00", "10:20")
        _timed(conn, W50, "math", "learn", 2, "10:00", "11:20")
        _timed(conn, W50, "geography", "geography", 1, "11:00", "12:20")
        _timed(conn, W50 + timedelta(days=1), "finance", "learn", 2, "09:00", "10:20")
        _did(conn, W50 + timedelta(days=1), "geography", "geography", 1)  # untimed
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await _report(pilot, app)
        stats = app.query_one(YourStats)
        drawn = _drawn(stats)
        at = drawn.index("Sittings")
        assert drawn[at + 1] == (
            "Sitting times show once 5 reports carry start and end times (1 more to go)."
        )
        with closing(db.open_existing(week_home)) as conn:
            _timed(conn, W50 + timedelta(days=2), "math", "rep", 1, "10:00", "11:20")
        await pilot.press("r")
        await settle(pilot, app)
        drawn = _drawn(stats)
        assert drawn[at + 1] == (
            "Half the sittings start between 09:00 and 10:00; the median one runs 1 h 20."
        )
        assert _fg(stats, *_find(stats, "Half the sittings")) == _rgb(app, "foreground")


async def test_evaluations_newest_per_subject_and_enter_lists_the_selected_subject(week_home):
    """Each subject's newest evaluation up to the window's end, in its mentor's words, wrapped
    in full; enter lists every week's for the subject picked in the strip (all: each one)."""
    _starts(week_home, date(2026, 9, 7))
    with closing(db.open_existing(week_home)) as conn:
        _assessments(conn)
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        v = await _report(pilot, app)
        said = app.query_one(EvaluationsText)
        assert _drawn(said) == [
            "▌math week of Nov 30, written Mon Dec 7",
            "▌ strong math 11/30 strong",
            "▌ shaky math 11/30 shaky",
            "▌ next math 11/30 next",
            "▌finance week of Nov 30, written Tue Dec 8",
            "▌ strong finance 11/30 strong",
            "▌ shaky finance 11/30 shaky",
            "▌ next finance 11/30 next",
        ]
        panel = app.query_one(Evaluations)
        assert _top(panel).startswith("╭─ Mentor evaluations ─")
        x, y = _find(said, "▌ strong  math")
        assert _fg(said, x, y) == _hex(MATH_DARK)
        assert _fg(said, _lines(said)[y].index("math 11/30"), y) == _rgb(app, "foreground")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, Popup)
        assert app.screen.text.splitlines() == [
            "math: every weekly evaluation, in its mentor's words",
            "",
            "week of Nov 30, written Mon Dec 7",
            "strong  math 11/30 strong",
            "shaky   math 11/30 shaky",
            "next    math 11/30 next",
            "",
            "week of Nov 23, written Mon Nov 30",
            "strong  math 11/23 strong",
            "shaky   math 11/23 shaky",
            "next    math 11/23 next",
            "",
            "finance: every weekly evaluation, in its mentor's words",
            "",
            "week of Nov 30, written Tue Dec 8",
            "strong  finance 11/30 strong",
            "shaky   finance 11/30 shaky",
            "next    finance 11/30 next",
        ]
        await pilot.press("escape", "down", "enter")
        await pilot.pause()
        assert (
            app.screen.text.splitlines()[0]
            == "math: every weekly evaluation, in its mentor's words"
        )
        assert len(app.screen.text.splitlines()) == 11
        await pilot.press("escape", "down", "down", "enter")
        await pilot.pause()
        assert app.screen.text.splitlines() == [
            "geography: every weekly evaluation, in its mentor's words",
            "",
            "no evaluations yet",
        ]
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is v
        await pilot.press("left")  # the window ending Nov 8: none yet
        await settle(pilot, app)
        assert _drawn(said) == ["no evaluations yet"]
        await pilot.press("t")
        await settle(pilot, app)
        long = "a long sentence of the mentor's, " * 6
        with closing(db.open_existing(week_home)) as conn:
            _assess(conn, "geography", W49, LATER, words=long)
        await pilot.press("r")
        await settle(pilot, app)
        rows = [row.rstrip() for row in _lines(said)]
        at = next(i for i, row in enumerate(rows) if row.startswith("▌ strong  geography"))
        end = next(i for i, row in enumerate(rows) if row.startswith("▌ shaky   geography"))
        assert end - at > 2 and all(row.startswith("▌" + " " * 9) for row in rows[at + 1 : end])
        assert " ".join(row[10:] for row in rows[at:end]).split() == (
            f"geography 11/30 strong{long}".split()
        )


@pytest.mark.parametrize("size", [(235, 60), (160, 45), (159, 45)])
async def test_report_panels_side_by_side_at_160_and_stacked_below(week_home, size):
    """From 160 columns: the heatmap on top, and under it your stats beside the evaluations,
    to the window's bottom. Narrower, the three stack in one column."""
    app = _app(week_home)
    async with app.run_test(size=size) as pilot:
        await _report(pilot, app)
        top, stats, said = (app.query_one(w).region for w in (ReportView, YourStats, Evaluations))
        chat = app.query_one(ChatPanel).region
        if size[0] >= 160:
            assert top.x == stats.x and top.right == said.right == chat.x
            assert top.bottom <= stats.y == said.y and stats.right <= said.x
            assert stats.bottom == said.bottom == size[1] - 3  # over the key line's 3 rows
        else:
            assert top.x == stats.x == said.x and top.width == stats.width == said.width
            assert top.bottom <= stats.y < stats.bottom <= said.y


@pytest.mark.parametrize("size", [(235, 60), (160, 45), (120, 40)])
async def test_report_no_required_text_cut(week_home, size):
    """No row of the strip, the heatmap or your stats ends in an ellipsis, and each one's
    required text is drawn in full."""
    with closing(db.open_existing(week_home)) as conn:
        _five_weeks(conn)
        _assessments(conn)
    app = _app(week_home)
    async with app.run_test(size=size) as pilot:
        await _report(pilot, app)
        required = {
            "#subjects": [STRIP.strip()],
            Heatmap: ["Mon", "Sun", "less ■ ■ ■ ■ more", "□ planned, none done"],
            YourStats: ["longest run  extras", "▌geography  4 of 24", "17%", "3 days", "+0"]
            + ["Best week: W46", "0 of 13 planned, Mon to Thu"],
            EvaluationsText: ["▌finance", "finance 11/30 next"],
        }
        for w, texts in required.items():
            rows = [row.rstrip() for row in _lines(app.query_one(w))]
            assert rows and not any("…" in row for row in rows), (w, rows)
            for t in texts:
                assert any(t in row for row in rows), (w, t, rows)


@pytest.mark.parametrize("size", [(235, 60), (160, 45)])
async def test_stats_table_takes_the_width_a_long_subject_name_needs(week_home, size):
    """A 12-letter subject with two-digit counts cuts no column of the stats table."""
    with closing(db.open_existing(week_home)) as conn:
        with ledger.change(conn, actor="orrery", kind="setup", now=LATER):
            conn.execute(
                "insert into subjects (name, label, ord, active) values ('trigonometry', 'T', 3, 1)"
            )
            monday = schedule.read_template(conn)[0]
            schedule.write_template(conn, {0: (*monday, Block("main", "trigonometry", "learn", 1))})
        _five_weeks(conn)
    app = _app(week_home)
    async with app.run_test(size=size) as pilot:
        await _report(pilot, app)
        rows = [row.rstrip() for row in _lines(app.query_one(YourStats))]
        assert not any("…" in row for row in rows), rows
        assert rows[0].endswith("longest run  extras"), rows
        assert any(row.startswith("▌trigonometry") and row.endswith("+0") for row in rows), rows


async def test_stats_table_takes_its_width_only_side_by_side(week_home):
    """Stacked (below 160 columns), Your stats is the pane's width and never wider; from 160
    it takes what the table needs, and lets go again when the window narrows."""
    with closing(db.open_existing(week_home)) as conn:
        with ledger.change(conn, actor="orrery", kind="setup", now=LATER):
            conn.execute(
                "insert into subjects (name, label, ord, active) values ('trigonometry', 'T', 3, 1)"
            )
            monday = schedule.read_template(conn)[0]
            schedule.write_template(conn, {0: (*monday, Block("main", "trigonometry", "learn", 1))})
        _five_weeks(conn)
    app = _app(week_home)
    async with app.run_test(size=(235, 60)) as pilot:
        await _report(pilot, app)
        s = app.query_one(YourStats)
        assert s.styles.min_width is not None
        for size, side_by_side in (((90, 40), False), ((235, 60), True)):
            await pilot.resize_terminal(*size)
            await settle(pilot, app)
            assert (s.styles.min_width is not None) == side_by_side, size
            if not side_by_side:
                pane = s.parent.parent if s.parent.id == "report-lower" else s.parent
                assert s.region.right <= pane.content_region.right, (size, s.region, pane)


def test_dates_of_one_day_name_it_once():
    assert report.dates(date(2026, 11, 15), date(2026, 11, 9)) == "Nov 15"
    assert report.dates(date(2026, 11, 11), date(2026, 11, 9)) == "Nov 11 – 15"


async def test_report_text_passes_verdict_gate(week_home):
    """orrery's own text states facts; the mentor's words are shown as theirs, verdict or not."""
    with closing(db.open_existing(week_home)) as conn:
        _five_weeks(conn)
        _did(conn, W47, "math", "rep", 1, started_at=f"{W47}T22:00:00", ended_at=f"{W47}T23:40:00")
        channels.assess(
            conn,
            channel="math",
            week=W49,
            strong="proofs",
            shaky="fell behind on the problem sets",
            next="catch up on what was missed",
            today=TODAY,
            now=LATER,
        )
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        v = await _report(pilot, app)
        mentor = {"▌ shaky fell behind on the problem sets", "▌ next catch up on what was missed"}

        def ours() -> list[str]:
            widgets = ("#subjects", Heatmap, YourStats, EvaluationsText)
            rows = [row for w in widgets for row in _drawn(app.query_one(w))]
            panels = (ReportView, YourStats, Evaluations)
            return [r for r in rows if r not in mentor] + [_top(app.query_one(p)) for p in panels]

        assert mentor <= set(_drawn(app.query_one(EvaluationsText)))
        assert not any(text.has_verdict(row) for row in ours())
        assert not text.has_verdict(_controls(app))
        await pilot.press("enter")
        await pilot.pause()
        said = app.screen.text.splitlines()
        assert not any(text.has_verdict(row) for row in said if not row.startswith(("sh", "ne")))
        await pilot.press("escape")
        for key in ("z", "down", "down"):
            await pilot.press(key)
            await settle(pilot, app)
            assert not any(text.has_verdict(row) for row in ours()), key
        assert v.subject == "finance"


async def test_header_controls_follow_the_open_tab(week_home):
    """The Week tab's views and its view's period; the Report's ranges and its window: there
    d, w, m and a Week view's Switch change nothing."""
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        await settle(pilot, app)
        assert _views(app) == ("week  month", "week")
        assert _controls(app) == "‹ Dec 7 – 13, week 50 ›"
        v = await _report(pilot, app)
        assert _views(app) == ("month  quarter  half  year", "month")
        assert _controls(app) == "‹ last 5 weeks, Nov 9 – Dec 13 ›"
        await pilot.press("d", "w", "m")
        app.query_one(WeekView).post_message(CursorView.Switch("month"))
        await settle(pilot, app)
        assert app.view == "week" and app.focused is v
        assert _controls(app) == "‹ last 5 weeks, Nov 9 – Dec 13 ›"
        await pilot.press("tab")
        await settle(pilot, app)
        await pilot.press("m", "z")  # z: the Report's, nothing here
        await settle(pilot, app)
        assert app.view == "month" and _controls(app) == "‹ December 2026 ›"
        assert _views(app) == ("week  month", "month")
        await pilot.press("tab")
        await settle(pilot, app)
        assert _views(app)[1] == "month" and _controls(app) == "‹ last 5 weeks, Nov 9 – Dec 13 ›"


@pytest.mark.parametrize("now", [datetime(2026, 11, 12, 9, 0), datetime(2027, 1, 7, 9, 0)])
async def test_report_header_keeps_the_tabs_and_the_window_at_80x24(week_home, now):
    """Below 110 columns the Report's ranges go from the header, as before: the tabs and the
    window's whole heading stay in every range, its weeks naming the range. From 110 they're
    back."""
    _starts(week_home, date(2025, 9, 1))  # every range's window ends with this week
    app = _app(week_home, now)
    async with app.run_test(size=(80, 24)) as pilot:
        v = await _report(pilot, app)
        for weeks in (5, 13, 26, 53):
            row = _screen_lines(app)[0]
            assert "Week  Report" in row and v.heading in row, row
            assert row.rstrip().endswith("›") and f"last {weeks} weeks" in row, row
            await pilot.press("z")
            await settle(pilot, app)
        await pilot.resize_terminal(*WIDE)
        await _until(pilot, lambda: "month  quarter  half  year" in _screen_lines(app)[0])
        await pilot.resize_terminal(80, 24)
        await _until(pilot, lambda: "quarter" not in _screen_lines(app)[0])


@pytest.mark.parametrize(
    ("starts", "says"),
    [
        (date(2026, 12, 11), "orrery starts Fri 12/11"),  # later this week
        (date(2026, 12, 14), "orrery starts Mon 12/14"),  # next week
    ],
)
async def test_report_before_orrery_starts(week_home, starts, says):
    """starts after today: the header and the heatmap say when, the stats count nothing, and
    no key moves the window into the future or before starts' week."""
    _starts(week_home, starts)
    app = _app(week_home)
    async with app.run_test(size=WIDE) as pilot:
        v = await _report(pilot, app)
        assert _controls(app) == says and app.report_week == W50
        for key in ("left", "right", "left_square_bracket", "right_square_bracket", "t", "z"):
            await pilot.press(key)
            await settle(pilot, app)
            assert _controls(app) == says and app.report_week == W50 == v.focus_date, key
        assert _drawn(app.query_one(Heatmap)) == [says]
        assert "nothing planned in these weeks" in _drawn(app.query_one(YourStats))
        rows = [row for w in ("#subjects", Heatmap, YourStats) for row in _drawn(app.query_one(w))]
        assert not any(text.has_verdict(row) for row in rows)
        assert app.query_one(SoFar).caption() == says  # the Week tab's, hidden here


async def test_report_window_moves_to_starts_week_when_it_comes(week_home):
    """starts next Monday: past the Sunday-to-Monday boundary the minute's read moves the
    window to starts' week, once, and the header names the 5 weeks ending with it."""
    _starts(week_home, date(2026, 12, 14))
    clock = [datetime(2026, 12, 13, 23, 0)]  # Sunday
    app = OrreryApp(root=lambda: week_home, now=lambda: clock[0])
    async with app.run_test(size=WIDE) as pilot:
        v = await _report(pilot, app)
        assert _controls(app) == "orrery starts Mon 12/14" and app.report_week == W50
        reloads = _counting_reloads(app)
        clock[0] = datetime(2026, 12, 14, 5, 0)
        app._check_day()
        await settle(pilot, app)
        assert _controls(app) == "‹ last 5 weeks, Nov 16 – Dec 20 ›"
        assert app.report_week == date(2026, 12, 14) == v.focus_date and reloads == [1]


@pytest.mark.parametrize(
    ("starts", "days"), [(date(2026, 11, 9), "Nov 9 – 15"), (date(2026, 11, 11), "Nov 11 – 15")]
)
async def test_report_in_orrery_first_week_draws_the_whole_month(week_home, starts, days):
    """In starts' week the heatmap still draws the month's 5 weeks; the stats say their own
    days, from starts."""
    _starts(week_home, starts)
    app = _app(week_home, datetime(2026, 11, 12, 9, 0))  # starts' week
    async with app.run_test(size=WIDE) as pilot:
        await _report(pilot, app)
        assert _controls(app) == "‹ last 5 weeks, Oct 12 – Nov 15 ›"
        assert len(app.query_one(Heatmap).weeks) == 5
        assert app.query_one(YourStats).caption() == days


async def test_report_window_across_new_year_names_the_years(week_home):
    """A window whose ends are in different years says so."""
    _starts(week_home, date(2026, 9, 7))
    app = _app(week_home, datetime(2027, 1, 7, 9, 0))
    async with app.run_test(size=WIDE) as pilot:
        await _report(pilot, app)
        assert _controls(app) == "‹ last 5 weeks, Dec 7 2026 – Jan 10 2027 ›"
        await pilot.press("z")
        await settle(pilot, app)
        assert _controls(app) == "‹ last 13 weeks, Oct 12 2026 – Jan 10 2027 ›"
        await _until(pilot, lambda: "Jan" in _lines(app.query_one(Heatmap))[0])
