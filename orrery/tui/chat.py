"""The organizer chat, beside every tab: a panel titled Organizer, since when this chat has run
on the right of its top border, holding every session's transcript (older pages read as you
scroll up, a divider where each chat began), the turn in progress, the waiting proposal's
diff, the last key's line, and the input.

The transcript reads as messages: yours and the organizer's each its speaker on a line, then
its text two columns in, wrapped to the panel; orrery's own lines `· <text>`, muted, together
with no gap; a blank line between the rest. While a turn runs, the organizer's latest words
show muted after `… ` until its reply replaces them.

From 110 columns it is docked on the right, 30% of the window from 37 to 72 columns wide.
Narrower it folds to a one-line strip at the bottom (the newest message's speaker and first
line), and c opens it over the content until esc. Transcript text reaches the screen as plain
Content, never parsed as markup; only an organizer row's ** pairs are read, as bold spans built
by hand.
"""

import re
from collections.abc import Callable, Sequence
from datetime import date
from functools import lru_cache

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.geometry import Size
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, Static

from orrery.organizer.sessions import Line
from orrery.proposals import DiffLine
from orrery.text import fmt_day
from orrery.tui import keymap
from orrery.tui.day import para
from orrery.tui.diff import MUTED, chat_bar, render_diff
from orrery.tui.themes import ThemePicker
from orrery.tui.views import panel_title

PAGE = 100  # transcript rows read at a time
TITLE = "Organizer"
STARTED = "new chat started"  # every session's first row: shown as its divider
WAITING = "proposal waiting: c shows it"
HINT = "press c, then ask orrery to move, add or report a session"
_HEAD = {"you": "bold $text-accent", "organizer": "bold $text-primary"}
INDENT = Content("  ")


def _day(at: str) -> str:
    return fmt_day(date.fromisoformat(at[:10]))


def _divider(line: Line) -> bool:
    return line.speaker == "orrery" and line.text == STARTED and bool(line.at)


# a **span** outside any word, its text starting and ending with neither space nor *
# ASCII word characters only: next to CJK text a pair is still bold (\w would count 粗 as a word)
_WORD = "A-Za-z0-9_"
_BOLD = re.compile(rf"(?<![{_WORD}])\*\*(?=[^\s*])(.+?)(?<=[^\s*])\*\*(?![{_WORD}])")
# any other **, unless inside a word (x**2)
_STRAY = re.compile(rf"(?<![{_WORD}])\*\*|\*\*(?![{_WORD}])")


def _bold(text: str) -> Content:
    """An organizer's text, each **pair**'s span bold and its markers gone; an unpaired **
    goes too, but not one inside a word. Spans built by hand: nothing else in the text is
    read, never as markup."""
    parts = _BOLD.split(text)  # plain, bold, plain, ...
    return Content.assemble(
        *((p, "bold") if i % 2 else _STRAY.sub("", p) for i, p in enumerate(parts))
    )


def _dot(line: Line | None) -> bool:
    """orrery's own line, drawn `· <text>`: not a divider."""
    return line is not None and line.speaker == "orrery" and not _divider(line)


@lru_cache(maxsize=4096)  # each layout measures every row, and a streamed reply lays out often
def render_line(line: Line, width: int, live: bool = False) -> tuple[Content, ...]:
    """A transcript row's lines at width: a chat's first row as its divider; orrery's as
    `· <text>`, muted; yours and the organizer's as the speaker, then the text two columns in
    (the organizer's ** pairs bold). live: the turn in progress, its latest words muted after
    `… `. Wrapped lines hang under the text. Theme tokens, so a cached row follows the theme."""
    if _divider(line):
        return (Content.styled(f"── new chat, {_day(line.at)} ──", MUTED),)
    text = line.text.strip()
    if line.speaker == "orrery":
        dot = Content.styled("· ", MUTED)
        return tuple(para(dot, INDENT, Content.styled(text, MUTED), width))
    first, body = INDENT, _bold(text) if line.speaker == "organizer" else Content(text)
    if live:
        first, body = Content.styled("  … ", MUTED), body.stylize_before(MUTED)
    head = Content.styled(line.speaker, _HEAD[line.speaker])
    return (head, *para(first, Content(" " * first.cell_length), body, width))


def summary(line: Line, live: bool = False) -> str:
    """A row on one line, as the strip shows it: its speaker and its text's first line, with
    … if more follow; a divider as drawn."""
    if _divider(line):
        return render_line(line, 0)[0].plain
    text = line.text.strip()
    first, _, more = (_bold(text).plain if line.speaker == "organizer" else text).partition("\n")
    return f"{line.speaker}: {'… ' if live else ''}{first}{' …' if more else ''}"


