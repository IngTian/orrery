"""The Report's completion heatmap: weeks across, Mon to Sun down, two columns a day (one,
where two don't fit), the months over the weeks they start, and a legend on the right (under
the grid, where it doesn't fit beside it).

A day from starts to today with blocks planned is `■` shaded by its rate, the sessions done of
those planned (capped per block, so extras never raise it) over the subject's blocks or, for
all, every subject's; `□` when none was done; `·` with none planned; blank before starts and
after today. The shade has 4 levels (`level`), mixed from the background toward the subject's
hue (past the sixth slot, the muted text), or for all toward the text colour (`ramp`), again at
every theme change.

Pure rendering: it draws the days it's shown (`stats.daily`), never reads them.
"""

from datetime import date, timedelta

from textual.color import Color
from textual.content import Content
from textual.geometry import Size
from textual.style import Style
from textual.widget import Widget

from orrery.text import fmt_day
from orrery.tui.cursor import hue
from orrery.tui.diff import MUTED

ALL = "all"
GUTTER = 5  # the day names' column
GAP = 6  # between the grid and the legend beside it
MIX = (0.30, 0.55, 0.80, 1.0)  # each level's share of the way from the background to the hue
DAYS = {0: "Mon", 2: "Wed", 4: "Fri", 6: "Sun"}


def level(done: int, planned: int) -> int:
    """0 if planned and nothing done; else the quarter the rate is in: ≤25% 1, ≤50% 2, ≤75%
    3, more 4."""
    return 0 if not done else min(4, max(1, -(-4 * done // planned)))


def ramp(hex: str, background: str, level: int) -> str:
    """The shade of level 1 to 4: hex mixed into background at MIX's share."""
    return Color.parse(background).blend(Color.parse(hex), MIX[level - 1]).hex


def _solid(c: Color, white: bool) -> str:
    """c, or white or black for an ANSI theme's terminal default, which can't be mixed."""
    return c.hex if c.ansi is None and c.a else "#ffffff" if white else "#000000"


class Heatmap(Widget):
    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.days: dict[date, dict[str, tuple[int, int]]] = {}
        self.weeks: list[date] = []  # the columns' Mondays
        self.starts = self.today = date.min
        self.subject = ALL
        self.slots: dict[str, int] = {}

    def on_mount(self) -> None:
        self.app.theme_changed_signal.subscribe(self, lambda _theme: self.refresh())

    def show(self, days, weeks: list[date], starts: date, today: date, subject: str, slots) -> None:
        """days (stats.daily) in the columns of weeks, for subject (or ALL)."""
        self.days, self.weeks, self.starts, self.today = days, weeks, starts, today
        self.subject, self.slots = subject, slots
        self.refresh(layout=True)

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return len(self._lines(width))

    def render(self) -> Content:
        return Content("\n").join(self._lines(self.size.width))

    def _shades(self) -> list[str]:
        """Levels 1 to 4 in the theme on screen."""
        dark = self.app.current_theme.dark
        bg = _solid(self.background_colors[1], white=not dark)
        if self.subject == ALL:
            toward = _solid(Color.parse(self.app.get_css_variables()["foreground"]), white=dark)
        else:
            toward = hue(self.slots, self.subject, dark)
            if toward == MUTED:  # the seventh slot on: the muted text, solid over bg
                muted = self.background_colors[1] + Style.parse(MUTED).foreground
                toward = _solid(muted, white=dark)
        return [ramp(toward, bg, k) for k in (1, 2, 3, 4)]

    def _lines(self, width: int) -> list[Content]:
        if self.starts > self.today:
            return [Content.styled(f"orrery starts {fmt_day(self.starts)}", MUTED)]
        if not self.weeks:
            return []
        cell = 2 if GUTTER + 2 * len(self.weeks) <= width else 1
        shades = self._shades()
        rows = [self._months(cell)]
        for i in range(7):
            cells = [self._cell(m + timedelta(days=i), shades) for m in self.weeks]
            rows.append(
                Content.assemble(
                    (DAYS.get(i, "").ljust(GUTTER), MUTED),
                    *((glyph.ljust(cell), style) for glyph, style in cells),
                )
            )
        legend = [
            Content.assemble(("less ", MUTED), *(("■ ", s) for s in shades), ("more", MUTED)),
            Content(),
            Content.styled("□ planned, none done", MUTED),
            Content.styled("· nothing planned", MUTED),
        ]
        grid = GUTTER + cell * len(self.weeks)
        if grid + GAP + max(x.cell_length for x in legend) > width:
            return [*rows, Content(), *legend]
        for i, line in enumerate(legend, 1):
            rows[i] = rows[i] + " " * GAP + line
        return rows

    def _cell(self, d: date, shades: list[str]) -> tuple[str, str]:
        if not self.starts <= d <= self.today:
            return " ", ""
        got = self.days.get(d, {})
        if self.subject == ALL:
            x = tuple(map(sum, zip(*got.values(), strict=True))) if got else None
        else:
            x = got.get(self.subject)
        if x is None:
            return "·", MUTED
        k = level(*x)
        return ("■", shades[k - 1]) if k else ("□", MUTED)

    def _months(self, cell: int) -> Content:
        """Each month's name over the first week starting in it; a later one that would run
        into it goes in its place."""
        at: list[tuple[int, str]] = []
        for i, m in enumerate(self.weeks):
            if i == 0 or m.month != self.weeks[i - 1].month:
                x = GUTTER + i * cell
                if at and at[-1][0] + 4 > x:
                    at.pop()
                at.append((x, f"{m:%b}"))
        line = ""
        for x, name in at:
            line = line.ljust(x) + name
        return Content.styled(line, MUTED)
