"""The Week tab's day panel, always on the cursor's day: a ribbon of the sitting day, from the
day boundary to the same hour the next morning, placing each timed sitting in its subject's
hue; each block with its pips and topic, then each report filed for it, its times and
sessions and the mentor's note wrapped in full; the day's deadlines; then what the chat kept
and noted that day.

It has no cursor and is never focused: the app shows it the cursor's day (`DayPanel.show`) and
the mouse wheel scrolls it. enter on a week's block opens that block's reports and agenda in a
popup (`block_at`, `block_text`). Before starts there are no blocks, and one note says so.
Drawn in the week view's styles and marks.
"""

import math
from dataclasses import dataclass
from datetime import date, datetime

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.content import Content
from textual.geometry import Size
from textual.widget import Widget

from orrery.board import Cell, DayBoard, WeekBoard
from orrery.stats import SessionRow
from orrery.text import fmt_day, phrase
from orrery.tui.cursor import hue
from orrery.tui.diff import MUTED, para
from orrery.tui.views import panel_title
from orrery.tui.week import BEFORE, FG, GROUPS, NAME, STRUCK, edged, pips, short_day

GUTTER = 7  # the part names' column
HOURS = 24  # the ribbon: the sitting day, from the day boundary
NOTHING = "nothing kept or noted"
UNREPORTED = "nothing reported"


def title(d: date, today: date) -> str:
    """The panel's title: the day, e.g. `Mon 9 November`, with its year when that isn't
    today's."""
    return f"{d:%a} {d.day} {d:%B}" + (f" {d.year}" if d.year != today.year else "")


@dataclass(frozen=True)
class Block:
    """One of the day's blocks: its planned cell (None for sessions reported off the plan),
    the reports filed for its subject and kind, the sessions past the plan, and its subject's
    deadlines that day."""

    date: date
    part: str  # a GROUPS key
    subject: str
    kind: str
    cell: Cell | None
    rows: tuple[SessionRow, ...]
    extra: int
    due: tuple[str, ...]


def blocks(day: DayBoard, rows: tuple[SessionRow, ...]) -> list[Block]:
    """day's blocks in drawing order, then one per subject and kind reported off the plan.
    The reports and the sessions past the plan go with the first block of their kind."""
    extra = {(e.subject, e.kind): e.done for e in day.extras}
    due: dict[str, list[str]] = {}
    for x in day.deadlines:  # board's "<subject>: <item>"
        subject, _, item = x.partition(": ")
        due.setdefault(subject, []).append(item)
    keys = [(c.part, (c.subject, c.kind), c) for c in day.cells]
    planned = {key for _, key, _ in keys}
    reported = dict.fromkeys((r.subject, r.kind) for r in rows)
    keys += [("extra", key, None) for key in reported if key not in planned]
    out, seen = [], set()
    for part, key, cell in keys:
        first = key not in seen
        seen.add(key)
        mine = tuple(r for r in rows if (r.subject, r.kind) == key) if first else ()
        more = extra.get(key, 0) if first else 0
        out.append(Block(day.date, part, *key, cell, mine, more, tuple(due.get(key[0], ()))))
    return out


def block_at(day: DayBoard, rows: tuple[SessionRow, ...], item: int) -> Block | None:
    """The block the week view draws as item: a cell's own (the cells come first, in order),
    or for an extra (drawn after them) the first block of its subject and kind, which holds
    its reports."""
    out = blocks(day, rows)
    if item < len(day.cells):
        return out[item]
    if item - len(day.cells) >= len(day.extras):
        return None
    e = day.extras[item - len(day.cells)]
    return next((b for b in out if (b.subject, b.kind) == (e.subject, e.kind)), None)


def sitting(r: SessionRow) -> tuple[str, str]:
    """A report as its times and session count, and its note."""
    times = f"{r.started_at:%H:%M}–{r.ended_at:%H:%M}  " if r.started_at and r.ended_at else ""
    count = f"{r.sessions} session" + ("" if r.sessions == 1 else "s")
    return times + count + (", partial" if r.status == "partial" else ""), r.note


