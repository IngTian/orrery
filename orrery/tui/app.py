"""The app: a header row (orrery, the tabs, the tab's controls), the tabs' panes beside the
organizer chat, and at the bottom T's theme picker over a key line generated from the keymap.

The data root, the clock and the claude binary are injected so tests drive a synthetic
database on a frozen Thursday, and a fake organizer. Each database read and write runs on a
connection of its own, opened and closed by its thread: a reload (collect, the session, then
the board, the chat's newest page and the waiting proposal) in the "collect" group; y, n and
u in the "edit" group, each reloading after; an organizer turn in the "organizer" group, one
at a time. The M and h popups, x's restart and the chat's older pages are short and run on
the app's own thread.

The Week tab's top panel is the week or the month (`view`; w and m switch, as does enter on
a month's day), on one cursor date (`cursor_date`), which is the app's: a view asks to move it
(Shift), and a move into another period of the view (week or month) reloads for that period; a
read the cursor or the view left before it landed reads theirs again. A reload never moves it.
Under the top panel: so far this week and the last 5 weeks, beside the day panel, which shows
the cursor's day once per move, from the read that has its week (`_show_day`).

The Report tab reads a window of `report_range`'s weeks ending with `report_week`, both the
app's, kept as the Week tab's cursor is: the view asks to move it (Shift; z asks with 0) and
the app reads it again, keeping its last week from the one whose window starts on starts'
week to this one (`_window`).
"""

import dataclasses
import logging
import sqlite3
import threading
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from textual import events, work
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import ContentSwitcher, Static, Tab, Tabs
from textual.worker import get_current_worker

from orrery import (
    channels,
    db,
    ledger,
    palette,
    paths,
    proposals,
    schedule,
    settings,
    stats,
    store,
    text,
)
from orrery.board import WeekBoard
from orrery.ops import OpError
from orrery.organizer import prompt, sessions, turn
from orrery.proposals import DiffLine
from orrery.tui import keymap, report, themes, views, week
from orrery.tui.chat import PAGE, ChatInput, ChatPanel
from orrery.tui.cursor import CursorView
from orrery.tui.day import DayPanel, block_at, block_text
from orrery.tui.month import MonthView, grid_mondays
from orrery.tui.report import Evaluations, ReportPane, ReportView, YourStats
from orrery.tui.themes import ThemePicker
from orrery.tui.views import Confirm, KeyFooter, Popup
from orrery.tui.week import LastWeeks, SoFar, WeekPane, WeekView

logger = logging.getLogger(__name__)

UNAVAILABLE = "orrery: data unavailable ({})"
KEPT = "kept"
DISCARDED = "discarded; requests stay pending"
ANSWERS = {"y": "y", "yes": "y", "n": "n", "no": "n"}  # typed, while a diff shows: its keys
UNDONE = "undone"
NOTHING_TO_UNDO = "nothing to undo"
WORKING = "working… (esc cancels)"
STILL_ANSWERING = "the organizer is still answering; esc cancels"
NEW_CHAT = "End this organizer session and start a new one? Its transcript is kept; memory stays."
NO_NOTES = "no notes"
FORGET = "ask the organizer to forget a note"
MIN_SIZE = (80, 24)
DOCKED = 110  # from this width the chat is docked on the right; narrower, a strip
PANELS = 160  # from this width a tab's lower panels sit side by side; narrower, they stack
TOO_SMALL = "orrery needs at least 80×24"
View = Literal["week", "month"]
VIEWS: dict[str, type[CursorView]] = {"week": WeekView, "month": MonthView}


def _period(view: str, d: date) -> date:
    """The first day of the period view draws around d: its week or its month."""
    return {"week": schedule.week_start(d), "month": d.replace(day=1)}[view]


def _window(last: date, starts: date, day: date, weeks: int) -> date:
    """The last week of the Report's window of weeks for last: no later than day's, and no
    earlier than the one whose window starts on starts' week, unless that's later (then
    day's, as before starts)."""
    hi = schedule.week_start(day)
    lo = min(hi, schedule.week_start(starts) + timedelta(weeks=weeks - 1))
    return min(max(lo, schedule.week_start(last)), hi)


def _tell(conn: sqlite3.Connection, now: datetime, line: str) -> None:
    """orrery's line in the active chat, for the organizer's next turn to hear."""
    sessions.say(conn, sessions.ensure(conn, now).id, "orrery", line, now)


def _names(names, current: str) -> Content:
    """The open tab's views (or the Report's ranges) for the header, current in the accent."""
    return Content("  ").join(
        Content.styled(v, "bold $accent" if v == current else "") for v in names
    )


