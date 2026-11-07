"""Theme names, borrowed from Textual rather than defined here, and `T`'s picker.

app.tcss styles the frame with Textual design tokens only, so `App.theme = name` re-themes
it with no stylesheet change. Subject colours are not themed: a subject's hue is its
identity (palette.py). A widget that paints with them repaints on a theme change through
`self.app.theme_changed_signal.subscribe(self, callback)`.
"""

from textual.app import App
from textual.message import Message
from textual.widgets import Static

DEFAULT = "gruvbox"  # settings.py repeats the literal to stay free of textual; a test ties them


def names(app: App) -> tuple[str, ...]:
    """Every theme the app offers, sorted: read at call time, so a Textual upgrade or a
    registered theme is offered without a code change."""
    return tuple(sorted(app.available_themes))


def resolve(name: str | None, app: App) -> str:
    """A usable theme name for config.toml's value: a stale, misspelled or missing one falls
    back to the default rather than stopping the app over a cosmetic setting."""
    return name if name in app.available_themes else DEFAULT


# ── the picker ───────────────────────────────────────────────────────────
# `←`/`→` apply each theme live, `enter` keeps the one on screen, `esc` puts back the one you
# started with. A focused widget in the bottom container, not a modal: a modal would cover
# the screen you are previewing.

_SEP = "   "
# Literal marks, not colour: the cursor sits on a name whose own colours change under it.
_CURSOR = ("▸", "◂")
# Reserved for the two ellipses, always, so growing the window can't overshoot the panel.
_ELLIPSIS_RESERVE = 4


def strip(names, index: int, width: int) -> str:
    """A window of theme names centred on `index`, as wide as `width` allows.

    Plain text, so `len()` measures what reaches the screen. Grows rightward first: the
    next name `→` lands on is the one most worth seeing.
    """
    names = list(names)
    if not names:
        return ""
    index = max(0, min(index, len(names) - 1))
    marked = [f"{_CURSOR[0]}{n}{_CURSOR[1]}" if i == index else n for i, n in enumerate(names)]
    budget = max(len(marked[index]), width - _ELLIPSIS_RESERVE)

    lo = hi = index
    out = marked[index]
    while True:
        grew = False
        if hi + 1 < len(names) and len(out) + len(_SEP) + len(marked[hi + 1]) <= budget:
            hi += 1
            out = out + _SEP + marked[hi]
            grew = True
        if lo - 1 >= 0 and len(out) + len(_SEP) + len(marked[lo - 1]) <= budget:
            lo -= 1
            out = marked[lo] + _SEP + out
            grew = True
        if not grew:
            break

    if lo > 0:
        out = "… " + out
    if hi < len(names) - 1:
        out = out + " …"
    return out


class ThemePicker(Static):
    """`T`'s surface: a live preview you arrow through.

    Keys are taken in `on_key` with `stop()` + `prevent_default()`, so `←`/`→`, `enter` and
    `esc` reach the picker before any non-priority binding (`←`/`→`/`enter` are the views',
    `esc` cancels a turn). Cancelling restores the theme in effect when the picker
    opened, not config.toml's value, which may be stale or never written.
    """

    class Chosen(Message):
        def __init__(self, name: str) -> None:
            super().__init__()
            self.name = name

    class Cancelled(Message):
        pass

    can_focus = True

    def __init__(self, **kw) -> None:
        super().__init__("", markup=False, **kw)
        self._names: tuple[str, ...] = ()
        self._index = 0
        self._restore = DEFAULT
        self.display = False

    @property
    def is_open(self) -> bool:
        return self.display

    @property
    def selected(self) -> str:
        return self._names[self._index] if self._names else DEFAULT

    def open(self, current: str) -> None:
        """Show the picker, positioned on the theme in effect."""
        self._names = names(self.app)
        self._restore = current
        self._index = self._names.index(current) if current in self._names else 0
        self.display = True
        self.repaint()
        self.focus()

    def close(self) -> None:
        self.display = False

    def cancel(self) -> None:
        """Put the theme back and go away, without asking for focus: for a widget that has
        just taken the keyboard, so no `Cancelled`, which would hand focus elsewhere."""
        self.app.theme = self._restore
        self.close()

    def repaint(self) -> None:
        if not self._names:
            return
        width = self.content_size.width or 80
        self.update(strip(self._names, self._index, width))
        self.border_title = f"theme › {self.selected}   {self._index + 1} of {len(self._names)}"
        # Names the theme esc puts back: the one thing you can't see once every colour changed.
        self.border_subtitle = f"← → preview · enter keeps it · esc restores {self._restore}"

    def on_resize(self) -> None:
        if self.display:
            self.repaint()

    def _step(self, delta: int) -> None:
        """Wraps, so the far end of the list is never more than half of it away."""
        self._index = (self._index + delta) % len(self._names)
        self.app.theme = self.selected
        self.repaint()

    def on_key(self, event) -> None:
        if not self.display:
            return
        if event.key in ("left", "right"):
            event.stop()
            event.prevent_default()
            self._step(-1 if event.key == "left" else 1)
        elif event.key == "enter":
            event.stop()
            event.prevent_default()
            name = self.selected
            self.close()
            self.post_message(self.Chosen(name))
        elif event.key == "escape":
            event.stop()
            event.prevent_default()
            self.app.theme = self._restore
            self.close()
            self.post_message(self.Cancelled())