def _tight(prev: Line | None, line: Line) -> bool:
    """No blank line above line: the log's first row, or orrery's line after another."""
    return prev is None or (_dot(prev) and _dot(line))


class _Row(Widget):
    """A transcript row (live: the turn in progress), drawn at its own width, so it wraps to
    the panel's; a blank line above it unless it's tight."""

    def __init__(self, line: Line, tight: bool = False, live: bool = False, **kw) -> None:
        super().__init__(classes="row -tight" if tight else "row", **kw)
        self.line, self.live = line, live

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        """The lines at the width measured for: on a resize, self.size is still the old one."""
        return len(render_line(self.line, width, self.live))

    def render(self) -> Content:
        return Content("\n").join(render_line(self.line, self.size.width, self.live))


def _rows(lines: Sequence[Line], prev: Line | None) -> list[_Row]:
    """A row per line, oldest first, the first after prev (None: at the top)."""
    out = []
    for ln in lines:
        out.append(_Row(ln, _tight(prev, ln)))
        prev = ln
    return out


class _Proposal(Static):
    """The waiting proposal's diff with its y/n bar, painted at its width once it has one."""

    diff: tuple[DiffLine, ...] = ()

    def show(self, diff: tuple[DiffLine, ...]) -> None:
        self.diff = diff
        self.display = bool(diff)
        self._paint()

    def on_resize(self) -> None:
        self._paint()

    def _paint(self) -> None:
        if not self.diff:
            self.update("")
            return
        bar = chat_bar(keymap.footer("proposal"), self.content_size.width)
        self.update(Content("\n").join([render_diff(list(self.diff)), bar]))


class _Log(VerticalScroll, can_focus=False):
    """The transcript, oldest at the top: a _Row per row, then the turn in progress, the hint
    while nothing but dividers was said, and the waiting proposal. Anchored to the
    bottom; scrolled to the top, it reads the page before and keeps the rows you were reading
    where they were. Rows too few to scroll read the pages before until they fill it."""

    def __init__(self, older: Callable[[int], Sequence[Line] | None], **kw) -> None:
        super().__init__(**kw)
        self.older = older  # the page of rows before an id, newest first; None if unreadable
        self.oldest = self.newest = 0  # the ids shown; 0 while none are
        self.more = False  # rows older than oldest may exist
        self.pending: list[_Row] = []  # rows said just now, until the database has them
        self.said = False  # a row shown is more than a divider

    def compose(self) -> ComposeResult:
        yield _Row(Line("", "organizer", ""), live=True, id="live")
        yield Static(Content.styled(HINT, MUTED), id="hint")
        yield _Proposal(id="proposal")

    def on_mount(self) -> None:
        self.anchor()

    def on_resize(self) -> None:
        self.fill()  # shown at last (c on a strip, the frame back), or taller

    def heard(self, lines: Sequence[Line]) -> None:
        """lines are shown: the hint shows while the rows shown are nothing but dividers."""
        self.said = self.said or not all(map(_divider, lines))
        self.query_one("#hint").display = bool(self.newest) and not (self.said or self.pending)

    def watch_scroll_y(self, old: float, new: float) -> None:
        super().watch_scroll_y(old, new)
        if new == 0 and old > 0:
            self._read_more()

    def fill(self) -> None:
        """With nothing to scroll there's no scrolling up to the older rows: read them. Only
        while on screen: a hidden log (folded to the strip, or the frame too small) is never
        laid out, so it would read every page."""
        if self.region.height and self.max_scroll_y == 0:
            self._read_more()

    def _read_more(self) -> None:
        if self.more:
            self.more = False  # one read at a time
            self.call_later(self._read_older)

    async def _read_older(self) -> None:
        page = self.older(self.oldest)
        if page is None:  # unreadable just now: the next scroll to the top tries again
            self.more = True
            return
        self.more = len(page) == PAGE
        if not page:
            return
        self.oldest = page[-1].id
        self.heard(page)
        top = self.children[0]
        if isinstance(top, _Row):  # no longer the first: a blank line above it, unless tight
            top.set_class(_tight(page[0], top.line), "-tight")
        await self.mount_all(_rows(page[::-1], None), before=0)

        def back() -> None:  # laid out by now: the row that was at the top goes back there
            self.scroll_to(y=top.virtual_region.y, animate=False)
            self.fill()

        self.call_after_refresh(back)


