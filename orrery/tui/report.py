"""The Report tab, over a range (`z`: month, quarter, half or year, 5, 13, 26 or 53 weeks)
ending with the window's last week. ←/→ and [/] move the window by its own length and t
brings it back to this week; the app keeps it from the week whose window starts on starts'
week to this one. It always draws all its weeks: in orrery's first ones, the days before
starts are blank.

On top, Completion: the subject strip (all, then the subjects by slot), whose pick ↑/↓ cycle
and a click sets, over the heatmap of the pick's days. Under it, your stats for the range,
beside each mentor's newest weekly evaluation up to the window's end; enter lists every
week's for the pick (all: every subject's) in a popup (`evaluations_text`). Drawn in the week
view's styles and marks; the mentors' text goes in as plain segments, wrapped, never cut.
"""

from datetime import date, timedelta

from textual import events
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.content import Content
from textual.geometry import Size
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from orrery import schedule
from orrery.channels import Assessment
from orrery.stats import MIN_TIMED, Rhythm, Summary
from orrery.text import fmt_day
from orrery.tui import keymap
from orrery.tui.cursor import CursorView, hue
from orrery.tui.diff import MUTED, para
from orrery.tui.heatmap import ALL, Heatmap
from orrery.tui.views import panel_title
from orrery.tui.week import CURSOR, FG, NAME, NOTHING_PLANNED, Lower, span

RANGES = {"month": 5, "quarter": 13, "half": 26, "year": 53}  # z's, in weeks
PICKED = f"{NAME} {CURSOR}"  # the strip's pick
COLUMNS = ("done", "rate", "study days", "longest run", "extras")
LABEL = 8  # strong, shaky, next
EDGE = 1 + 1 + LABEL  # an evaluation line's edge, space and label, before the mentor's words
NONE_YET = "no evaluations yet"
HINT = "enter: every week's evaluations"


def _parts(a: Assessment) -> list[tuple[str, str]]:
    """The evaluation's three parts, each on one line."""
    parts = ("strong", a.strong), ("shaky", a.shaky), ("next", a.next)
    return [(k, " ".join(v.split())) for k, v in parts]


def _covers(a: Assessment, boundary: int) -> str:
    """The week it covers and the day it was written (by the day boundary)."""
    on = schedule.today(a.at, boundary)
    return f"week of {a.week:%b} {a.week.day}, written {on:%a %b} {on.day}"


