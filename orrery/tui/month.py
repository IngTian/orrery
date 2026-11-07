"""The Week tab's month view: a calendar of six week rows, each day with its planned sessions
as pips in their subjects' hues (filled once reported) and its first topic, today's date a chip.

Days before starts stay empty, with one note; future days come from the template, as the board
reads them. ←/→ move a day and ↑/↓ a week, into the next or last month past its edges; [/] a
month; t back to today; enter shows the day's week in the week view. Drawn in the week view's
styles and marks.
"""

import calendar
from datetime import date, timedelta

from textual.content import Content

from orrery import schedule
from orrery.board import DayBoard, WeekBoard
from orrery.tui.cursor import WeekTabView, hue
from orrery.tui.diff import MUTED
from orrery.tui.week import BEFORE, CURSOR, FG, MIN_COL, pips, short_day, today_chip

ROWS = 6
TOP = 2  # the day names and a blank line, above the weeks
LINES = 4  # per week: its dates, pips and topics, then a blank line


def title(first: date) -> str:
    """The header's controls: the month shown, e.g. `‹ November 2026 ›`."""
    return f"‹ {first:%B %Y} ›"


def grid_mondays(d: date) -> list[date]:
    """The Mondays of the six week rows of d's month."""
    start = schedule.week_start(d.replace(day=1))
    return [start + timedelta(weeks=i) for i in range(ROWS)]


def months_on(d: date, n: int) -> date:
    """d, n months on (back, for negative n), on the month's last day if it's shorter."""
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


class MonthView(WeekTabView):
    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.weeks: tuple[WeekBoard, ...] | str = ""  # the six rows, or the line saying why not
        self.first = date.min  # the month's first day
        self.slots: dict[str, int] = {}
        self.starts = date.min
        self.col = MIN_COL

    def show(self, weeks: tuple[WeekBoard, ...] | str, first: date | None, slots, starts):
        self.weeks, self.slots, self.starts = weeks, slots, starts
        if isinstance(weeks, str):
            self.heading = ""
        else:
            self.first, self.heading = first, title(first)
        self._title()
        self.refresh(layout=True)

    @property
    def panel(self) -> tuple[str, str]:
        """The month, and the cursor's day."""
        if isinstance(self.weeks, str):
            return "", ""
        return f"{self.first:%B %Y}", f"cursor on {short_day(self.focus_date)}"

    def watch_focus_date(self) -> None:
        self._title()

    def action_left(self) -> None:
        self.post_message(self.Shift(-1))

    def action_right(self) -> None:
        self.post_message(self.Shift(1))

    def action_up(self) -> None:
        self.post_message(self.Shift(-7))

    def action_down(self) -> None:
        self.post_message(self.Shift(7))

    def action_earlier(self) -> None:
        self._jump(-1)

    def action_later(self) -> None:
        self._jump(1)

    def action_open(self) -> None:
        self.post_message(self.Switch("week"))

    def _jump(self, n: int) -> None:
        """n months on; no date yet (the data never read), nowhere to jump from."""
        if self.focus_date != date.min:
            self.post_message(self.Shift((months_on(self.focus_date, n) - self.focus_date).days))

    def pick(self, x: int, y: int) -> None:
        week, line = divmod(y - TOP, LINES)
        if isinstance(self.weeks, str) or not 0 <= week < ROWS or line == LINES - 1:
            return
        if (i := x // self.col) < 7:
            self.post_message(self.Shift((self.weeks[week].days[i].date - self.focus_date).days))

    def render(self) -> Content:
        if isinstance(self.weeks, str):
            return Content(self.weeks)
        self.col = max(MIN_COL, self.size.width // 7)
        dark = self.app.current_theme.dark
        # the note goes once, in the last week wholly before starts; else under the calendar
        empty = [i for i, w in enumerate(self.weeks) if w.days[-1].date < self.starts]
        note = empty[-1] if empty else None
        names = [self._fit(Content.styled(f"{d.date:%a}", MUTED)) for d in self.weeks[0].days]
        rows = [Content.assemble(*names), Content()]
        for i, w in enumerate(self.weeks):
            rows.append(self._row(w, self._date))
            if i == note:
                rows.append(Content.styled(BEFORE, MUTED))
            else:
                rows.append(self._row(w, lambda d: self._pips(d, dark)))
            rows += [self._row(w, self._topic), Content()]
        if note is None and self.weeks[0].days[0].date < self.starts:
            rows += [Content.styled(BEFORE, MUTED), Content()]
        return Content("\n").join(rows)

    def _fit(self, line: Content) -> Content:
        return line.truncate(self.col - 1, ellipsis=True, pad=True) + " "

    def _row(self, w: WeekBoard, draw) -> Content:
        """A line of w's days, each drawn into its cell, the focused day's on the cursor's
        background, under today's chip's own (and its edge's, in an ANSI theme). An ANSI
        theme's cursor, drawn in the chip's fill, leaves its cell's gap to the terminal's
        background, the edge before today's chip when the cursor is on the day before."""
        ansi = self.app.current_theme.ansi
        cells = []
        for d in w.days:
            cell = self._fit(draw(d))
            if d.date == self.focus_date:  # an offset in characters: the gap is _fit's last one
                cell = cell.stylize_before(CURSOR, 0, len(cell.plain) - 1 if ansi else None)
            cells.append(cell)
        return Content.assemble(*cells)

    def _date(self, d: DayBoard) -> Content:
        """d's date: today's a chip, the others' muted outside the month. In an ANSI theme the
        cursor is drawn in the accent, the chip's fill: a cell of the terminal's own background
        after the chip keeps it apart from the cursor's (the cell before it is the last day's
        gap, which the cursor leaves unfilled there, or the edge)."""
        if d.date == self.today:
            ansi = self.app.current_theme.ansi
            chip = today_chip(str(d.date.day), ansi)
            return chip + Content.styled(" ", "on $background") if ansi else chip
        return Content.styled(str(d.date.day), FG if d.date.month == self.first.month else MUTED)

    def _pips(self, d: DayBoard, dark: bool) -> Content:
        """A pip per planned session, a block's together, then those moved away, struck; on a
        day without blocks the sessions reported off the plan, or else rest."""
        if d.date < self.starts:
            return Content()
        if not d.cells:
            extra = sum(e.done for e in d.extras)
            return Content.styled(f"+{extra} extra" if extra else "rest", MUTED)
        marks = (pips(c, hue(self.slots, c.subject, dark)) for c in d.cells)
        # the pips and the struck only: a cell this narrow would cut pips for the +N added
        return Content(" ").join(Content.assemble(*m[:2]) for m in marks)

    def _topic(self, d: DayBoard) -> Content:
        topic = next((c.topic for c in d.cells if c.topic), "") if d.date >= self.starts else ""
        return Content.styled(topic, MUTED)