@dataclass(frozen=True)
class Loaded:
    """One reload: the cursor's week, or the line saying why it can't be read; the day and
    boundary it was read for; the chat's newest page, oldest first, and when the active chat
    began; the waiting proposal and its diff, or the line saying why they can't be; pending
    requests; last_review from settings; and for the lower left panels today's week, its
    subjects' weekly rates and the open questions per subject with any; the subjects' colour
    slots, and the day orrery started. Then the view and the cursor date it was read for, the
    month view's six weeks, and for the day panel each drawn day's sittings and changes. Then
    the Report's window: its last week and range, its days, summary, best week and rhythm, the
    strip's subjects (the active ones and any other planned in it), and every subject's
    assessments."""

    board: WeekBoard | str
    day: date | None = None
    boundary: int = 5
    talk: tuple[sessions.Line, ...] = ()
    since: str = ""
    proposal: int | None = None
    diff: tuple[DiffLine, ...] = ()
    note: str = ""
    pending: tuple[channels.Message, ...] = ()
    last_review: str | None = None
    this_week: WeekBoard | None = None
    starts: date = date.min
    rates: dict[str, list[tuple[date, int, int]]] = field(default_factory=dict)
    questions: dict[str, int] = field(default_factory=dict)
    slots: dict[str, int] = field(default_factory=dict)
    view: View = "week"
    cursor: date | None = None
    month: tuple[WeekBoard, ...] = ()
    sessions: dict[date, tuple[stats.SessionRow, ...]] = field(default_factory=dict)
    changes: dict[date, tuple[tuple[datetime, str], ...]] = field(default_factory=dict)
    report_week: date | None = None
    report_range: str = "month"
    report_days: dict[date, dict[str, tuple[int, int]]] = field(default_factory=dict)
    summary: dict[str, stats.Summary] = field(default_factory=dict)
    best_week: tuple[date, int, int] | None = None
    rhythm: stats.Rhythm | None = None
    strip: tuple[str, ...] = ()
    assessed: dict[str, list[channels.Assessment]] = field(default_factory=dict)


class _ReviewRace(Exception):
    """Lost the race to claim today's review; rollback the change."""


class _Tabs(Tabs, can_focus=False):
    """The tab row, switched by tab and shift+tab or a click. Never focused, so the arrows
    stay the views' own."""


class OrreryApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "orrery"
    BINDINGS = keymap.app_bindings()
    AUTO_FOCUS = None  # on_mount gives the keyboard to the view, never to the chat input
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (DOCKED, "-wide")]

    def __init__(
        self,
        *,
        root: Callable[[], Path] = paths.data_root,
        now: Callable[[], datetime] = datetime.now,
        claude_path: str | None = None,
    ) -> None:
        super().__init__()
        # An instance attribute in Textual 8.2: a class attribute would be a silent no-op.
        self.animation_level = "none"
        self.root = root
        self.now = now
        self.claude_path = claude_path  # overrides config.toml's, for tests
        self.board: WeekBoard | str = ""
        self.day: date | None = None  # None while the data can't be read: each minute retries
        self.view: View = "week"
        self.cursor_date: date | None = None  # the views' date: max(today, starts) at first
        self.report_week: date | None = None  # the Report window's last week: this one at first
        self.report_range = "month"  # its length, a report.RANGES key: z's
        self.starts = date.min
        self.boundary = 5
        self.shown_proposal: int | None = None  # the id whose diff is on screen, for y and n
        self.turn_cancel: threading.Event | None = None  # set while an organizer turn runs
        self._typing = False  # the input had the keyboard when the frame hid
        self._got: Loaded | None = None  # the read on screen: the day panel's days, enter's blocks

    def compose(self) -> ComposeResult:
        with Horizontal(id="header"):
            yield Static("orrery", id="brand")
            yield _Tabs(Tab("Week", id="tab-week"), Tab("Report", id="tab-report"))
            yield Static(id="views")
            yield Static(id="controls", markup=False)
        with Horizontal(id="body"):
            with ContentSwitcher(id="tabs", initial="week"):
                with WeekPane(id="week"):
                    yield WeekView(id="week-view", classes="panel")
                    yield MonthView(id="month-view", classes="panel")
                    with Container(id="lower"):  # side by side from 160 columns, else stacked
                        with Vertical(id="lower-left"):
                            yield SoFar(id="so-far", classes="panel")
                            yield LastWeeks(id="last-weeks", classes="panel")
                        yield DayPanel(id="day", classes="panel")
                with ReportPane(id="report"):
                    yield ReportView(id="report-view", classes="panel")
                    with Container(id="report-lower"):  # as the Week tab's lower row
                        yield YourStats(id="your-stats", classes="panel")
                        yield Evaluations(id="evaluations", classes="panel")
            yield ChatPanel(self._older, id="chat")
        with Vertical(id="bottom"):  # one docked container: two docked widgets clip each other
            yield ThemePicker(id="theme-picker")
            yield KeyFooter(id="footer")
        yield Static(TOO_SMALL, id="too-small")

    def on_mount(self) -> None:
        cfg = settings.load_settings(self.root())
        name = None if isinstance(cfg, store.Unavailable) else cfg.theme
        # resolved, never the raw name: a stale one would raise InvalidThemeError at startup
        self.theme = themes.resolve(name, self)
        self._show_view()
        self._focus_view()
        self.app_reload()
        self.set_interval(60, self._check_day)

    def on_unmount(self) -> None:
        """Quitting mid-turn: the turn is killed and recorded as cancelled, nothing drawn."""
        self.workers.cancel_group(self, "organizer")
        if self.turn_cancel is not None:
            self.turn_cancel.set()

    def on_resize(self, event: events.Resize) -> None:
        """Below 80 columns or 24 rows, one line says the size needed instead of the frame.
        A hidden input loses the keyboard, and the next letter would fire the app's key, so
        while you type: folding to a strip opens the chat over the content, and the frame
        hidden by the line gives the input the keyboard back when it returns. Docked, the
        chat is no longer over the content: narrowing again shows the strip until c. Hidden,
        the view lets go of the keyboard too, so its keys never move what you can't see, and
        takes it back with the frame; a theme preview is put back, as its picker goes too.
        From 160 columns the frame's screen has -panels as well as -wide."""
        width, height = MIN_SIZE
        tiny = event.size.width < width or event.size.height < height
        typing = self._typing or isinstance(self.focused, ChatInput)
        self._typing = typing and tiny  # owed the keyboard until the frame is back
        self.set_class(tiny, "-tiny")
        picker = self.query_one(ThemePicker)
        if tiny and picker.is_open:
            picker.cancel()
        base = self.screen_stack[0]  # the frame's screen, under any dialog
        # Its breakpoint now, not at its own resize after this one: else the overlay goes
        # while the screen is still narrow, the input folds into the strip and loses the keys.
        base.set_class(event.size.width < DOCKED, "-narrow")
        base.set_class(event.size.width >= DOCKED, "-wide")
        base.set_class(event.size.width >= PANELS, "-panels")  # not a breakpoint: it'd drop -wide
        base.query_one(YourStats).fit()
        chat = self.query_one(ChatPanel)
        if event.size.width >= DOCKED:
            chat.remove_class("-overlay")
        elif typing:
            chat.add_class("-overlay")
        if tiny:
            base.set_focus(None)
        elif typing:
            self.query_one(ChatInput).focus()
        elif base.focused is None:
            self._focus_view()
        self._header()

    def _focus_view(self) -> None:
        """The keyboard to the open tab's view, on the frame's screen even under a dialog; to
        nothing on a tab without one, or while the frame is hidden."""
        base = self.screen_stack[0]
        pane = base.query_one("#tabs", ContentSwitcher).visible_content
        shown = pane is not None and not self.has_class("-tiny")
        views = [v for v in pane.query(CursorView) if v.display] if shown else []
        # never Textual's scroll to the focused: a view keeps its own place in the pane
        base.set_focus(views[0] if views else None, scroll_visible=False)

    def _show_view(self) -> None:
        """The Week tab's view in effect shown on the cursor's date, with its controls in the
        header; the other hidden, and the panels under it shown once there's a week to show."""
        for name, cls in VIEWS.items():
            self.query_one(cls).display = name == self.view
        self.query_one("#lower").display = self.query_one(SoFar).week is not None
        view = self.query_one(VIEWS[self.view])
        if self.cursor_date is not None:
            view.focus_date = self.cursor_date
        self._header()

    def _header(self) -> None:
        """The open tab's controls in the header: the Week tab's views and its view's period,
        or the Report's ranges (docked, from 110 columns: narrower, its window's weeks name
        the range) and its window."""
        on_report = self._on_report()
        view = self.query_one(ReportView if on_report else VIEWS[self.view])
        names = (report.RANGES, view.range) if on_report else (VIEWS, self.view)
        views = self.query_one("#views", Static)
        views.display = not on_report or self.screen_stack[0].has_class("-wide")
        views.update(_names(*names))
        self.query_one("#controls", Static).update(view.heading)

    def _on_report(self) -> bool:
        return self.query_one("#tabs", ContentSwitcher).current == "report"

    def _handler(self, k: keymap.Key) -> Callable[[], object] | None:
        """What k runs on the open tab, or None: an app or proposal key, the app's
        app_<action>, as dispatch calls it; the tab's own key, its view's action_<action>, as
        the view's binding calls it; another tab's, nothing. On the frame's screen, even
        under a dialog."""
        if k.scope in keymap.DISPATCHED:
            return getattr(self, f"app_{k.action}", None)
        tab = self.screen_stack[0].query_one("#tabs", ContentSwitcher).current
        view = self.screen_stack[0].query_one(ReportView if tab == "report" else VIEWS[self.view])
        return getattr(view, f"action_{k.action}", None) if k.scope == tab else None

    def handles(self, k: keymap.Key) -> bool:
        """Whether k does something on the open tab: what the key line asks, so it never lists
        a key that does nothing, nor misses one that does."""
        return self._handler(k) is not None

    def action_dispatch(self, key: str) -> None:
        if self.has_class("-tiny") and (key != "q" or self._typing):
            return  # the frame is hidden: only q acts, unless you were typing, so nothing
            # changes unseen and a word with a q in it never quits
        if key in keymap.PRIORITY and (
            isinstance(self.screen, ModalScreen) or isinstance(self.focused, ChatInput)
        ):
            return  # tab reaches here past a dialog and the chat input: there it does nothing
        chat = self.query_one(ChatPanel)
        # a strip hides the diff: y and n wait until c opens the chat over the content
        folded = self.screen.has_class("-narrow") and not chat.has_class("-overlay")
        shown = self.shown_proposal is not None and not folded
        entry = keymap.lookup(key, ("proposal", "app") if shown else ("app",))
        if entry is not None and (run := self._handler(entry)) is not None:
            chat.note("")  # a key's line lasts until the next key
            run()

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        """The tab's pane, its controls in the header and its keys on the key line (the Tabs
        activate the first at mount, which draws the line the first time)."""
        tab = event.tab.id.removeprefix("tab-")
        self.query_one("#tabs", ContentSwitcher).current = tab
        self._header()
        self.query_one(KeyFooter).show(tab, self.handles)
        if not isinstance(self.focused, (ChatInput, ThemePicker)):
            self._focus_view()

    def on_cursor_view_shift(self, event: CursorView.Shift) -> None:
        """The cursor moved by days; into another period of the view, that period is read.
        Judged by the cursor's own period, not the one drawn: a read still in flight is for
        the period it left, and the new reload drops it. The day panel shows the day from the
        read that has it: the one on screen inside the period, the new one past its edge (the
        month's grid may draw the day already, but then the read would show it twice)."""
        if self.cursor_date is None:
            return
        left = _period(self.view, self.cursor_date)
        self.cursor_date += timedelta(days=event.days)
        self.query_one(VIEWS[self.view]).focus_date = self.cursor_date
        if _period(self.view, self.cursor_date) != left:
            self.app_reload()
        else:
            self._show_day()

    def _week_of(self, d: date) -> WeekBoard | None:
        """d's week, if the read on screen has it."""
        got = self._got
        weeks = (got.board, *got.month) if got is not None else ()
        monday = schedule.week_start(d)
        return next((w for w in weeks if isinstance(w, WeekBoard) and w.monday == monday), None)

    def _show_day(self) -> None:
        """The day panel on the cursor's day, from the read on screen; a day whose week isn't
        read yet waits for its read, the day shown meanwhile. While the data can't be read,
        the day alone."""
        got, d = self._got, self.cursor_date
        if got is None or d is None:
            return
        board = self._week_of(d)
        if board is None and isinstance(got.board, WeekBoard):
            return
        args = got.sessions.get(d, ()), got.changes.get(d, ()), got.slots, got.starts, got.boundary
        self.query_one(DayPanel).show(d, board, *args)

    def on_cursor_view_switch(self, event: CursorView.Switch) -> None:
        """w, m, or enter on a month's day: that view, on the cursor's date, from the pane's
        top, given the keyboard; its period is read. Never on the Report tab, say a key's
        Switch landing after tab."""
        if event.view != self.view and not self._on_report():
            self.view = event.view
            self._show_view()
            self.query_one(WeekPane).scroll_home(animate=False)
            self._focus_view()
            self.app_reload()

    def on_cursor_view_open(self, event: CursorView.Open) -> None:
        """enter on a week's block (its index): its reports and agenda, from the read on
        screen."""
        d = self.cursor_date
        board = self._week_of(d) if d is not None else None
        if board is None:
            return
        day = next(x for x in board.days if x.date == d)
        b = block_at(day, self._got.sessions.get(d, ()), event.target)
        if b is not None:
            self.push_screen(Popup(block_text(b)))

    def on_report_view_shift(self, event: ReportView.Shift) -> None:
        """The Report window moved by days, or z changed its range: its last week, kept by
        _window, and range are read."""
        r = self.query_one(ReportView)
        was = self.report_week, self.report_range
        self.report_range = r.range
        if self.report_week is not None and self.day is not None:
            last = self.report_week + timedelta(days=event.days)
            weeks = report.RANGES[r.range]
            self.report_week = r.focus_date = _window(last, self.starts, self.day, weeks)
        if (self.report_week, self.report_range) != was:
            self.app_reload()
        self._header()

    def on_report_view_open(self, event: ReportView.Open) -> None:
        """enter on the Report's strip: every week's evaluations of its pick."""
        self.push_screen(Popup(event.target))

    def app_next_tab(self) -> None:
        self.query_one(_Tabs).action_next_tab()

    def app_previous_tab(self) -> None:
        self.query_one(_Tabs).action_previous_tab()

    def _check_day(self) -> None:
        """Reload once the day boundary passes."""
        if schedule.today(self.now(), self.boundary) != self.day:
            self.app_reload()

    def _connect(self) -> tuple[sqlite3.Connection, datetime, int] | str:
        """A connection for this thread alone, now and the day boundary; or the line saying why
        the data can't be read."""
        root = self.root()
        cfg = settings.load_settings(root)
        if isinstance(cfg, store.Unavailable):
            return UNAVAILABLE.format(cfg.reason)
        conn = db.open_existing(root)
        if isinstance(conn, store.Unavailable):
            return UNAVAILABLE.format(conn.reason)
        return conn, self.now(), cfg.day_boundary_hour

    @work(exclusive=True, thread=True, group="collect")
    def app_reload(self) -> None:
        """Collect what the mentors filed, make sure an organizer session exists, then read the
        cursor's week (or month) and its days, today's week, the chat's newest page and the
        waiting proposal."""
        try:
            got = self._read()
        except Exception as e:  # one line says so, and the app stays up
            logger.warning("reload failed", exc_info=True)
            got = Loaded(UNAVAILABLE.format(e))
        if not get_current_worker().is_cancelled:  # a newer reload draws instead
            self.call_from_thread(self._loaded, got)

    def _read(self) -> Loaded:
        got = self._connect()
        if isinstance(got, str):
            return Loaded(got)
        conn, now, boundary = got
        with closing(conn):
            day = schedule.today(now, boundary)
            channels.collect(conn, now=now, today=day)
            monday = schedule.week_start(day)
            s = sessions.ensure(conn, now)  # a first open; x's restart inserts its own
            row = conn.execute("select value from settings where key = 'last_review'").fetchone()
            starts = schedule.starts(conn)
            # at the first read today, or the day orrery starts if that's later
            view, cursor = self.view, self.cursor_date or max(day, starts)
            board = stats.week(conn, schedule.week_start(cursor), day)
            month = (
                [stats.week(conn, m, day) for m in grid_mondays(cursor)] if view == "month" else []
            )
            # ponytail: two queries a day drawn, 84 for a month; one ranged query if that's slow
            drawn = [x.date for w in month or [board] for x in w.days]
            span = self.report_range, report.RANGES[self.report_range]
            last = _window(
                self.report_week or monday, starts, day, span[1]
            )  # starts' may have come
            # from the window's first Monday to its last Sunday; the stats count none before starts
            first, end = last - timedelta(weeks=span[1] - 1), last + timedelta(days=6)
            summary = stats.range_summary(conn, first, end, day)
            loaded = Loaded(
                board,
                day,
                boundary,
                talk=tuple(reversed(sessions.all_lines(conn, limit=PAGE))),
                since=s.started_at,
                pending=tuple(channels.pending_requests(conn)),
                last_review=row[0] if row else None,
                this_week=stats.week(conn, monday, day),
                rates=stats.week_rates(conn, monday, week.WEEKS, day),
                questions={x.subject: n for x in channels.status(conn) if (n := x.open_questions)},
                slots=palette.slots(conn),
                starts=starts,
                view=view,
                cursor=cursor,
                month=tuple(month),
                sessions={d: tuple(stats.day_sessions(conn, d)) for d in drawn},
                changes={d: tuple(stats.day_changes(conn, d, boundary)) for d in drawn},
                report_week=last,
                report_range=span[0],
                report_days=stats.daily(conn, first, end, day),
                summary=summary,
                best_week=stats.best_week(conn, first, end, day),
                rhythm=stats.rhythm(conn, first, min(end, day), boundary),
                strip=tuple(
                    s.name for s in schedule.all_subjects(conn) if s.active or s.name in summary
                ),
                assessed={
                    s.name: channels.assessments(conn, s.name) for s in schedule.all_subjects(conn)
                },
            )
            try:
                w = proposals.waiting(conn)
                if w is None:
                    return loaded
                diff = tuple(proposals.diff(conn, w, day))
                return dataclasses.replace(loaded, proposal=w.id, diff=diff)
            except Exception as e:  # the week still shows; the line says why the proposal can't
                logger.warning("waiting proposal unreadable", exc_info=True)
                return dataclasses.replace(loaded, note=f"can't show the waiting proposal: {e}")

    def _older(self, before_id: int) -> list[sessions.Line] | None:
        """The chat's page of rows before before_id, newest first; None if unreadable, so the
        next scroll to the top tries again."""
        try:
            got = self._connect()
            if isinstance(got, str):
                return None
            with closing(got[0]) as conn:
                return sessions.all_lines(conn, before_id=before_id, limit=PAGE)
        except Exception:  # scrolling back must never take the app down
            logger.warning("older transcript unreadable", exc_info=True)
            return None

    def _loaded(self, got: Loaded) -> None:
        self._got = got
        self.board, self.day, self.boundary = got.board, got.day, got.boundary
        self.shown_proposal = got.proposal
        if self.cursor_date is None:
            self.cursor_date = got.cursor  # the first read's; from then on only Shift moves it
        if got.report_week is not None:  # the Report window's, kept as the read kept it
            weeks = report.RANGES[self.report_range]
            self.report_week = _window(self.report_week or got.day, got.starts, got.day, weeks)
        self.starts = got.starts
        if got.day is not None:  # every view's t, even one shown before its own read lands
            for v in self.query(CursorView):
                v.today = got.day
        read = isinstance(got.board, WeekBoard)
        drawn = got.view if read else self.view  # the line saying why, in the view on screen
        if drawn == "week":
            self.query_one(WeekView).show(got.board, got.slots, got.starts)
        else:
            first = got.cursor.replace(day=1) if read else None
            self.query_one(MonthView).show(got.month or got.board, first, got.slots, got.starts)
        self.query_one(SoFar).show(got.this_week, got.rates, got.slots, got.starts, got.questions)
        self.query_one(LastWeeks).show(got.rates, got.starts, got.slots)
        r = self.query_one(ReportView)
        r.focus_date = self.report_week or date.min
        if not read:
            r.show(got.board)
            self.query_one("#report-lower").display = False
        elif got.report_week is not None:  # a read without the window leaves it be
            self.query_one("#report-lower").display = True
            last, weeks = got.report_week, report.RANGES[got.report_range]
            args = got.strip, got.assessed, got.slots, got.starts, got.boundary
            r.show(got.report_days, last, weeks, *args)
            first = max(r.weeks[0], got.starts)  # the stats' own days, none before starts
            days = report.dates(first, last) if first <= last + timedelta(days=6) else ""
            so_far = self.query_one(SoFar).caption()  # this week's, as the Week tab says it
            stats_args = got.summary, got.rhythm, got.best_week, so_far, got.slots, days
            self.query_one(YourStats).show(*stats_args)
            self.query_one(Evaluations).show(got.assessed, last, got.slots, got.boundary)
        self._show_view()
        self._show_day()
        if read:
            was = got.view, _period(got.view, got.cursor), got.report_week, got.report_range
            now = self.view, _period(self.view, self.cursor_date), self.report_week
            if was != (*now, self.report_range):
                self.app_reload()  # the cursor, the view or the Report's window moved meanwhile
        chat = self.query_one(ChatPanel)
        chat.show(got.talk, got.since)
        if self.turn_cancel is None:  # the turn's over: its reply is in the transcript now,
            chat.stream(None)  # and a line it never recorded (busy, say) was never sent
            chat.drop_pending()
        chat.show_proposal(got.diff)
        if got.note:
            chat.note(got.note)
        self._maybe_review(got.pending, got.day, got.last_review)

    @work(thread=True, group="edit")
    def _edit(self, fn: Callable[[sqlite3.Connection, datetime, date], str]) -> None:
        """fn on a connection of this worker's own; its line shown, then a reload."""
        self.call_from_thread(self._edited, self._run(fn))

    def _run(self, fn: Callable[[sqlite3.Connection, datetime, date], str]) -> str:
        """fn on a connection of this thread's own, and its line; or the line saying why not."""
        try:
            got = self._connect()
            if isinstance(got, str):
                line = got
            else:
                conn, now, boundary = got
                with closing(conn):
                    line = fn(conn, now, schedule.today(now, boundary))
        except (proposals.Stale, OpError) as e:  # nothing written: why, in one line
            line = str(e)
        except Exception as e:  # whatever breaks, one line says so and the app stays up
            logger.warning("edit failed", exc_info=True)
            line = UNAVAILABLE.format(e)
        return line

    def _edited(self, line: str) -> None:
        self.query_one(ChatPanel).note(line)
        self.app_reload()

    def _claim_review(self, root: Path, today: date, now: datetime) -> bool:
        """Claim today's review with a conditional upsert; returns True if claimed."""
        conn = db.open_existing(root)
        if isinstance(conn, store.Unavailable):
            return False
        with closing(conn):
            try:
                with ledger.change(conn, actor="orrery", kind="review", now=now):
                    cur = conn.execute(
                        "insert into settings (key, value) values ('last_review', ?)"
                        " on conflict(key) do update set value = ? where value != ?",
                        (today.isoformat(), today.isoformat(), today.isoformat()),
                    )
                    if cur.rowcount == 0:
                        raise _ReviewRace()
                return True
            except _ReviewRace:
                return False

    def _maybe_review(
        self,
        pending: tuple[channels.Message, ...],
        today: date | None,
        last_review: str | None,
    ) -> None:
        """After reload: the first eligible reload of the day (no turn, no proposal) claims the
        day even with nothing pending; a review turn starts only if requests are
        pending, so a request filed later waits for the next day."""
        if self.turn_cancel is not None or self.shown_proposal is not None or today is None:
            return
        # Skip if already reviewed today
        if last_review == today.isoformat():
            return
        try:
            # Claim the day
            if not self._claim_review(self.root(), today, self.now()) or not pending:
                return
            # Start the turn
            self.turn_cancel = threading.Event()
            line = prompt.review_line(pending)
            chat = self.query_one(ChatPanel)
            chat.append(sessions.Line("", "orrery", line))
            chat.stream(WORKING)
            self._converse(line, self.turn_cancel, chat.stream, speaker="orrery")
        except Exception:
            logger.warning("review setup failed", exc_info=True)

    def app_cancel(self) -> None:
        """esc, in the chat or out of it: cancels a running turn; with none, leaves the chat
        input for the view, or else closes the chat opened over a narrow screen."""
        if self.turn_cancel is not None:
            self.turn_cancel.set()
        elif isinstance(self.focused, ChatInput):
            self._focus_view()
        else:
            self.query_one(ChatPanel).remove_class("-overlay")

    def app_new_chat(self) -> None:
        if self.turn_cancel is not None:
            self.query_one(ChatPanel).note(STILL_ANSWERING)
            return
        self.push_screen(Confirm(NEW_CHAT), self._new_chat)

    def _new_chat(self, yes: bool | None) -> None:
        """On the app's thread, so no turn can start between the dialog and the restart."""

        def restart(conn: sqlite3.Connection, now: datetime, today: date) -> str:
            try:
                with store.file_lock("organizer", blocking=False):
                    sessions.end_and_restart(conn, now)
            except BlockingIOError:  # another window's turn
                return turn.BUSY
            return ""

        if yes:
            self._edited(self._run(restart))

    def app_memory(self) -> None:
        def notes(conn: sqlite3.Connection, now: datetime, today: date) -> str:
            got = [f"#{n.id} {n.text}" for n in sessions.memory(conn)] or [NO_NOTES]
            return "\n".join([*got, "", FORGET])

        self.push_screen(Popup(self._run(notes)))

    def app_history(self) -> None:
        def history(conn: sqlite3.Connection, now: datetime, today: date) -> str:
            return text.history_text(conn, since=today - timedelta(days=7))

        self.push_screen(Popup(self._run(history)))

    @work(thread=True, group="organizer")
    def _converse(
        self,
        line: str,
        cancel: threading.Event,
        stream: Callable[[str], None],
        speaker: Literal["you", "orrery"] = "you",
    ) -> None:
        """One turn, its reply streamed into the chat; then the notes it remembered and its
        line, if any, and a reload."""
        worker = get_current_worker()

        def on_text(reply: str) -> None:
            if not worker.is_cancelled:  # cancelled on quit: the app's going, draw nothing
                self.call_from_thread(stream, reply)

        try:
            root, before = self.root(), self._memory()
            cfg = settings.load_settings(root)
            if isinstance(cfg, store.Unavailable):
                said = UNAVAILABLE.format(cfg.reason)
            else:
                if self.claude_path is not None:
                    cfg = dataclasses.replace(cfg, claude_path=self.claude_path)
                r = turn.run_turn(
                    root, cfg, line, speaker, now=self.now, on_text=on_text, cancel=cancel
                )
                notes = [
                    f"remembered: {t}" for i, t in self._memory().items() if before.get(i) != t
                ]
                said = "\n".join([*notes, *([r.line] if r.line else [])])
        except Exception as e:  # run_turn never raises; the reads around it might
            logger.warning("organizer turn failed", exc_info=True)
            said = UNAVAILABLE.format(e)
        if not worker.is_cancelled:
            self.call_from_thread(self._answered, said)

    def _memory(self) -> dict[int, str]:
        """The memory notes by id, on a connection of this thread's own; {} if unreadable."""
        got = self._connect()
        if isinstance(got, str):
            return {}
        with closing(got[0]) as conn:
            return {n.id: n.text for n in sessions.memory(conn)}

    def _answered(self, said: str) -> None:
        self.turn_cancel = None
        self.query_one(ChatPanel).note(said)
        self.app_reload()  # the transcript, and a proposal the turn made

    def app_keep(self) -> None:
        """Keep the proposal whose diff is shown, never a newer one; one y per diff. orrery
        says so in the chat, which is how the organizer hears it."""
        pid, self.shown_proposal = self.shown_proposal, None

        def keep(conn: sqlite3.Connection, now: datetime, today: date) -> str:
            proposals.keep(conn, pid, now=now, today=today)
            _tell(conn, now, f"kept proposal #{pid}")
            return KEPT

        self._edit(keep)

    def app_discard(self) -> None:
        pid, self.shown_proposal = self.shown_proposal, None

        def discard(conn: sqlite3.Connection, now: datetime, today: date) -> str:
            proposals.discard(conn, pid, now=now)
            _tell(conn, now, f"discarded proposal #{pid}; its requests stay pending")
            return DISCARDED

        self._edit(discard)

    def app_undo(self) -> None:
        def undo(conn: sqlite3.Connection, now: datetime, today: date) -> str:
            return UNDONE if proposals.undo_last(conn, now=now, today=today) else NOTHING_TO_UNDO

        self._edit(undo)

    def app_chat(self) -> None:
        self.query_one(ChatPanel).focus_input()

    def on_chat_panel_submitted(self, event: ChatPanel.Submitted) -> None:
        """The input's line: one organizer turn, the user's line and the working line shown at
        once, the input kept for the next. While a turn runs nothing starts, and the line
        stays in the input. A bare y or n (yes, no) while a diff is shown is that key, as if
        pressed outside the input, even mid-turn: never the organizer's."""
        line = event.text.strip()
        if not line:
            return
        chat = self.query_one(ChatPanel)
        key = ANSWERS.get(line.lower())
        if key is not None and self.shown_proposal is not None:
            self.query_one(ChatInput).clear()
            self.action_dispatch(key)
            return
        if self.turn_cancel is not None:
            chat.note(STILL_ANSWERING)
            return
        self.query_one(ChatInput).clear()
        self.turn_cancel = threading.Event()
        chat.append(sessions.Line("", "you", line))
        chat.stream(WORKING)
        self._converse(line, self.turn_cancel, chat.stream)

    def app_keys(self) -> None:
        self.push_screen(Popup(keymap.help_text()))

    def app_fronts(self) -> None:
        self.query_one(ChatPanel).note(views.edit_fronts(self.root() / "fronts.md", self))

    def app_quit(self) -> None:
        self.exit()

    def app_theme(self) -> None:
        """The picker, on the theme in effect (not config.toml's, which may be stale). A second
        T mid-preview only refocuses it, say after a click took the keyboard: reopening would
        move the theme esc restores."""
        picker = self.query_one(ThemePicker)
        if picker.is_open:
            picker.focus()
        else:
            picker.open(self.theme)

    def on_theme_picker_chosen(self, event: ThemePicker.Chosen) -> None:
        """Already applied by the preview; only saving is left. A failed save says so in one
        line and the theme stays for this run."""
        event.stop()
        self.theme = event.name
        try:
            settings.update_config(self.root() / "config.toml", {"theme": event.name})
            line = f"theme {event.name}"
        except (OSError, store.RefusedWrite) as e:
            line = f"theme {event.name}, not saved: {e}"
        self.query_one(ChatPanel).note(line)
        self._focus_view()

    def on_theme_picker_cancelled(self, event: ThemePicker.Cancelled) -> None:
        """The picker has put the theme back; hidden, it drops focus, which goes back to the
        view."""
        event.stop()
        self._focus_view()
