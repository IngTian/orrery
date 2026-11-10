"""The board on screen, through the Pilot: the timetable, the waiting proposal's diff, y, n
and u, ?, f, r and the day timer."""

import contextlib
import threading
from contextlib import closing
from datetime import datetime, timedelta

import pytest
from helpers import (
    NOW,
    TODAY,
    add_agenda_at,
    keep_move_at,
    propose_move,
    settle,
    state_of,
    store_waiting,
)
from textual.screen import ModalScreen
from textual.widgets import Static

from orrery import board, channels, db, proposals, settings
from orrery.__main__ import main
from orrery.tui import keymap, views, week
from orrery.tui.app import OrreryApp
from orrery.tui.chat import summary
from orrery.tui.week import WeekView

SIZE = (120, 40)  # the timetable beside the docked chat
THU = TODAY
MON, FRI = THU - timedelta(days=3), THU + timedelta(days=1)
FRI_6AM = datetime(2026, 11, 13, 6, 0)  # past the boundary: Thursday is a past day
TITLE = "‹ Nov 9 – 15, week 46 ›"


def _app(home, now: datetime = NOW) -> OrreryApp:
    return OrreryApp(root=lambda: home, now=lambda: now)


def _text(app: OrreryApp, selector: str) -> str:
    return str(app.screen.query_one(selector, Static).content)


def _talk(app: OrreryApp) -> list[str]:
    """The chat's rows as shown, oldest first, then the turn in progress: each on one line,
    as the strip says it."""
    return [summary(w.line, w.live) for w in app.query("#log > .row") if w.display]


def _key_line(tab: str) -> str:
    """The tab's key line at SIZE, inside the footer's padding."""
    return "\n".join(r.plain for r in keymap.key_rows(tab, SIZE[0] - 2))


def _rows(app: OrreryApp) -> list[str]:
    """The timetable's rows as drawn (or the line in its place)."""
    view = app.query_one(WeekView)
    return [view.render_line(y).text for y in range(view.size.height)]


def _week(app: OrreryApp) -> str:
    return "\n".join(row.rstrip() for row in _rows(app))


def _column(app: OrreryApp, i: int) -> list[str]:
    """Day i's column, a line per row, stripped; the header first."""
    col = app.query_one(WeekView).col
    x = week.GUTTER + i * col
    return [row[x : x + col].strip() for row in _rows(app)]


async def _until(pilot, condition) -> None:
    """Pause until condition() holds: a thread worker's result lands a few ticks after it."""
    for _ in range(500):
        if condition():
            return
        await pilot.pause(0.01)
    raise AssertionError("condition never held")


def _counting_reloads(app: OrreryApp) -> list[int]:
    count = [0]
    real = app.app_reload

    def reload() -> None:
        count[0] += 1
        real()

    app.app_reload = reload
    return count


@contextlib.contextmanager
def _recording_suspend(events: list[str]):
    events.append("suspend")
    yield
    events.append("resume")  # reached only if the body exits normally, as Textual's does


async def test_week_header_footer_and_a_moved_block(week_home):
    keep_move_at(week_home, THU, "math", "learn", FRI)
    app = OrreryApp(root=lambda: week_home, now=lambda: NOW)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        assert "▌→ Fri 13" in _column(app, 3)
        assert _text(app, "#controls") == TITLE
        assert _text(app, "#footer") == _key_line("week")
        assert not app.query_one("#proposal").display  # nothing waits
        await pilot.press("tab")  # the key line follows the tab
        await settle(pilot, app)
        assert _text(app, "#footer") == _key_line("report")
        await pilot.press("tab")
        await settle(pilot, app)
        assert _text(app, "#footer") == _key_line("week")


async def test_unavailable_db(orrery_home):
    (orrery_home / "config.toml").write_text(settings.CONFIG_TEMPLATE)
    line = "orrery: data unavailable (missing)"
    app = _app(orrery_home)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        assert _week(app) == line
        assert not app.query_one("#lower").display
        await pilot.press("u")
        await settle(pilot, app)
        assert _text(app, "#note") == line and app.is_running
    assert not (orrery_home / "orrery.db").exists()