class ChatInput(Input):
    """The chat's line, always shown, focused by c or a click. Letters type here and never
    reach the app's keys; up and down stop here too, so they never move a view. esc and
    enter act."""

    def on_focus(self) -> None:
        """One widget owns the keyboard: a theme preview left open behind the input could
        never be put back, so the picker is cancelled, however the input got the focus."""
        picker = self.app.query_one(ThemePicker)
        if picker.is_open:
            picker.cancel()

    def on_key(self, event) -> None:
        if event.key in ("up", "down"):
            event.stop()
            event.prevent_default()


class ChatPanel(Vertical):
    """The chat. The app feeds it (show, append, drop_pending, stream, note, show_proposal)
    and hears from it only Submitted."""

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, older: Callable[[int], Sequence[Line] | None], **kw) -> None:
        super().__init__(**kw)
        self._older = older
        self._note = ""
        self._live: str | None = None
        self._last: Line | None = None  # the newest row: the strip's, and the next row's prev
        self._saved: Line | None = None  # the newest row the database has
        self._since = ""  # the panel's caption: since when the active chat has run

    def compose(self) -> ComposeResult:
        yield Static(id="strip", markup=False)
        with Vertical(id="chat-body", classes="panel"):
            yield _Log(self._older, id="log")
            yield Static(id="note", markup=False)
            yield ChatInput(id="chat-input", placeholder="› message orrery", select_on_focus=False)

    def show(self, lines: Sequence[Line], since: str) -> None:
        """The newest page of the transcript, oldest first, and when the active chat began:
        rows newer than those shown are added below them, and replace the rows said just
        now. A page that skips rows, or is older than what's shown, starts the log over."""
        self._since = f"since {_day(since)}" if since else ""
        self._title()
        log = self.query_one(_Log)
        new = [ln for ln in lines if ln.id > log.newest]
        prev = self._saved
        if lines and (lines[-1].id < log.newest or (log.newest and len(new) == PAGE)):
            log.query(".row").exclude("#live").remove()
            log.pending.clear()
            log.newest, log.said, new, prev = 0, False, list(lines), None
        if not new:
            return
        self.drop_pending()
        if not log.newest:
            log.oldest, log.more = new[0].id, len(lines) == PAGE
        log.newest = new[-1].id
        log.mount_all(_rows(new, prev), before="#live")
        log.heard(new)
        log.call_after_refresh(log.fill)
        self._last = self._saved = new[-1]
        self._paint_strip()

    def on_resize(self) -> None:
        self._title()

    def _title(self) -> None:
        """The panel's title and caption, drawn at its width (the body's: it has no frame)."""
        body = self.query_one("#chat-body")
        body.border_title = panel_title(TITLE, self._since, self.size.width)

    def drop_pending(self) -> None:
        """The rows said just now go: the database has them, or, the turn over, never will
        (the organizer was busy in another window, say), so they were never sent."""
        log = self.query_one(_Log)
        for row in log.pending:
            row.remove()
        log.pending.clear()
        log.heard([])
        self._last = self._saved
        self._paint_strip()

    def append(self, line: Line) -> None:
        """A row said just now, shown at the bottom until the database has it."""
        log = self.query_one(_Log)
        row = _Row(line, _tight(self._last, line))
        log.pending.append(row)
        log.heard([])
        log.mount(row, before="#live")
        log.scroll_end(animate=False)
        self._last = line
        self._paint_strip()

    def stream(self, text: str | None) -> None:
        """The organizer's latest words (or the working line), under the transcript; None
        clears them."""
        self._live = text
        live = self.query_one("#live", _Row)
        live.line = Line("", "organizer", text or "")
        live.display = text is not None
        live.refresh(layout=True)
        self._paint_strip()

    def note(self, text: str) -> None:
        """The last key's one line: kept, undone, why not; "" clears it."""
        self._note = text
        line = self.query_one("#note", Static)
        line.update(text)
        line.display = bool(text)
        self._paint_strip()

    def show_proposal(self, diff: tuple[DiffLine, ...] | None) -> None:
        self.query_one(_Proposal).show(diff or ())
        self._paint_strip()

    def focus_input(self) -> None:
        """The keyboard to the input (which cancels a theme preview), the chat opened over
        the content if it's a strip."""
        if self.screen.has_class("-narrow"):
            self.add_class("-overlay")
        self.query_one(ChatInput).focus()

    def _paint_strip(self) -> None:
        if self.query_one(_Proposal).diff:
            line = WAITING
        elif self._note:
            line = self._note
        elif self._live is not None:
            line = summary(Line("", "organizer", self._live), live=True)
        else:
            line = summary(self._last) if self._last else ""
        self.query_one("#strip", Static).update(line)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.post_message(self.Submitted(event.value))
