"""The keymap, as data: the bindings, the key line, the y/n line, the confirm line and the ?
list are all generated from KEYMAP, so none of them can name a key that isn't bound, or leave
one out (daylogs' design).

`scope` is "app" (every tab), a tab ("week": the week and month views' own keys; "report":
the Report view's; bound by the view while it has focus, never by the app), "proposal" (live
only while a waiting proposal's diff is shown), "popup" (the popups, which keep the app's keys
from firing while open) or "confirm" (the new-chat dialog). A key may mean different things
on each tab, but never shadows an app key. Key names are Textual's own (`question_mark`, not
`?`).

`kind` picks the key line's row and its glyph's colour: write and danger, then view, then
nav. `footer=False` keeps a key off the line (`?` still lists it), `pin` keeps it on however
narrow the line, and `hint` is the glyph a pair shows for both its keys (`←→`), the second
being `footer=False`.
"""

from collections.abc import Callable
from dataclasses import dataclass

from textual.binding import Binding
from textual.content import Content

from orrery.tui.diff import MUTED

KINDS = ("write", "danger", "view", "nav")
GROUPS = (("write", "danger"), ("view",), ("nav",))  # the key line's three rows
TABS = {"week": "Week", "report": "Report"}


@dataclass(frozen=True)
class Key:
    key: str  # Textual key name
    label: str  # shown on the key line, in the ? list, and on the y/n and confirm lines
    action: str  # app and proposal scope: the app's app_<action>(); else action_<action>()
    scope: str = "app"
    kind: str = "nav"
    footer: bool = True
    pin: bool = False
    hint: str = ""  # a pair's glyph, on its first key


# In order of importance within a row, which is the order a narrow line sheds them in reverse.
KEYMAP: tuple[Key, ...] = (
    # every tab's
    Key("c", "chat", "chat", kind="write"),
    Key("f", "fronts", "fronts", kind="write"),  # edits fronts.md
    Key("u", "undo", "undo", kind="danger"),
    Key("x", "new chat", "new_chat", kind="danger"),
    Key("T", "theme", "theme", kind="view"),
    Key("h", "history", "history", kind="view"),
    Key("M", "memory", "memory", kind="view"),
    Key("question_mark", "keys", "keys", kind="view", pin=True),  # how you find the rest
    Key("tab", "tabs", "next_tab"),
    Key("shift+tab", "tabs back", "previous_tab", footer=False),
    Key("r", "reload", "reload"),
    Key("q", "quit", "quit", pin=True),  # how you leave
    Key("escape", "cancel, or leave the chat", "cancel", footer=False),
    # the Week tab: the week and month views'
    Key("w", "week", "week", "week", "view"),
    Key("m", "month", "month", "week", "view"),
    Key("enter", "open", "open", "week", "view"),
    Key("left", "move", "left", "week", hint="←→"),
    Key("right", "move", "right", "week", footer=False),
    Key("up", "previous/next", "up", "week", hint="↑↓"),
    Key("down", "previous/next", "down", "week", footer=False),
    Key("left_square_bracket", "period", "earlier", "week", hint="[ ]"),
    Key("right_square_bracket", "period", "later", "week", footer=False),
    Key("t", "today", "today", "week"),
    # the Report tab's
    Key("z", "range", "range", "report", "view"),
    Key("up", "subject", "up", "report", "view", hint="↑↓"),
    Key("down", "subject", "down", "report", "view", footer=False),
    Key("enter", "evaluations", "open", "report", "view"),
    Key("left", "earlier/later", "left", "report", hint="←→"),
    Key("right", "earlier/later", "right", "report", footer=False),
    Key("left_square_bracket", "period", "earlier", "report", hint="[ ]"),
    Key("right_square_bracket", "period", "later", "report", footer=False),
    Key("t", "today", "today", "report"),
    # a waiting proposal's, the popups' and the new-chat dialog's
    Key("y", "keep", "keep", "proposal", "write"),
    Key("n", "discard", "discard", "proposal", "danger"),
    Key("escape", "close", "dismiss", "popup"),
    Key("y", "yes", "yes", "confirm", "danger"),
    Key("n", "no", "no", "confirm"),
    Key("escape", "no", "no", "confirm"),
)