async def test_a_failing_reload_shows_one_line(week_home, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(board, "build", broken)
    app = _app(week_home)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        assert _week(app) == "orrery: data unavailable (synthetic failure)"
        assert app.is_running


async def test_y_keeps_shown_proposal(week_home):
    with closing(db.open_existing(week_home)) as conn:
        p = propose_move(conn)
    app = _app(week_home)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        diff = [line.rstrip() for line in _text(app, "#proposal").splitlines()]
        assert diff == [
            f"proposal #{p.id} · organizer",
            "− Thu 11/12  math · learn ×2, geography ×1",
            "+ Thu 11/12  math · learn ×2, geography ×1",
            "− Fri 11/13  math · learn ×2, geography ×1",
            "+ Fri 11/13  math · learn ×4, geography ×1",
            "y keep · n discard",
        ]
        spans = app.query_one("#proposal", Static).content.spans
        assert "strike" in {str(s.style) for s in spans}
        assert "→ Fri" not in _week(app)
        await pilot.press("y")
        await settle(pilot, app)
        assert not app.query_one("#proposal").display and "→ Fri 13" in _week(app)
        assert _talk(app)[-1] == f"orrery: kept proposal #{p.id}"
        assert not app.query_one("#note").display, "the chat says it: no note repeats it"
    with closing(db.open_existing(week_home)) as conn:
        assert proposals.waiting(conn) is None and state_of(conn, p.id) == "kept"


async def test_a_second_y_before_the_reload_does_nothing(week_home, monkeypatch):
    """One y per diff shown: a second y while the first keep is still running keeps nothing."""
    with closing(db.open_existing(week_home)) as conn:
        p = propose_move(conn)
    gate, calls, real = threading.Event(), [], proposals.keep

    def keep(conn, pid, **kwargs):
        calls.append(pid)
        gate.wait(5)
        return real(conn, pid, **kwargs)

    monkeypatch.setattr(proposals, "keep", keep)
    app = _app(week_home)
    try:
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("y")
            await _until(pilot, lambda: calls)
            await pilot.press("y")
            gate.set()
            await settle(pilot, app)
            assert calls == [p.id] and _talk(app).count(f"orrery: kept proposal #{p.id}") == 1
    finally:
        gate.set()


async def test_y_keeps_only_what_was_shown(week_home):
    with closing(db.open_existing(week_home)) as conn:
        first = propose_move(conn)
    app = _app(week_home)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        assert _text(app, "#proposal").startswith(f"proposal #{first.id} · ")
        with closing(db.open_existing(week_home)) as conn:
            second = propose_move(conn, rationale="the same move, asked again", replaces=first.id)
        await pilot.press("y")
        await settle(pilot, app)
        assert _text(app, "#note") == proposals.STALE
        # shown now, for its own y
        assert _text(app, "#proposal").startswith(f"proposal #{second.id} · organizer\n")
        assert "→ Fri" not in _week(app)
    with closing(db.open_existing(week_home)) as conn:
        assert state_of(conn, second.id) == "waiting" and state_of(conn, first.id) == "replaced"


async def test_n_discards(week_home):
    with closing(db.open_existing(week_home)) as conn:
        p = propose_move(conn)
    app = _app(week_home)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await pilot.press("n")
        await settle(pilot, app)
        said = [f"orrery: discarded proposal #{p.id}; its requests stay pending"]
        assert _talk(app)[-1:] == said and not app.query_one("#note").display
        await pilot.press("y", "n")  # nothing is shown now: neither key does anything
        await settle(pilot, app)
        assert _talk(app)[-1:] == said and not app.query_one("#note").display
        assert "→ Fri" not in _week(app)
    with closing(db.open_existing(week_home)) as conn:
        assert state_of(conn, p.id) == "discarded"


async def test_u_undoes(week_home):
    keep_move_at(week_home, THU, "math", "learn", FRI)
    app = _app(week_home)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        assert "→ Fri 13" in _week(app)
        await pilot.press("u")
        await settle(pilot, app)
        assert _talk(app)[-1] == "orrery: undid the last kept proposal"
        assert not app.query_one("#note").display
        assert "→ Fri" not in _week(app)
        assert [x for x in _column(app, 3) if x][1:3] == ["▌math", "▌learn ○○"]
        await pilot.press("u")
        await settle(pilot, app)
        assert _text(app, "#note") == "nothing to undo"  # no chat line: the note says it
        await pilot.press("r")  # a key's line lasts until the next key
        await settle(pilot, app)
        assert not app.query_one("#note").display


async def test_u_says_why_it_cant(week_home):
    keep_move_at(week_home, THU, "math", "learn", FRI)
    app = _app(week_home, FRI_6AM)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await pilot.press("u")
        await settle(pilot, app)
        assert _text(app, "#note") == "past days keep their plan"
        assert "→ Fri 13" in _week(app)


async def test_reload_runs_collect(week_home):
    app = _app(week_home)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        assert "▌learn ○○" in _column(app, 0)
        with closing(db.open_existing(week_home)) as conn:
            channels.report(
                conn,
                channel="math",
                date=MON,
                kind="learn",
                sessions=1,
                status="partial",
                note="",
                key="mon-learn",
                now=NOW,
                boundary_hour=5,
            )
        await pilot.press("r")
        await settle(pilot, app)
        assert "▌learn ◐○" in _column(app, 0)


EXPIRED = (
    "proposal #{} can't apply any more: {}; its requests stay pending — ask the organizer for a"
    " new one"
)


def _expired(app: OrreryApp, home, pid: int) -> None:
    """pid expired, said once in the chat (no note repeats it), and the panel gone."""
    line = EXPIRED.format(pid, "past days keep their plan")
    assert not app.query_one("#note").display
    assert not app.query_one("#proposal").display and app.shown_proposal is None
    assert _talk(app).count(f"orrery: {line}") == 1
    with closing(db.open_existing(home)) as conn:
        assert state_of(conn, pid) == "discarded"


async def test_a_proposal_whose_day_passed_expires_when_the_app_opens(week_home):
    with closing(db.open_existing(week_home)) as conn:
        pid = propose_move(conn).id  # on Thursday, about Thursday
    app = _app(week_home, FRI_6AM)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        assert app.day == FRI
        _expired(app, week_home, pid)
        await pilot.press("y", "r")  # nothing shown: y does nothing; a reload says no more
        await settle(pilot, app)
        assert not app.query_one("#note").display
        talk = _talk(app)
        assert talk[-1].startswith(f"orrery: proposal #{pid} can't apply any more")
        assert sum("can't apply any more" in r for r in talk) == 1


async def test_the_day_change_expires_the_waiting_proposal(week_home, monkeypatch):
    """The minute check after midnight and the boundary: shown before, expired after."""
    clock = [datetime(2026, 11, 12, 23, 0)]
    timers, real = [], OrreryApp.set_interval

    def spy(self, interval, callback=None, **kwargs):
        timers.append((interval, callback))
        return real(self, interval, callback, **kwargs)

    monkeypatch.setattr(OrreryApp, "set_interval", spy)
    with closing(db.open_existing(week_home)) as conn:
        pid = propose_move(conn).id
    app = OrreryApp(root=lambda: week_home, now=lambda: clock[0])
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        assert app.shown_proposal == pid and app.query_one("#proposal").display
        (check,) = [callback for interval, callback in timers if interval == 60]
        clock[0] = datetime(2026, 11, 13, 0, 30)  # past midnight, still Thursday's day
        check()
        await settle(pilot, app)
        assert app.shown_proposal == pid
        clock[0] = FRI_6AM
        check()
        await settle(pilot, app)
        _expired(app, week_home, pid)


async def test_a_failed_y_expires_it_at_once(week_home):
    """y after the boundary, before the minute check has noticed: it expires there and then."""
    clock = [NOW]
    with closing(db.open_existing(week_home)) as conn:
        pid = propose_move(conn).id
    app = OrreryApp(root=lambda: week_home, now=lambda: clock[0])
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        assert app.shown_proposal == pid
        clock[0] = FRI_6AM
        await pilot.press("y")
        await settle(pilot, app)
        _expired(app, week_home, pid)


async def test_an_expiry_overtaken_by_a_newer_reload_still_shows(week_home, monkeypatch):
    """The read that expired it is overtaken (w, m, a Shift across weeks) and never drawn; the
    newer one finds nothing waiting, and shows the chat's line saying it expired."""
    with closing(db.open_existing(week_home)) as conn:
        pid = propose_move(conn).id
    held, release, real = threading.Event(), threading.Event(), proposals.expire

    def expire(conn, **kwargs):
        line = real(conn, **kwargs)
        if line:  # committed: this read waits until a newer one has replaced it
            held.set()
            release.wait(5)
        return line

    monkeypatch.setattr(proposals, "expire", expire)
    line, app = EXPIRED.format(pid, "past days keep their plan"), _app(week_home, FRI_6AM)
    try:
        async with app.run_test(size=SIZE) as pilot:
            await _until(pilot, held.is_set)
            app.app_reload()  # cancels the held one, exclusive in "collect"
            await _until(pilot, lambda: f"orrery: {line}" in _talk(app))  # the newer one drew
            release.set()
            await settle(pilot, app)
            _expired(app, week_home, pid)
    finally:
        release.set()


MOVE_0 = {"op": "move", "date": "2026-11-12", "subject": "math", "kind": "learn", "sessions": 0}


@pytest.mark.parametrize(
    ("op", "line"),
    [
        ({**MOVE_0, "to": "2026-11-14"}, EXPIRED.format(1, "sessions must be at least 1")),
        ({"op": "bogus"}, EXPIRED.format(1, "unknown op 'bogus'")),
    ],
)
async def test_a_bad_stored_proposal_leaves_the_board_up(week_home, op, line):
    """One keep would refuse expires, and the chat says why."""
    with closing(db.open_existing(week_home)) as conn:
        store_waiting(conn, [op])
    app = _app(week_home)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        assert [x for x in _column(app, 3) if x][1:3] == ["▌math", "▌learn ○○"]
        assert _talk(app)[-1] == f"orrery: {line}" and not app.query_one("#proposal").display


async def test_markup_and_long_text_render_literally(week_home):
    add_agenda_at(
        week_home, "math", [channels.AgendaEntry(THU, "[bold]x[/bold] " + "y" * 400, "learn")]
    )
    app = OrreryApp(root=lambda: week_home, now=lambda: NOW)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        assert "▌[bold]x[…" in _column(app, 3)  # as typed, cut to the column
        width = app.query_one(WeekView).size.width
        assert all(len(row) <= width for row in _rows(app))


async def test_day_change_reloads(week_home, monkeypatch):
    """A 60 s timer reloads once the day boundary (5:00) passes, and only then."""
    clock = [datetime(2026, 11, 12, 23, 0)]
    timers = []
    real = OrreryApp.set_interval

    def spy(self, interval, callback=None, **kwargs):
        timers.append((interval, callback))
        return real(self, interval, callback, **kwargs)

    monkeypatch.setattr(OrreryApp, "set_interval", spy)
    app = OrreryApp(root=lambda: week_home, now=lambda: clock[0])
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        (check,) = [callback for interval, callback in timers if interval == 60]
        reloads = _counting_reloads(app)
        clock[0] = datetime(2026, 11, 13, 4, 59)  # still Thursday's day
        check()
        await settle(pilot, app)
        assert reloads[0] == 0 and app.day == THU
        clock[0] = datetime(2026, 11, 13, 5, 0)
        check()
        await settle(pilot, app)
        assert reloads[0] == 1 and app.day == FRI


async def test_question_mark_shows_the_keys(week_home):
    keep_move_at(week_home, THU, "math", "learn", FRI)
    app = _app(week_home)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, ModalScreen)
        assert _text(app, "#popup-text") == keymap.help_text()
        await pilot.press("question_mark", "u", "M")  # the week's keys are off meanwhile
        await settle(pilot, app)
        assert len(app.screen_stack) == 2 and _text(app, "#popup-text") == keymap.help_text()
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1 and "→ Fri 13" in _week(app)


