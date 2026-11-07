"""The Week tab's week view: a timetable of the week (days across, parts down, today's date a
chip) with a cursor on a day and one of its blocks; and the panels under it about today's
week, so far this week and the last 5 weeks.

Rendered as Content in theme tokens, with the subject hues for marks (edges, pips, bars); the
month view and the day panel draw in the same styles and marks. Every data-derived string
(subjects, kinds, topics, notes) goes in as a plain segment, never parsed as markup.
"""

from collections import Counter
from datetime import date, timedelta

from textual.containers import VerticalScroll
from textual.content import Content
from textual.reactive import reactive
from textual.widgets import Static

from orrery import schedule
from orrery.board import Cell, DayBoard, Extra, WeekBoard
from orrery.text import bar, fmt_day
from orrery.tui.cursor import WeekTabView, hue
from orrery.tui.diff import MUTED
from orrery.tui.views import panel_title

FG = "$foreground"
NAME = f"bold {FG}"
STRUCK = f"{MUTED} strike"
# today's date, filled in the accent, its text $text: black or white, whichever reads on it
TODAY_CHIP = "bold $text on $accent"
# an ANSI theme's $text is the terminal's default, which on the accent is the month cursor's
# look: there, reverse video, its text the terminal's background
ANSI_CHIP = "bold reverse $accent on $background"
CURSOR = "on $accent-muted"  # the focused block, the week's; the month's focused day
GUTTER = 7  # the part names' column, a space either side
MIN_COL = 6
GROUPS = {"warmup": "warm", "main": "main", "residual": "after", "extra": "extra", "due": "due"}
WEEKS = 5  # the last weeks: the four before this one, then this one so far
BAR = 24  # so far's bar at most
COUNT = 12  # so far's "  N of M" column
CELL = 7  # a week's column in the last weeks at most
BEFORE = "before orrery started"  # once, under a week with days before starts
NOTHING_PLANNED = "nothing planned in these weeks"
DOTS = "weeks before orrery started show ·"
UNWRAPPED = 1000  # a lower panel's width before its first layout: wider than any line

Block = tuple[str, int | None, list[Content]]  # (group, item, lines)


def span(monday: date, weeks: int = 1) -> str:
    """The days of the weeks from monday's, e.g. `Nov 9 – 15` or `Nov 9 – Dec 13`."""
    sunday = monday + timedelta(weeks=weeks, days=-1)
    end = f"{sunday.day}" if sunday.month == monday.month else f"{sunday:%b} {sunday.day}"
    return f"{monday:%b} {monday.day} – {end}"


def title(monday: date, today: date) -> str:
    """The header's controls: the week shown, e.g. `‹ Nov 9 – 15, week 46 ›`, with the year
    its number counts in when that isn't today's (`week 1 of 2027`)."""
    year, n, _ = monday.isocalendar()
    return f"‹ {span(monday)}, week {n}" + (f" of {year}" if year != today.year else "") + " ›"


def short_day(d: date) -> str:
    return f"{d:%a} {d.day}"


def week_name(monday: date, today: date) -> str:
    """The top panel's title: this, next or last week, or its days."""
    weeks = (monday - schedule.week_start(today)).days // 7
    return {0: "This week", 1: "Next week", -1: "Last week"}.get(weeks, span(monday))


def week_caption(monday: date, starts: date, today: date) -> str:
    """The top panel's caption, in the week orrery starts: the days it has, and that day."""
    if not monday <= starts < monday + timedelta(weeks=1):
        return ""
    did = "started" if starts <= today else "starts"
    return f"{starts:%a} to Sun; orrery {did} {short_day(starts)}"


def edged(h: str, *parts: tuple[str, str] | str) -> Content:
    """A block's line: the edge in the subject's hue, then parts."""
    return Content.assemble(("▌", h), *parts)