def block_text(b: Block) -> str:
    """The popup for b: what's planned, its topic, moves and deadlines, then every report in
    full."""
    c = b.cell
    n = c.sessions if c else b.extra
    lines = [f"{fmt_day(b.date)} {GROUPS[b.part]}: {phrase(b.subject, b.kind, n)}"]
    if c and c.topic:
        lines.append(f"topic: {c.topic}")
    if c and c.struck:
        to = f" → {fmt_day(c.moved_to)}" if c.moved_to else ""
        lines.append(f"moved: {c.struck}{to}")
    if c and b.extra:
        lines.append(f"+{b.extra} extra")
    lines += [f"due: {x}" for x in b.due]
    reports = [s + (f" · {note}" if note else "") for s, note in map(sitting, b.rows)]
    return "\n".join([*lines, "", *(reports or ["no reports filed"])])


class DayText(Widget):
    """The day panel's text, drawn at its own width, so the notes wrap to it and the panel
    scrolls it."""

    def __init__(self) -> None:
        super().__init__()
        self.day: DayBoard | None = None  # None: nothing to draw (the data can't be read)
        self.today = date.min
        self.rows: tuple[SessionRow, ...] = ()
        self.changes: tuple[tuple[datetime, str], ...] = ()
        self.blocks: list[Block] = []
        self.slots: dict[str, int] = {}
        self.starts = date.min
        self.boundary = 5  # the ribbon's first hour: settings' day_boundary_hour
        self.per = 4  # the ribbon's cells an hour

    def on_mount(self) -> None:
        # Tokens follow the theme by themselves; the subject hues are picked per theme here.
        self.app.theme_changed_signal.subscribe(self, lambda _theme: self.refresh())

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        """The lines at the width measured for: on a resize, self.size is still the old one."""
        return len(self._lines(width))

    def render(self) -> Content:
        return Content("\n").join(self._lines(self.size.width))

    def _lines(self, width: int) -> list[Content]:
        if self.day is None:
            return []
        self.per = max(1, min(4, (width - GUTTER) // HOURS))
        dark = self.app.current_theme.dark
        pad = " " * GUTTER
        out = [
            Content.styled("When", NAME),
            Content.assemble(pad, *self._ribbon(dark)),
            Content.assemble(pad, (self._hours(), MUTED)),
            Content(),
        ]
        if self.day.date < self.starts:
            out.append(Content.assemble(pad, (BEFORE, MUTED)))
        else:
            out += self._blocks(dark, width)
        out += [Content(), Content.styled("That day in the chat", NAME)]
        for at, line in self.changes:
            out += para(Content.styled(f"{at:%H:%M}  ", MUTED), Content(pad), Content(line), width)
        if not self.changes:
            out.append(Content.styled(NOTHING, MUTED))
        return out

    def _ribbon(self, dark: bool) -> list[tuple[str, str]]:
        """A cell per 1/per hour from the day boundary: the timed sittings in their subjects'
        hues, the rest a muted track. A sitting starts at its clock time after the boundary
        (one past midnight goes after the evening, as in stats.rhythm) and runs as long as it
        ran; past the next morning's boundary it's cut off."""
        cells: list[str | None] = [None] * (HOURS * self.per)
        for r in self.rows:
            if r.started_at and r.ended_at:
                t = r.started_at
                a = ((t.hour - self.boundary) % 24 + t.minute / 60) * self.per
                b = a + (r.ended_at - t).total_seconds() / 3600 * self.per
                for x in range(math.floor(a), min(len(cells), math.ceil(b))):
                    cells[x] = hue(self.slots, r.subject, dark)
        return [("█", h) if h else ("┄", MUTED) for h in cells]

    def _hours(self) -> str:
        """Every third hour under the ribbon, at its place: 06 09 … 00 03 from 05:00."""
        labels = ""
        for i in range(HOURS):
            if (h := (self.boundary + i) % 24) % 3 == 0:
                labels = labels.ljust(i * self.per) + f"{h:02}"
        return labels

    def _blocks(self, dark: bool, width: int) -> list[Content]:
        """Each block after its part's name, a blank line between blocks: its head, its topic,
        then each report's times and sessions and its note; a planned block with none, on a
        day gone by or today, says so. Then the deadlines."""
        out: list[Content] = []
        pad = " " * GUTTER
        for b in self.blocks:
            h = hue(self.slots, b.subject, dark)
            under = Content.assemble(pad, ("▌", h), " ")
            if out:
                out.append(Content())
            head = Content.assemble((GROUPS[b.part].ljust(GUTTER), MUTED), self._head(b, h))
            out.append(head.truncate(width, ellipsis=True))
            if b.cell is not None and b.cell.topic:
                out += para(under, under, Content.styled(b.cell.topic, MUTED), width)
            for r in b.rows:
                count, note = sitting(r)
                out += para(under, under, Content.styled(count, MUTED), width)
                if note:
                    out += para(under, under, Content.styled(note, FG), width)
            if not b.rows and b.cell is not None and b.cell.sessions and b.date <= self.today:
                out.append(Content.assemble(under, (UNREPORTED, MUTED)))
        if not self.blocks:
            out.append(Content.assemble((GROUPS["main"].ljust(GUTTER), MUTED), ("rest", MUTED)))
        for j, x in enumerate(self.day.deadlines):  # board's "<subject>: <item>"
            subject, _, item = x.partition(": ")
            edge = ("▌", hue(self.slots, subject, dark))
            label = (GROUPS["due"] if j == 0 else "").ljust(GUTTER)
            if j == 0:
                out.append(Content())
            out += para(
                Content.assemble((label, MUTED), edge),
                Content.assemble(pad, edge),
                Content(item),
                width,
            )
        return out

    def _head(self, b: Block, h: str) -> Content:
        """The subject, its kind (not when it's the subject) and pips, the sessions past the
        plan, and where moved sessions went."""
        c = b.cell
        gone = c is not None and c.sessions == 0
        parts: list[tuple[str, str] | str] = [(b.subject, STRUCK if gone else NAME)]
        if b.kind != b.subject:
            parts += [" ", (b.kind, STRUCK if gone else FG)]
        if c is not None:
            parts += [" ", *pips(c, h)]
        if b.extra:
            parts.append((f" +{b.extra} extra", MUTED))
        if c is not None and c.moved_to:
            parts.append((f"  → {short_day(c.moved_to)}", MUTED))
        return edged(h, *parts)


class DayPanel(VerticalScroll, can_focus=False):
    """The cursor's day, under the top panel: titled with the day, `today` or `the selected
    day` on the right of its border. Never focused, so the keys stay with the view; the
    wheel scrolls it when it's taller than its place."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.panel = ("", "")
        self.day: date | None = None

    def compose(self) -> ComposeResult:
        yield DayText()

    def show(
        self,
        day: date,
        board: WeekBoard | None,
        sessions: tuple[SessionRow, ...],
        changes: tuple[tuple[datetime, str], ...],
        slots: dict[str, int],
        starts: date = date.min,
        boundary: int = 5,
    ) -> None:
        """day, from its week's board (None while the data can't be read: the title alone);
        nothing reported before starts; its ribbon from boundary (settings' day_boundary_hour).
        Another day than the one shown starts at the top."""
        t = self.query_one(DayText)
        t.day = next((x for x in board.days if x.date == day), None) if board else None
        t.today = board.today if board else day
        t.rows = () if day < starts else tuple(sessions)
        t.blocks = blocks(t.day, t.rows) if t.day is not None and day >= starts else []
        t.changes, t.slots, t.starts, t.boundary = tuple(changes), slots, starts, boundary
        self.panel = (
            title(day, t.today),
            "today" if board and day == board.today else "the selected day",
        )
        self._title()
        t.refresh(layout=True)
        if day != self.day:
            self.day = day
            self.scroll_home(animate=False)

    def on_resize(self) -> None:
        self._title()

    def _title(self) -> None:
        self.border_title = panel_title(*self.panel, self.outer_size.width)