@pytest.mark.parametrize(
    "editor, argv",
    [
        (None, ["vi"]),
        ("", ["vi"]),
        ("   ", ["vi"]),
        ('vim "unclosed', ["vi"]),
        ("code --wait", ["code", "--wait"]),
    ],
    ids=["unset", "empty", "blank", "quote", "args"],
)
def test_editor_argv(monkeypatch, editor, argv):
    """$EDITOR as argv; an empty or unparsable one is vi, so the file is never argv[0]."""
    if editor is None:
        monkeypatch.delenv("EDITOR", raising=False)
    else:
        monkeypatch.setenv("EDITOR", editor)
    assert views._editor() == argv


async def test_f_opens_fronts_md_in_the_editor(week_home, monkeypatch):
    path = week_home / "fronts.md"
    path.write_text("# Fronts\n")
    monkeypatch.setenv("EDITOR", "code --wait")
    calls, events = [], []
    app = _app(week_home)
    monkeypatch.setattr(app, "suspend", lambda: _recording_suspend(events))
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        monkeypatch.setattr(views.subprocess, "run", lambda cmd, *a, **k: calls.append(cmd))
        await pilot.press("f")
        await pilot.pause()
        assert not app.query_one("#note").display
    assert calls == [["code", "--wait", str(path)]]
    assert events == ["suspend", "resume"]


