"""The cursor views' base: one focused item, moved by the view's keys (its tab's scope in the
keymap: WeekTabView's are the Week tab's, ReportView's the Report's), and through time at the
edges.

A view never reads the database. It tells the app how far to move the shared date
(`Shift`), which of the Week tab's views to show on it (`Switch`: w, m, and enter on a
month's day) or what to open (`Open`), and the app sets `focus_date` back, reading another
period first if the date left the one on screen. A view in a .panel names it in its top
border (`panel`: the title and the caption).
"""

from datetime import date

from textual import events
from textual.geometry import Region
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget

from orrery import palette
from orrery.tui import keymap
from orrery.tui.diff import MUTED
from orrery.tui.views import panel_title


def hue(slots: dict[str, int], subject: str, dark: bool) -> str:
    """The subject's mark colour: its slot's hue, or muted from the seventh slot on."""
    return palette.hex_for(slots.get(subject, len(palette.DARK)), dark) or MUTED


class CursorView(Widget, can_focus=True):
    focus_date: reactive[date] = reactive(date.min)
    today = date.min  # the app's, set at every read that knows it, for t
    heading = ""  # the header's controls for the period drawn, set with the data
    panel = ("", "")  # the title and caption in its .panel's top border

    class Shift(Message):
        """Move the shared date by days."""

        def __init__(self, days: int) -> None:
            super().__init__()
            self.days = days

    class Switch(Message):
        """Show the Week tab's week or month view on the shared date."""

        def __init__(self, view: str) -> None:
            super().__init__()
            self.view = view

    class Open(Message):
        """Open target: a block, a subject, by the view."""

        def __init__(self, target: object) -> None:
            super().__init__()
            self.target = target

    def on_mount(self) -> None:
        # Tokens follow the theme by themselves; the subject hues are picked per theme here.
        self.app.theme_changed_signal.subscribe(self, lambda _theme: self.refresh())

    def on_resize(self) -> None:
        self._title()

    def _title(self) -> None:
        """The panel's title and caption, drawn at its width."""
        self.border_title = panel_title(*self.panel, self.outer_size.width)

    def on_click(self, event: events.Click) -> None:
        self.focus()
        if (offset := event.get_content_offset(self)) is not None:
            self.pick(offset.x, offset.y)

    def pick(self, x: int, y: int) -> None:
        """Focus the item drawn at x, y of the content; nothing there, nothing."""

    def watch_item(self) -> None:
        """A view with items (the week's blocks; the Report's strip draws its own) keeps
        the focused one in sight in its scrolled pane, once the redraw has placed it."""
        self.call_after_refresh(self._reveal)

    def item_rows(self) -> list[int]:
        """The content rows the focused item was last drawn on."""
        return []

    def _reveal(self) -> None:
        """Scroll the pane, up or down, the least that shows the whole of the focused item's
        last run of rows (a block). On the first item, home if that shows it: what's above it
        (the week's day names) is reached from the keyboard too."""
        if not self.display:
            return
        rows = set(self.item_rows())
        y = self.virtual_region.y + self.gutter.top
        if self.item == 0 and (not rows or y + min(rows) < self.parent.size.height):
            self.parent.scroll_home(animate=False)
            return
        if not rows:
            return
        end = start = max(rows)
        while start - 1 in rows:
            start -= 1
        top = y + start
        self.parent.scroll_to_region(Region(0, top, 1, end - start + 1), animate=False)

    def action_today(self) -> None:
        self.post_message(self.Shift((self.today - self.focus_date).days))


class WeekTabView(CursorView):
    """The Week tab's views, the week and the month: its keys, w and m switching between
    them."""

    BINDINGS = keymap.cursor_bindings("week")

    def action_week(self) -> None:
        self.post_message(self.Switch("week"))

    def action_month(self) -> None:
        self.post_message(self.Switch("month"))