def _length(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{m} min" if not h else f"{h} h" + (f" {m:02}" if m else "")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _wrap(s: str, width: int) -> list[Content]:
    """s in the text colour, wrapped to width, so nothing of it is cut."""
    return Content.styled(s, FG).wrap(max(1, width))


def evaluations_text(subject: str, assessed: dict[str, list[Assessment]], boundary: int) -> str:
    """The popup for subject: every week's evaluation, newest first; for all, those of each
    subject with any, by slot."""
    subjects = [s for s, got in assessed.items() if got] if subject == ALL else [subject]
    out = []
    for s in subjects:
        weeks = [
            "\n".join([_covers(a, boundary), *(f"{k:<{LABEL}}{v}" for k, v in _parts(a))])
            for a in assessed.get(s, [])
        ]
        head = f"{s}: every weekly evaluation, in its mentor's words"
        out.append("\n\n".join([head, *(weeks or [NONE_YET])]))
    return "\n\n".join(out) or NONE_YET


def sittings(r: Rhythm) -> str:
    """When sittings start and how long they run, or how many more timed reports that needs."""
    if r.n >= MIN_TIMED:
        return (
            f"Half the sittings start between {r.start_from:%H:%M} and {r.start_to:%H:%M};"
            f" the median one runs {_length(r.median_minutes)}."
        )
    return (
        f"Sitting times show once {MIN_TIMED} reports carry start and end times"
        f" ({MIN_TIMED - r.n} more to go)."
    )


def window(last: date, weeks: int) -> list[date]:
    """The Mondays of the weeks ending with last's."""
    return [last - timedelta(weeks=weeks - 1 - i) for i in range(weeks)]


def dates(first: date, last: date) -> str:
    """The days from first to last's Sunday, e.g. `Nov 9 – Dec 13` or `Nov 11 – 15`, with
    the years when its ends' differ."""
    sunday = last + timedelta(days=6)
    if first == sunday:
        return f"{first:%b} {first.day}"
    if first.year != sunday.year:
        return f"{first:%b} {first.day} {first.year} – {sunday:%b} {sunday.day} {sunday.year}"
    end = f"{sunday.day}" if sunday.month == first.month else f"{sunday:%b} {sunday.day}"
    return f"{first:%b} {first.day} – {end}"


class ReportView(CursorView):
    """Completion: the strip and the heatmap of its pick, and the tab's keys. ↑/↓ cycle the
    pick and wrap; z changes the range, asking the app for the window again (a Shift of 0).
    A read keeps the picked subject wherever the strip puts it, or picks all if it's gone."""

    BINDINGS = keymap.cursor_bindings("report")

    item = reactive(0)  # of the strip
    panel = ("Completion", "share of planned sessions done each day")

    class Shift(CursorView.Shift):
        """Move the window by days; the app keeps it between starts' week and this one."""

    class Open(CursorView.Open):
        """Show target, the pick's evaluations as text, in a popup."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.range = "month"  # z's, read by the app for the window
        self.days: dict[date, dict[str, tuple[int, int]]] | str = ""  # or the line saying why
        self.weeks: list[date] = []
        self.strip = [ALL]
        self.assessed: dict[str, list[Assessment]] = {}
        self.slots: dict[str, int] = {}
        self.starts = date.min
        self.boundary = 5
        self._spans: list[tuple[int, int]] = []  # each strip item's columns

    @property
    def subject(self) -> str:
        """The pick: ALL or a subject."""
        return self.strip[self.item]

    def compose(self) -> ComposeResult:
        yield Static(id="subjects")
        yield Heatmap()

    def on_mount(self) -> None:
        self.app.theme_changed_signal.subscribe(self, lambda _theme: self._paint())

    def show(
        self,
        days,
        last=None,
        weeks=0,
        subjects=(),
        assessed=None,
        slots=None,
        starts=date.min,
        boundary=5,
    ) -> None:
        """days (stats.daily) of the window of weeks ending with last's, the strip's subjects
        and every subject's evaluations; or the line saying why they can't be read."""
        self.days, self.assessed, self.slots = days, assessed or {}, slots or {}
        self.starts, self.boundary = starts, boundary
        was = self.subject
        if isinstance(days, str):  # the strip is kept, so the next read keeps the pick
            self.heading, self.weeks = "", []
        else:
            self.weeks = window(last, weeks)
            self.heading = self._heading(last)
            self.strip = [ALL, *subjects]
        self.item = self.strip.index(was) if was in self.strip else 0  # gone: all
        self._paint()

    def _heading(self, last: date) -> str:
        """The header's controls: the window's weeks, e.g. `‹ last 5 weeks, Nov 9 – Dec 13 ›`
        (last when it ends with this week); before starts, when it is."""
        if self.starts > self.today:
            return f"orrery starts {fmt_day(self.starts)}"
        last_ = "last " if last == schedule.week_start(self.today) else ""
        return f"‹ {last_}{len(self.weeks)} weeks, {dates(self.weeks[0], last)} ›"

    def watch_item(self) -> None:
        self._paint()

    def _paint(self) -> None:
        """The strip, the pick on the cursor's background, and the heatmap of the pick."""
        strip, grid = self.query_one("#subjects", Static), self.query_one(Heatmap)
        if isinstance(self.days, str):
            self._spans = []
            strip.update(Content(self.days))
            grid.show({}, [], date.min, date.min, ALL, {})
            return
        dark = self.app.current_theme.dark
        parts: list[tuple[str, str]] = []
        self._spans, x = [], 0
        for i, s in enumerate(self.strip):
            name = (f" {s} ", PICKED if i == self.item else FG)
            item = [name] if s == ALL else [("▌", hue(self.slots, s, dark)), name]
            width = sum(len(t) for t, _ in item)
            self._spans.append((x, x + width))
            parts += [*item, (" ", "")]
            x += width + 1
        strip.update(Content.assemble(*parts, ("   ↑↓ subject", MUTED)))
        grid.show(self.days, self.weeks, self.starts, self.today, self.subject, self.slots)

    def on_click(self, event: events.Click) -> None:
        """A click anywhere focuses the panel; on the strip, it picks what's under it. A click
        reaches here from the strip at the strip's offset, so the base's content offset won't
        do."""
        event.prevent_default()
        self.focus()
        if event.widget is self.query_one("#subjects"):
            hit = (i for i, (a, b) in enumerate(self._spans) if a <= event.x < b)
            self.item = next(hit, self.item)

    def action_left(self) -> None:
        self.post_message(self.Shift(-7 * RANGES[self.range]))

    def action_right(self) -> None:
        self.post_message(self.Shift(7 * RANGES[self.range]))

    action_earlier, action_later = action_left, action_right  # a period is the window here

    def action_up(self) -> None:
        self._cycle(-1)

    def action_down(self) -> None:
        self._cycle(1)

    def _cycle(self, step: int) -> None:
        if not isinstance(self.days, str):  # no strip on screen, nothing to cycle
            self.item = (self.item + step) % len(self.strip)

    def action_range(self) -> None:
        names = list(RANGES)
        self.range = names[(names.index(self.range) + 1) % len(names)]
        self.post_message(self.Shift(0))

    def action_open(self) -> None:
        if not isinstance(self.days, str):
            self.post_message(
                self.Open(evaluations_text(self.subject, self.assessed, self.boundary))
            )


class YourStats(Lower):
    """For the range: a row each for all and every subject planned in it (sessions done of
    planned, rate, study days, longest run of full ones, extras); then the sittings, the best
    week and this week so far. Its caption: the window's days."""

    LABEL = "Your stats"

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.summary: dict[str, Summary] | None = None
        self.rhythm: Rhythm | None = None
        self.best: tuple[date, int, int] | None = None
        self.now = ""
        self.slots: dict[str, int] = {}
        self.days = ""
        self.need: int | None = None  # the table's width

    def show(self, summary, rhythm, best, now: str, slots, days: str) -> None:
        """summary (stats.range_summary), rhythm and best (stats.best_week) of the window
        of days; now, so far this week's line."""
        self.summary, self.rhythm, self.best, self.now = summary, rhythm, best, now
        self.slots, self.days = slots, days
        # as wide as the table, its border and padding beside it, so no column is cut
        table = self._table(True) if summary["all"].planned else []
        self.need = max((c.cell_length + 4 for c in table), default=None)
        self.fit()
        self._paint()

    def fit(self) -> None:
        """Side by side (-panels), as wide as the table needs; stacked, the pane's width, so
        it never overflows the pane (its rows are cut instead)."""
        self.styles.min_width = self.need if self.screen.has_class("-panels") else None

    def ready(self) -> bool:
        return self.summary is not None

    def caption(self) -> str:
        return self.days

    def body(self, width: int, dark: bool) -> Content:
        planned = self.summary["all"].planned
        rows = self._table(dark) if planned else [Content.styled(NOTHING_PLANNED, MUTED)]
        facts = [sittings(self.rhythm)]
        if self.best is not None and self.best[1]:  # a best week of none done says nothing
            m, done, of = self.best
            week = f"W{m.isocalendar()[1]} ({span(m)})"
            facts.append(f"Best week: {week}, {done} of {of} planned sessions done.")
        rows += [Content(), Content.styled("Sittings", NAME)]
        rows += [line for fact in facts for line in _wrap(fact, width)]
        rows += [Content(), Content.styled("This week", NAME), *_wrap(self.now, width)]
        return Content("\n").join(rows)

    def _table(self, dark: bool) -> list[Content]:
        """The header, muted, then a row each, every column as wide as its widest cell."""
        rows = [("subject", *COLUMNS)] + [
            (k, f"{s.done} of {s.planned}", f"{round(s.done / s.planned * 100)}%")
            + (str(s.study_days), _plural(s.longest_run, "day"), f"+{s.extras}")
            for k, s in self.summary.items()
        ]
        widths = [max(map(len, column)) + 2 for column in zip(*rows, strict=True)]
        lines = ["".join(c.ljust(w) for c, w in zip(row, widths, strict=True)) for row in rows]
        out = [Content.styled(" " + lines[0].rstrip(), MUTED)]
        for k, line in zip(self.summary, lines[1:], strict=True):
            edge = " " if k == ALL else ("▌", hue(self.slots, k, dark))
            out.append(Content.assemble(edge, line.rstrip()))
        return out


class EvaluationsText(Widget):
    """The evaluations, drawn at their own width, so the mentors' words wrap to it and the
    panel scrolls them."""

    def __init__(self) -> None:
        super().__init__()
        self.assessed: dict[str, list[Assessment]] = {}  # newest week first
        self.last = date.min  # the window's last week
        self.slots: dict[str, int] = {}
        self.boundary = 5

    def on_mount(self) -> None:
        self.app.theme_changed_signal.subscribe(self, lambda _theme: self.refresh())

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        """The lines at the width measured for: on a resize, self.size is still the old one."""
        return len(self._lines(width))

    def render(self) -> Content:
        return Content("\n").join(self._lines(self.size.width))

    def _lines(self, width: int) -> list[Content]:
        """Per subject, its newest evaluation of a week up to the window's last: the week it
        covers and when it was written, then strong, shaky and next in the mentor's words."""
        dark = self.app.current_theme.dark
        latest = {
            s: a
            for s, got in self.assessed.items()
            if (a := next((a for a in got if a.week <= self.last), None))
        }
        name = max(map(len, latest), default=0) + 2
        out: list[Content] = []
        for s, a in latest.items():
            h = hue(self.slots, s, dark)
            if out:
                out.append(Content())
            edge = Content.styled("▌", h)
            first = edge + Content.styled(s.ljust(name), NAME)
            covers = Content.styled(_covers(a, self.boundary), MUTED)
            out += para(first, edge + " " * name, covers, width)
            for label, said in _parts(a):
                first = edge + Content.styled(f" {label:<{LABEL}}", MUTED)
                out += para(first, edge + " " * (EDGE - 1), Content.styled(said, FG), width)
        return out or [Content.styled(NONE_YET, MUTED)]


class Evaluations(VerticalScroll, can_focus=False):
    """Mentor evaluations, enter's hint in the bottom border. Never focused, so the keys stay
    with the strip; the wheel scrolls it when it's taller than its place."""

    CAPTION = "the newest, in each mentor's words"

    def compose(self) -> ComposeResult:
        yield EvaluationsText()

    def show(self, assessed, last: date, slots: dict[str, int], boundary: int) -> None:
        """Every subject's evaluations, newest week first, up to the window's last week."""
        t = self.query_one(EvaluationsText)
        t.assessed, t.last, t.slots, t.boundary = assessed, last, slots, boundary
        t.refresh(layout=True)

    def on_resize(self) -> None:
        self.border_title = panel_title("Mentor evaluations", self.CAPTION, self.outer_size.width)
        self.border_subtitle = HINT


class ReportPane(VerticalScroll, can_focus=False):
    """The Report tab's pane: Completion on top, then the lower row (your stats beside the
    evaluations), scrolled when taller than the window. Never focused itself, so a click
    outside the strip leaves the keys with it."""