async def test_f_editor_missing_shows_one_notice(week_home, monkeypatch):
    (week_home / "fronts.md").write_text("# Fronts\n")
    monkeypatch.setenv("EDITOR", "/nonexistent/editor")
    events = []
    app = _app(week_home)
    monkeypatch.setattr(app, "suspend", lambda: _recording_suspend(events))
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await pilot.press("f", "f")
        await pilot.pause()
        assert events == ["suspend", "resume"] * 2 and app.is_running
        notice = _text(app, "#note")
        assert notice.startswith("Editor did not start: ") and "/nonexistent/editor" in notice
        assert notice.count("Editor did not start") == 1 and "\n" not in notice


async def test_f_without_fronts_md_never_creates_it(week_home, monkeypatch):
    calls = []
    app = _app(week_home)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        monkeypatch.setattr(views.subprocess, "run", lambda *a, **k: calls.append(a))
        await pilot.press("f")
        await pilot.pause()
        assert _text(app, "#note") == "orrery: data unavailable (fronts.md missing)"
    assert calls == []
    assert not (week_home / "fronts.md").exists()


def test_no_subcommand_runs_the_app(monkeypatch):
    ran = []
    monkeypatch.setattr(OrreryApp, "run", lambda self: ran.append(self))
    assert main([]) == 0
    assert len(ran) == 1
