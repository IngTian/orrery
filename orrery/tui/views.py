"""f's editor, the key line (KeyFooter), the popup for M, h and ?, and the new-chat dialog
(Confirm).

Data-derived strings reach the screen as Content or through Static(..., markup=False): a
`[/b]` in a mentor's topic would otherwise raise MarkupError and take the app down.
"""

import os
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Static

from orrery import store
from orrery.tui import keymap
from orrery.tui.diff import MUTED

TITLE = "bold $accent"  # a .panel's title; its border-title-color is the border's, for the edge


def panel_title(title: str, caption: str, width: int) -> Content:
    """A .panel's border title for its outer width: the title, the edge, then the caption,
    muted, against the right corner (`╭─ title ──── caption ─╮`). The caption goes when
    the two don't fit."""
    # 8: the two corners, and a ─ and a space on each side
    gap = width - 8 - Content(title).cell_length - Content(caption).cell_length
    if not caption or gap < 1:
        return Content.styled(title, TITLE)
    return Content.assemble((title, TITLE), " ", "─" * gap, " ", (caption, MUTED))


def _editor() -> list[str]:
    """$EDITOR as argv ("code --wait" is two words); vi when unset, blank or unparsable."""
    try:
        return shlex.split(os.environ.get("EDITOR") or "vi") or ["vi"]
    except ValueError:  # an unbalanced quote
        return ["vi"]


def edit_fronts(path: Path, app: App) -> str:
    """path (fronts.md) in $EDITOR, the app suspended meanwhile; never creates the file. The
    line to show after: why it couldn't, or "" once the editor is done."""
    got = store.read_text(path)
    if isinstance(got, store.Unavailable):
        return f"orrery: data unavailable (fronts.md {got.reason})"
    argv = [*_editor(), str(path)]  # the path is never argv[0]
    notice = ""
    with app.suspend():
        # Textual's suspend has no try/finally: an exception escaping here skips the resume
        # and leaves the terminal suspended, so nothing may escape.
        try:
            subprocess.run(argv)
        except (OSError, ValueError) as e:
            notice = f"Editor did not start: {e}"
    return notice


class KeyFooter(Static):
    """The key line: the open tab's live keys from the keymap, on its three rows, fitted to
    the line's width."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self._tab, self._live = "week", None

    def show(self, tab: str, live: Callable[[keymap.Key], bool] | None = None) -> None:
        """tab's keys that live says do something there, fitted inside the padding (size is
        already the content's). Before the first layout the line has no width yet, so the
        app's less the padding stands in: guessing narrow would drop keys that fit."""
        self._tab, self._live = tab, live
        width = self.size.width or self.app.size.width - self.styles.gutter.width
        rows = keymap.key_rows(tab, max(width, 10), live)
        self.update(Content("\n").join(rows))

    def on_resize(self) -> None:
        """Fitted again at its own resize: a Resize reaches the widgets, never the App."""
        self.show(self._tab, self._live)


class Popup(ModalScreen):
    """Text over the tabs (the memory notes, the history, the keys) until escape; the app's
    keys are off meanwhile. Taller than the window (? at 24 rows), it scrolls: the arrows and
    the page keys are its box's, focused while it's open."""

    BINDINGS = [Binding(key, action, show=False) for key, action in keymap.popup_bindings()]
    AUTO_FOCUS = "#popup-scroll"

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="popup-scroll"):
            yield Static(self.text, id="popup-text", markup=False)


class Confirm(ModalScreen[bool]):
    """A question over the tabs, with its y/n line: y dismisses it with True, n and escape
    with False; the app's keys are off meanwhile."""

    BINDINGS = [
        Binding(key, action, show=False) for key, action in keymap.popup_bindings("confirm")
    ]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        line = Content("\n\n").join([Content(self.question), keymap.footer("confirm")])
        yield Static(line, id="popup-text")  # styled as the popup's

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)