_GLYPH = {
    "question_mark": "?",
    "escape": "esc",
    "left": "←",
    "right": "→",
    "up": "↑",
    "down": "↓",
    "left_square_bracket": "[",
    "right_square_bracket": "]",
}
# A glyph's colour says what its key does before you read the label: never red or a status.
# The text-* tokens, as the theme tints them for text on its background: the plain ones can
# sit on the muted label's colour (gruvbox's $secondary) or vanish (textual-dark's).
_KIND_STYLE = {
    "write": "bold $text-accent",
    "danger": "bold $text-secondary",
    "view": "bold $text-primary",
    "nav": "bold $foreground-muted",
}
_SEP = " · "
DISPATCHED = ("app", "proposal")  # the app's own keys, all through dispatch('<key>')
# Ahead of the screen's own tab (focus next) and of any focused widget; the app makes them do
# nothing in the chat input and over a dialog.
PRIORITY = ("tab", "shift+tab")


def glyph(key: str) -> str:
    """A key as it's drawn: `?`, not Textual's `question_mark`."""
    return _GLYPH.get(key, key)


def lookup(key: str, scopes: tuple[str, ...] = ("app",)) -> Key | None:
    """key's entry in the first of scopes that has it."""
    return next((k for s in scopes for k in KEYMAP if k.key == key and k.scope == s), None)


def app_bindings() -> list[Binding]:
    """App.BINDINGS: every app and proposal key goes through dispatch('<key>')."""
    return [
        Binding(k.key, f"dispatch('{k.key}')", show=False, priority=k.key in PRIORITY)
        for k in KEYMAP
        if k.scope in DISPATCHED
    ]


def cursor_bindings(scope: str) -> list[Binding]:
    """A tab's views' BINDINGS (scope week or report): their own action_<action>."""
    return [Binding(k.key, k.action, show=False) for k in KEYMAP if k.scope == scope]


def popup_bindings(scope: str = "popup") -> list[tuple[str, str]]:
    """(key, action) for the BINDINGS of scope's screen (the popups; the confirm dialog):
    their own action_<action>."""
    return [(k.key, k.action) for k in KEYMAP if k.scope == scope]


def footer(scope: str) -> Content:
    """Every key of scope on one line, `<key> <label>` joined by ` · `, keys in the accent:
    the y/n line under a proposal, or the confirm dialog's."""
    parts: list[str | tuple[str, str]] = []
    for k in KEYMAP:
        if k.scope == scope:
            parts += [_SEP] if parts else []
            parts += [(glyph(k.key), "$accent"), f" {k.label}"]
    return Content.assemble(*parts)


def footer_keys(tab: str) -> tuple[Key, ...]:
    """The tab's footer keys, then the app's: the tab's own are what you're looking for, and a
    narrow line sheds from the end."""
    return tuple(k for s in (tab, "app") for k in KEYMAP if k.scope == s and k.footer)


def shown(k: Key) -> Content:
    """k's hint on the key line: its glyph (a pair's, for a pair) in its kind's colour, then
    its label, muted."""
    return Content.assemble((k.hint or glyph(k.key), _KIND_STYLE[k.kind]), (f" {k.label}", MUTED))


def key_rows(tab: str, width: int, live: Callable[[Key], bool] | None = None) -> list[Content]:
    """The tab's key line, one row per GROUPS entry, always all of them (a row with no keys is
    blank, so the line's height never changes): its live footer keys of the row's kinds,
    dropped from the row's end until it fits width, except the pinned. Not even they fit, the
    row is blank rather than wider than width."""
    keys = [k for k in footer_keys(tab) if live is None or live(k)]
    rows = []
    for kinds in GROUPS:
        keep = [k for kind in kinds for k in keys if k.kind == kind]
        while keep and Content(_SEP).join(map(shown, keep)).cell_length > width:
            drop = [i for i, k in enumerate(keep) if not k.pin]
            if not drop:
                keep = []
                break
            del keep[drop[-1]]
        rows.append(Content.styled(_SEP, MUTED).join(map(shown, keep)))
    return rows


def help_text() -> str:
    """Every key for the ? list, under the headings write, danger, view and nav: in keymap
    order within one, each with its tab (or the popup or dialog it's in) where it isn't
    app-wide."""
    lines = []
    for kind in KINDS:
        lines.append(kind)
        for k in KEYMAP:
            if k.kind == kind:
                where = "" if k.scope == "app" else f" ({TABS.get(k.scope, k.scope)})"
                lines.append(f"  {glyph(k.key):<9} {k.label}{where}")
    return "\n".join(lines)