def pips(c: Cell, h: str) -> tuple[tuple[str, str], ...]:
    """c's planned sessions in its hue (● done, ◐ partial, ○ not yet), then those moved away,
    muted and struck, then how many were added past the week's baseline, muted."""
    planned = "●" * (c.done - c.partial) + "◐" * c.partial + "○" * (c.sessions - c.done)
    added = ((f" +{c.added}", MUTED),) if c.added > 0 else ()
    return (planned, h), ("○" * c.struck, STRUCK), *added


def today_chip(text: str, ansi: bool) -> Content:
    """text as today's chip, padded a space either side, in an ANSI theme or not."""
    return Content.styled(f" {text} ", ANSI_CHIP if ansi else TODAY_CHIP)


class WeekView(WeekTabView):
    """The timetable. ←/→ move a day (past the week's edge into the next or last week), ↑/↓
    a block of the focused day, [/] a week, t back to today, and enter opens the block's
    reports and agenda (the app has them: `Open` carries the block's index). Days before
    starts are left empty, nothing computed for them.

    A block is one line, `▌subject kind ●○` or `▌subject kind +N extra`, where it fits its
    column; narrower, a stack of the subject, then the kind and pips."""

    item = reactive(0)  # of the focused day's blocks, in drawing order

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.board: WeekBoard | str = ""  # the week, or the line saying why it can't be read
        self.slots: dict[str, int] = {}
        self.starts = date.min
        self.col = MIN_COL
        self._hits: list[list[int | None]] = []  # per row, per day: the item drawn there

    def show(self, board: WeekBoard | str, slots: dict[str, int], starts: date = date.min):
        """The week, or the line saying why it can't be read. Another week than the one
        drawn (the cursor crossed into it before it was read) keeps the focused block in
        sight; a reread of the same week leaves the pane where it is."""
        was = self.heading
        self.board, self.slots, self.starts = board, slots, starts
        if isinstance(board, WeekBoard):
            self.heading = title(board.monday, board.today)
            b = board
            self.panel = week_name(b.monday, b.today), week_caption(b.monday, starts, b.today)
        else:
            self.heading, self.panel = "", ("", "")
        self._title()
        self._clamp()  # the focused day may have fewer blocks now
        if self.heading != was:
            self.call_after_refresh(self._reveal)
        self.refresh(layout=True)

    def _count(self, d: date) -> int:
        """How many blocks d has on screen; 0 off the week shown and before starts."""
        if isinstance(self.board, WeekBoard) and d >= self.starts:
            for day in self.board.days:
                if day.date == d:
                    return len(day.cells) + len(day.extras)
        return 0

    def _clamp(self) -> None:
        self.item = min(self.item, max(0, self._count(self.focus_date) - 1))

    def watch_focus_date(self) -> None:
        """Another day: its block of the same index, or its last, kept in sight, even when
        the index is the same and only its rows moved."""
        self._clamp()
        self.call_after_refresh(self._reveal)

    def action_left(self) -> None:
        self.post_message(self.Shift(-1))

    def action_right(self) -> None:
        self.post_message(self.Shift(1))

    def action_earlier(self) -> None:
        self.post_message(self.Shift(-7))

    def action_later(self) -> None:
        self.post_message(self.Shift(7))

    def action_up(self) -> None:
        self.item = max(0, self.item - 1)

    def action_down(self) -> None:
        self.item = min(self.item + 1, max(0, self._count(self.focus_date) - 1))

    def action_open(self) -> None:
        if self._count(self.focus_date):  # none on a day before starts, nor off the week read
            self.post_message(self.Open(self.item))

    def item_rows(self) -> list[int]:
        days = [d.date for d in self.board.days] if isinstance(self.board, WeekBoard) else []
        if self.focus_date not in days:
            return []
        i = days.index(self.focus_date)
        return [y for y, row in enumerate(self._hits) if row[i] == self.item]

    def pick(self, x: int, y: int) -> None:
        if not isinstance(self.board, WeekBoard) or x < GUTTER or y >= len(self._hits):
            return
        i = (x - GUTTER) // self.col
        if i >= 7:
            return
        if (hit := self._hits[y][i]) is not None:
            self.item = hit
        self.post_message(self.Shift((self.board.days[i].date - self.focus_date).days))

    def render(self) -> Content:
        self._hits = []
        if not isinstance(self.board, WeekBoard):
            return Content(self.board)
        self.col = max(MIN_COL, (self.size.width - GUTTER) // 7)
        dark = self.app.current_theme.dark
        days = self.board.days
        before = [d.date < self.starts for d in days]
        blocks = [[] if b else self._blocks(d, dark) for d, b in zip(days, before, strict=True)]
        groups = [g for g in GROUPS if any(b[0] == g for day in blocks for b in day)]
        if not all(before):
            groups = groups or ["main"]
        for day, b in zip(blocks, before, strict=True):
            if not day and not b:  # a rest day says so in the first part shown
                day.append((groups[0], None, [Content.styled("rest", MUTED)]))

        rows: list[Content] = []
        blank = [(self._cell(d, Content(), None), None) for d in days]
        self._row(rows, "", [(self._head(d), None) for d in days])
        for group in groups:
            self._row(rows, "", blank)
            cols = [self._group(d, b, group) for d, b in zip(days, blocks, strict=True)]
            for y in range(max(map(len, cols))):
                cells = [col[y] if y < len(col) else blank[i] for i, col in enumerate(cols)]
                self._row(rows, "" if y else GROUPS[group], cells)
        if any(before):
            self._row(rows, "", blank)
            rows.append(Content.assemble(" " * (GUTTER + 1), (BEFORE, MUTED)))
            self._hits.append([None] * 7)
        return Content("\n").join(rows)

    def _group(self, d: DayBoard, blocks: list[Block], group: str):
        """d's lines in group, each with the item it draws."""
        return [(self._cell(d, x, i), i) for g, i, lines in blocks if g == group for x in lines]

    def _row(self, rows: list[Content], label: str, cells: list[tuple[Content, int | None]]):
        gutter = Content.styled(f" {label}".ljust(GUTTER), MUTED)
        rows.append(Content.assemble(gutter, *(c for c, _ in cells)))
        self._hits.append([i for _, i in cells])

    def _head(self, d: DayBoard) -> Content:
        """d's name, a space into its column: today's a chip, the space its padding, the
        focused day's other than today underlined, the cursor's only mark on a day without a
        block."""
        name = short_day(d.date)
        if d.date == self.today:
            head = today_chip(name, self.app.current_theme.ansi)
        else:
            focused = d.date == self.focus_date
            head = Content(" ") + Content.styled(name, f"{NAME} underline" if focused else NAME)
        return head.truncate(self.col, ellipsis=True, pad=True)

    def _cell(self, d: DayBoard, line: Content, item: int | None) -> Content:
        """line in d's column, after its edge (or a space), cut to the column, on the cursor's
        background if it's the focused block's. A line too long for a space after it (a long
        subject's name, alone on its line) may fill its column, the next column's edge or
        space keeping them apart, rather than be cut."""
        if not line.plain.startswith("▌"):
            line = Content(" ") + line
        out = line.truncate(self.col, ellipsis=True, pad=True)
        if d.date == self.focus_date and item is not None and item == self.item:
            return out.stylize(CURSOR)
        return out

    def _blocks(self, d: DayBoard, dark: bool) -> list[Block]:
        """d's blocks in drawing order, then its deadlines."""
        out: list[Block] = [
            (c.part, i, self._cell_lines(c, hue(self.slots, c.subject, dark)))
            for i, c in enumerate(d.cells)
        ]
        for i, e in enumerate(d.extras, len(d.cells)):
            out.append(("extra", i, self._extra_lines(e, hue(self.slots, e.subject, dark))))
        for x in d.deadlines:  # board's "<subject>: <item>"
            subject, _, item = x.partition(": ")
            out.append(("due", None, [edged(hue(self.slots, subject, dark), item)]))
        return out

    def _fit(self, line: Content, stacked: list[Content]) -> list[Content]:
        """line, if it fits a column with a space after it; else stacked."""
        return [line] if line.cell_length < self.col else stacked

    def _cell_lines(self, c: Cell, h: str) -> list[Content]:
        """The subject, its kind (not when it's the subject) and pips on one line, or stacked:
        the subject, then its kind and pips (the pips and the topic, when the kind is the
        subject), the pips under the kind if they don't fit beside it. Then where struck
        sessions went, and the topic, muted. A block moved away entirely is struck."""
        gone = c.sessions == 0
        marks = pips(c, h)
        subject, kind = (c.subject, STRUCK if gone else NAME), (c.kind, STRUCK if gone else FG)
        if c.kind == c.subject:
            line = edged(h, subject, " ", *marks)
            topic = [(f" {c.topic}", MUTED)] if c.topic else []
            stacked = [edged(h, subject), edged(h, *marks, *topic)]
        else:
            line = edged(h, subject, " ", kind, " ", *marks)
            both = self._fit(edged(h, kind, " ", *marks), [edged(h, kind), edged(h, *marks)])
            stacked = [edged(h, subject), *both]
        one = line.cell_length < self.col  # with a space after it, as _fit
        lines = [line] if one else stacked
        if c.moved_to:
            lines.append(edged(h, (f"→ {short_day(c.moved_to)}", MUTED)))
        if c.topic and (one or c.kind != c.subject):  # else it's beside the pips already
            lines.append(edged(h, (c.topic, MUTED)))
        return lines

    def _extra_lines(self, e: Extra, h: str) -> list[Content]:
        """`▌subject kind +N extra`, or stacked: the subject, its kind, then +N extra."""
        same = e.kind == e.subject
        extra = (f"+{e.done} extra", MUTED)
        line = edged(h, (e.subject, NAME), *([] if same else [" ", (e.kind, FG)]), " ", extra)
        kind = [] if same else [edged(h, (e.kind, FG))]
        stacked = [edged(h, (e.subject, NAME)), *kind, edged(h, extra)]
        return self._fit(line, stacked)


class Lower(Static):
    """A tab's lower panel (the Week tab's on today's week, the Report's stats): drawn from its
    data at its width, again at a resize and a theme change, its title and caption in its top
    border. Each gives ready() (the first read has landed), caption() and body(width, dark)."""

    LABEL = ""

    def on_mount(self) -> None:
        self.app.theme_changed_signal.subscribe(self, lambda _theme: self._paint())

    def on_resize(self) -> None:
        self._paint()

    def _paint(self) -> None:
        """Before its first layout (its tab not shown yet) it has no width: then it's drawn
        unwrapped, as tall as it will be about, and again at its width on its first resize.
        Wrapped to nothing, it would be tall enough to give the pane a scrollbar for a moment
        and leave its title a column short."""
        if self.ready():
            width = self.content_size.width or UNWRAPPED
            self.update(self.body(width, self.app.current_theme.dark))
            self.border_title = panel_title(self.LABEL, self.caption(), self.outer_size.width)


class SoFar(Lower):
    """Per subject planned this week: a bar of the sessions done of those planned
    up to today, N of M, and the week's total; then, muted and wrapped rather than cut, the
    subjects with questions waiting for their mentors and how many. In the caption, all
    subjects' N of M and the days they count, none before starts."""

    LABEL = "So far this week"

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.week: WeekBoard | None = None
        self.rates: dict[str, list[tuple[date, int, int]]] = {}
        self.slots: dict[str, int] = {}
        self.starts = date.min
        self.questions: dict[str, int] = {}

    def show(
        self, week: WeekBoard | None, rates, slots, starts: date = date.min, questions=None
    ) -> None:
        """questions: the open ones per subject with any, in slot order."""
        self.week, self.rates, self.slots, self.starts = week, rates, slots, starts
        self.questions = questions or {}
        self._paint()

    def ready(self) -> bool:
        return self.week is not None

    def _now(self) -> dict[str, tuple[int, int]]:
        """Per subject, this week's (done, planned to date)."""
        b = self.week
        return {
            s: next(((d, p) for m, d, p in w if m == b.monday), (0, 0))
            for s, w in self.rates.items()
        }

    def caption(self) -> str:
        b = self.week
        first = max(b.monday, self.starts)  # the days counted: none before starts
        if first > b.today:
            return f"orrery starts {fmt_day(self.starts)}"
        days = f"{first:%a}" if first == b.today else f"{first:%a} to {b.today:%a}"
        now = self._now().values()
        return f"{sum(d for d, _ in now)} of {sum(p for _, p in now)} planned, {days}"

    def body(self, width: int, dark: bool) -> Content:
        in_week = Counter()
        for day in self.week.days:
            if day.date >= self.starts:  # nothing before starts, as in week_rates
                for c in day.cells:
                    in_week[c.subject] += c.sessions
        name = max(map(len, self.rates), default=0) + 2
        size = max(4, min(BAR, width - 1 - name - COUNT - len("NN in the week")))
        rows = []
        for s, (done, planned) in self._now().items():  # by ord, as week_rates gives them
            if not planned and not in_week[s]:
                continue  # nothing this week: the last 5 weeks still have it
            h, cells = hue(self.slots, s, dark), bar(done, planned, size)
            full = cells.count("█")
            rows.append(
                Content.assemble(
                    ("▌", h),
                    (s.ljust(name), FG),
                    (cells[:full], h),
                    (cells[full:], MUTED),
                    f"  {done} of {planned}".ljust(COUNT),
                    (f"{in_week[s]} in the week", MUTED),
                )
            )
        rows = rows or [Content.styled(NOTHING_PLANNED, MUTED)]
        if self.questions:
            asked = " · ".join(f"{s} {n}" for s, n in self.questions.items())
            rows += Content.styled(f"questions waiting: {asked}", MUTED).wrap(max(1, width))
        return Content("\n").join(rows)


class LastWeeks(Lower):
    """Per subject planned in them, the share of its sessions planned up to today that were
    done, in each of the last 5 weeks ending with this one; · for a week with none planned,
    as every week before starts."""

    LABEL = f"Last {WEEKS} weeks"

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.rates: dict[str, list[tuple[date, int, int]]] | None = None
        self.starts = date.min
        self.slots: dict[str, int] = {}

    def show(self, rates, starts: date, slots: dict[str, int]) -> None:
        """week_rates' last 5 weeks: they leave out the weeks before starts'."""
        self.rates, self.starts, self.slots = rates, starts, slots
        self._paint()

    def ready(self) -> bool:
        return self.rates is not None

    def caption(self) -> str:
        return "planned sessions done"

    def body(self, width: int, dark: bool) -> Content:
        if not self.rates:
            return Content.styled(NOTHING_PLANNED, MUTED)
        last = next(iter(self.rates.values()))[-1][0]  # this week: every list ends with it
        mondays = [last - timedelta(weeks=i) for i in reversed(range(WEEKS))]
        name = max(map(len, self.rates)) + 2
        cell = max(len("100% "), min(CELL, (width - 1 - name) // WEEKS))
        numbers = "".join(f"W{m.isocalendar()[1]}".ljust(cell) for m in mondays)
        rows = [Content.styled(" " * (1 + name) + numbers.rstrip(), MUTED)]
        for s, weeks in self.rates.items():
            got = {m: (d, p) for m, d, p in weeks}
            cells = []
            for m in mondays:
                d, p = got.get(m, (0, 0))
                cells.append(
                    (f"{round(d / p * 100)}%".ljust(cell), FG) if p else ("·".ljust(cell), MUTED)
                )
            rows.append(
                Content.assemble(("▌", hue(self.slots, s, dark)), (s.ljust(name), FG), *cells)
            )
        if mondays[0] < schedule.week_start(self.starts):
            rows += [Content(), Content.styled(DOTS, MUTED)]
        return Content("\n").join(row.rstrip() for row in rows)


class WeekPane(VerticalScroll, can_focus=False):
    """The Week tab's pane: the top panel (the week or the month), then the lower row (so far
    this week above the last 5 weeks, beside the cursor's day), scrolled when taller than
    the window. Never focused itself, so a click outside a view leaves the keys with it."""
