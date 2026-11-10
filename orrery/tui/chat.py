"""The organizer chat, beside every tab: a panel titled Organizer, since when this chat has run
on the right of its top border, holding every session's transcript (older pages read as you
scroll up, a divider where each chat began), the reply streaming in, the waiting proposal's
diff, the last key's line, and the input.

From 110 columns it is docked on the right, 30% of the window from 37 to 72 columns wide.
Narrower it folds to a one-line strip at the bottom, and c opens it over the content until
esc. Transcript text reaches the screen as plain Content, never parsed as markup; only an
organizer row's ** pairs are read, as bold spans built by hand.
"""

import re
from collections.abc import Callable, Sequence
from datetime import date

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.widgets import Input, Static

from orrery.organizer.sessions import Line
from orrery.proposals import DiffLine
from orrery.text import fmt_day
from orrery.tui import keymap
from orrery.tui.diff import MUTED, chat_bar, render_diff
from orrery.tui.themes import ThemePicker
from orrery.tui.views import panel_title

PAGE = 100  # transcript rows read at a time
TITLE = "Organizer"
STARTED = "new chat started"  # every session's first row: shown as its divider
WAITING = "proposal waiting: c shows it"
HINT = "press c, then ask orrery to move, add or report a session"
_STYLE = {"you": "bold", "orrery": MUTED}


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


def _reply(text: str) -> Content:
    """An organizer row, each **pair**'s span bold and its markers gone; an unpaired ** goes
    too, but not one inside a word. Spans built by hand: nothing else in the text is read,
    never as markup."""
    parts = _BOLD.split(text)  # plain, bold, plain, ...
    styled = ((p, "bold") if i % 2 else _STRAY.sub("", p) for i, p in enumerate(parts))
    return Content.assemble("organizer: ", *styled)


def render_line(line: Line) -> Content:
    """A transcript row as `<speaker>: <text>`; a chat's first row as its divider."""
    if _divider(line):
        return Content.styled(f"── new chat, {_day(line.at)} ──", MUTED)
    if line.speaker == "organizer":
        return _reply(line.text)
    return Content.styled(f"{line.speaker}: {line.text}", _STYLE.get(line.speaker, ""))


def _row(line: Line) -> Static:
    return Static(render_line(line), classes="row")


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
    """The transcript, oldest at the top: a Static per row, then the reply streaming in, the
    hint while nothing but dividers was said, and the waiting proposal. Anchored to the
    bottom; scrolled to the top, it reads the page before and keeps the rows you were reading
    where they were. Rows too few to scroll read the pages before until they fill it."""

    def __init__(self, older: Callable[[int], Sequence[Line] | None], **kw) -> None:
        super().__init__(**kw)
        self.older = older  # the page of rows before an id, newest first; None if unreadable
        self.oldest = self.newest = 0  # the ids shown; 0 while none are
        self.more = False  # rows older than oldest may exist
        self.pending: list[Static] = []  # rows said just now, until the database has them
        self.said = False  # a row shown is more than a divider

    def compose(self) -> ComposeResult:
        yield Static(id="live", classes="row", markup=False)
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
        await self.mount_all([_row(ln) for ln in reversed(page)], before=0)

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
        self._last = ""  # the newest row, for the strip
        self._saved = ""  # the newest row the database has
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
        if lines and (lines[-1].id < log.newest or (log.newest and len(new) == PAGE)):
            log.query(".row").exclude("#live").remove()
            log.pending.clear()
            log.newest, log.said, new = 0, False, list(lines)
        if not new:
            return
        self.drop_pending()
        if not log.newest:
            log.oldest, log.more = new[0].id, len(lines) == PAGE
        log.newest = new[-1].id
        log.mount_all([_row(ln) for ln in new], before="#live")
        log.heard(new)
        log.call_after_refresh(log.fill)
        self._last = self._saved = render_line(new[-1]).plain
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
        row = _row(line)
        log.pending.append(row)
        log.heard([])
        log.mount(row, before="#live")
        log.scroll_end(animate=False)
        self._last = render_line(line).plain
        self._paint_strip()

    def stream(self, text: str | None) -> None:
        """The reply so far (or the working line), under the transcript; None clears it."""
        self._live = text
        live = self.query_one("#live", Static)
        live.update(_reply(text) if text is not None else "")
        live.display = text is not None
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
            line = _reply(self._live).plain
        else:
            line = self._last
        self.query_one("#strip", Static).update(line)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.post_message(self.Submitted(event.value))
