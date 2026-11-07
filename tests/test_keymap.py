"""The keymap, and the key line generated from it: three rows by kind, fitted to the footer,
each tab listing only the keys that do something there."""

import pytest
from helpers import NOW, chat_keys, settle
from textual.app import App
from textual.color import Color
from textual.widget import Widget
from textual.widgets import ContentSwitcher

from orrery.tui import keymap as km
from orrery.tui.app import OrreryApp
from orrery.tui.chat import ChatPanel
from orrery.tui.month import MonthView
from orrery.tui.report import ReportView
from orrery.tui.themes import ThemePicker
from orrery.tui.views import Confirm, KeyFooter, Popup
from orrery.tui.week import WeekView

OWNERS = {
    "app": [(OrreryApp, "app_")],
    "proposal": [(OrreryApp, "app_")],
    "week": [(WeekView, "action_"), (MonthView, "action_")],
    "report": [(ReportView, "action_")],
    "popup": [(Popup, "action_")],
    "confirm": [(Confirm, "action_")],
}
VIEWS = {"week": (WeekView, MonthView), "report": (ReportView,)}
TABS = ("week", "report")
WIDTHS = (80, 82, 84, 100, 120, 160, 235)  # 82, 84: a row that just fits
SEP = " · "


def _app(home) -> OrreryApp:
    return OrreryApp(root=lambda: home, now=lambda: NOW)


def _hints(rows) -> list[list[str]]:
    """Each row's hints, `<glyph> <label>`."""
    return [r.plain.split(SEP) if r.plain else [] for r in rows]


def _footer(app) -> list[str]:
    """The key line's three rows as drawn."""
    f = app.query_one(KeyFooter)
    return [f.render_line(y).text.strip() for y in range(f.size.height)]


async def _tab(pilot, app, tab: str) -> None:
    if app.query_one("#tabs", ContentSwitcher).current != tab:
        await pilot.press("tab")
        await settle(pilot, app)


def test_keymap_no_duplicate_key_scope():
    pairs = [(k.key, k.scope) for k in km.KEYMAP]
    assert len(pairs) == len(set(pairs))


def test_every_key_has_a_kind():
    assert {k.kind for k in km.KEYMAP} == set(km.KINDS)
    assert [k for kinds in km.GROUPS for k in kinds] == ["write", "danger", "view", "nav"]


def test_each_view_binds_its_own_tabs_keys_and_the_app_never_does():
    """The arrows, enter, [ ], t, and w and m or z are the views' own, bound where a view has
    focus, never by the app, so the chat input and the popups never see them."""
    tab_keys = {k.key for k in km.KEYMAP if k.scope in TABS}
    for owner in (OrreryApp, Popup, Confirm):
        assert not tab_keys & {b.key for b in owner.BINDINGS}, owner
    for tab, views in VIEWS.items():
        bindings = km.cursor_bindings(tab)
        assert {(b.key, b.action) for b in bindings} == {
            (k.key, k.action) for k in km.KEYMAP if k.scope == tab
        }
        assert not any(b.show or b.priority for b in bindings)
        for view in views:
            bound = view._merge_bindings().key_to_bindings
            assert {(key, b.action) for key, bs in bound.items() for b in bs} == {
                (b.key, b.action) for b in bindings
            }, view


def test_tab_keys_never_shadow_app_keys():
    """A key may mean different things on each tab, never something the app means too."""
    app = {k.key for k in km.KEYMAP if k.scope in ("app", "proposal")}
    for tab in TABS:
        assert not app & {k.key for k in km.KEYMAP if k.scope == tab}, tab


def test_bindings_cover_each_key_once():
    """The app binds its own keys and the proposal's; the popup binds escape; the confirm
    dialog binds y, n and escape. Only tab and shift+tab are priority, so they reach the app
    past the screen's focus keys."""
    owners = ((("app", "proposal"), OrreryApp), (("popup",), Popup), (("confirm",), Confirm))
    for scopes, owner in owners:
        keys = [b.key for b in owner.BINDINGS]
        assert len(keys) == len(set(keys))
        assert set(keys) == {k.key for k in km.KEYMAP if k.scope in scopes}
    assert all(b.action == f"dispatch('{b.key}')" for b in OrreryApp.BINDINGS)
    assert {b.key for b in OrreryApp.BINDINGS if b.priority} == {"tab", "shift+tab"}
    assert {b.key: b.action for b in Confirm.BINDINGS} == {"y": "yes", "n": "no", "escape": "no"}


def test_every_key_has_a_handler_in_its_scope():
    for k in km.KEYMAP:
        for owner, prefix in OWNERS[k.scope]:
            assert callable(getattr(owner, prefix + k.action, None)), (k, owner)


def test_no_view_has_an_action_its_tab_doesnt_bind():
    """No do-nothing stubs: a view's actions are its tab's keys', so a key it has an action
    for is a key that does something there."""
    for tab, views in VIEWS.items():
        actions = {f"action_{k.action}" for k in km.KEYMAP if k.scope == tab}
        for view in views:
            own = {a for a in dir(view) if a.startswith("action_")} - set(dir(Widget))
            assert own == actions, view


def test_lookup_finds_the_scopes_asked_for():
    assert km.lookup("r").action == "reload"
    assert km.lookup("x").action == "new_chat"
    assert km.lookup("M").action == "memory"
    assert km.lookup("m") is None  # the month view's
    assert km.lookup("escape").action == "cancel"  # the popups take it first
    assert km.lookup("z") is None
    assert km.lookup("y") is None  # y and n only while a proposal waits
    assert km.lookup("y", ("proposal", "app")).action == "keep"
    assert km.lookup("u", ("proposal", "app")).action == "undo"


def test_footer_keys_are_the_tabs_then_the_apps():
    for tab in TABS:
        keys = km.footer_keys(tab)
        assert all(k.footer and k.scope in (tab, "app") for k in keys)
        scopes = [k.scope for k in keys]
        assert scopes == sorted(scopes, key=lambda s: s == "app") and scopes[-1] == "app"
        assert set(keys) == {k for k in km.KEYMAP if k.footer and k.scope in (tab, "app")}


def test_a_row_sheds_from_its_end_and_never_a_pin():
    """Too wide, a row drops hints from its end until it fits; the pinned stay, wherever they
    are. Not even they fit: the row is empty, never wider than asked. Always three rows."""
    full = _hints(km.key_rows("report", 400))
    for width in (60, 40, 20):
        rows = km.key_rows("report", width)
        assert len(rows) == len(km.GROUPS)
        for row, hints, every in zip(rows, _hints(rows), full, strict=True):
            assert row.cell_length <= width
            pins = [h for h in every if h in ("? keys", "q quit")]
            rest = [h for h in hints if h not in pins]
            assert rest == [h for h in every if h not in pins][: len(rest)]  # a prefix
            assert all(p in hints for p in pins), (width, hints)
    assert _hints(km.key_rows("report", 40))[1] == ["z range", "↑↓ subject", "? keys"]
    assert [r.plain for r in km.key_rows("week", 5)] == ["", "", ""]


def test_footer_glyphs_take_their_kind_token():
    """Each hint's glyph in its kind's token, its label and the separators muted."""
    tokens = {"write": "bold $text-accent", "danger": "bold $text-secondary"}
    tokens |= {"view": "bold $text-primary", "nav": "bold $foreground-muted"}
    for tab in TABS:
        for row in km.key_rows(tab, 400):
            for i, hint in enumerate(row.plain.split(SEP)):
                k = next(k for k in km.footer_keys(tab) if hint == km.shown(k).plain)
                glyph = k.hint or km.glyph(k.key)
                start = sum(len(h) + len(SEP) for h in row.plain.split(SEP)[:i])
                styles = {s.style for s in row.spans if s.start == start}
                assert styles == {tokens[k.kind]}, (hint, styles)
                label = {s.style for s in row.spans if s.start == start + len(glyph)}
                assert label == {"$foreground-muted"}, hint


async def test_every_kind_reads_apart_from_its_label_in_every_theme():
    """In every theme but the ansi ones (the terminal's own colours), each kind's glyph is at
    least 30 apart (RGB distance) from the muted label beside it: gruvbox's $secondary was 7."""
    app = App()
    async with app.run_test() as pilot:
        for theme in sorted(t for t in app.available_themes if not t.startswith("ansi")):
            app.theme = theme
            await pilot.pause()
            v = app.theme_variables
            label = (Color.parse(v["background"]) + Color.parse(v["foreground-muted"])).rgb
            for kind in ("write", "danger", "view"):
                glyph = Color.parse(v[km._KIND_STYLE[kind].split("$")[1]]).rgb
                apart = sum((a - b) ** 2 for a, b in zip(glyph, label, strict=True)) ** 0.5
                assert apart >= 30, (theme, kind, round(apart))


def test_pairs_show_together():
    """←→, ↑↓ and [ ] are one hint each, the pair's first key's, and the second key's is off
    the line."""
    for k in km.KEYMAP:
        if k.hint:
            assert k.scope in TABS and k.footer
            nxt = km.KEYMAP[km.KEYMAP.index(k) + 1]
            assert nxt.scope == k.scope and nxt.label == k.label and not nxt.footer
    week, report = (" ".join(r.plain for r in km.key_rows(t, 400)) for t in TABS)
    assert "←→ move" in week and "↑↓ previous/next" in week and "[ ] period" in week
    assert "←→ earlier/later" in report and "↑↓ subject" in report and "[ ] period" in report


def test_confirm_line_lists_y_n_esc():
    """The line under the new-chat question."""
    assert km.footer("confirm").plain == "y yes · n no · esc no"


def test_proposal_line_lists_y_n():
    """The line under a waiting proposal's diff."""
    line = km.footer("proposal")
    assert line.plain == "y keep · n discard"
    assert [line.plain[s.start : s.end] for s in line.spans if s.style == "$accent"] == ["y", "n"]


def test_question_mark_lists_keys_by_group():
    """Every key under its kind's heading, write, danger, view then nav; in keymap order
    within one, each with its tab where it isn't app-wide."""
    where = {"week": "Week", "report": "Report"}
    got: dict[str, list[tuple[str, str]]] = {}
    for line in km.help_text().splitlines():
        if not line.startswith(" "):
            got[line] = []
        elif line.strip():
            glyph, label = line.split(maxsplit=1)
            got[list(got)[-1]].append((glyph, label))
    assert list(got) == list(km.KINDS)
    for kind, rows in got.items():
        assert rows == [
            (
                km.glyph(k.key),
                k.label if k.scope == "app" else f"{k.label} ({where.get(k.scope, k.scope)})",
            )
            for k in km.KEYMAP
            if k.kind == kind
        ]


# ── the key line on screen ───────────────────────────────────────────────────────────────
async def test_footer_is_generated_from_the_keymap(week_home):
    """At 235 columns (a wide terminal), every hint on both tabs is a footer key of the keymap's
    that the app says is live there, and T theme is on both."""
    app = _app(week_home)
    async with app.run_test(size=(235, 60)) as pilot:
        await settle(pilot, app)
        for tab in TABS:
            await _tab(pilot, app, tab)
            hints = [h for row in _footer(app) if row for h in row.split(SEP)]
            shown = {km.shown(k).plain: k for k in km.footer_keys(tab)}
            assert all(h in shown and app.handles(shown[h]) for h in hints), hints
            assert "T theme" in hints
            live = [km.shown(k).plain for k in km.footer_keys(tab) if app.handles(k)]
            assert sorted(hints) == sorted(live)  # every one of them, at this width


@pytest.mark.parametrize("width", WIDTHS)
async def test_footer_rows_fit_and_keep_keys_and_quit(week_home, width):
    app = _app(week_home)
    async with app.run_test(size=(width, 40)) as pilot:
        await settle(pilot, app)
        footer = app.query_one(KeyFooter)
        for tab in TABS:
            await _tab(pilot, app, tab)
            rows = _footer(app)
            # what's drawn is clipped to the line, so the fit is checked on what's generated
            want = km.key_rows(tab, footer.content_size.width, app.handles)
            assert all(r.cell_length <= footer.content_size.width for r in want), (tab, want)
            assert rows == [r.plain for r in want], (tab, rows)  # fitted to all of it
            hints = [h for r in rows if r for h in r.split(SEP)]
            assert {"? keys", "q quit", "T theme"} <= set(hints), (tab, hints)


async def test_a_row_that_just_fits_keeps_every_hint(week_home):
    """At 82 columns the line inside its padding is 80 wide, as wide as Week's whole nav row:
    nothing is dropped from it, before a resize or after one."""
    app = _app(week_home)
    async with app.run_test(size=(82, 24)) as pilot:
        await settle(pilot, app)
        full = km.key_rows("week", 400, app.handles)[2]
        assert full.cell_length == 80 and _footer(app)[2] == full.plain
        await pilot.resize_terminal(81, 24)
        await settle(pilot, app)
        assert "r reload" not in _footer(app)[2]
        await pilot.resize_terminal(82, 24)
        await settle(pilot, app)
        assert _footer(app)[2] == full.plain


async def test_each_tab_lists_only_its_live_keys(week_home):
    app = _app(week_home)
    async with app.run_test(size=(235, 60)) as pilot:
        await settle(pilot, app)
        week = " · ".join(_footer(app))
        await _tab(pilot, app, "report")
        report = " · ".join(_footer(app))
    assert "w week" in week and "m month" in week and "enter open" in week
    assert "z range" not in week and "subject" not in week and "evaluations" not in week
    assert "z range" in report and "↑↓ subject" in report and "enter evaluations" in report
    assert "w week" not in report and "m month" not in report and "move" not in report


async def test_the_resolver_is_the_dispatchers(week_home):
    """app.handles answers as the keys do: an app key everywhere, a tab's key on its tab."""
    app = _app(week_home)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot, app)
        z, w = km.lookup("z", ("report",)), km.lookup("w", ("week",))
        assert app.handles(km.lookup("T")) and app.handles(w) and not app.handles(z)
        await _tab(pilot, app, "report")
        assert app.handles(km.lookup("T")) and app.handles(z) and not app.handles(w)


async def test_footer_is_three_rows_on_both_tabs(week_home):
    """The same height on both tabs, a row blank where a tab has no keys for it, so a switch
    never reflows the screen; the theme picker opens right above it."""
    app = _app(week_home)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(pilot, app)
        footer = app.query_one(KeyFooter)
        for tab in TABS:
            await _tab(pilot, app, tab)
            assert footer.region.height == 3 and len(_footer(app)) == 3, tab
            assert footer.region.bottom == 24
        await pilot.press("T")
        await settle(pilot, app)
        picker = app.query_one(ThemePicker)
        assert picker.region.height == 3 and picker.region.bottom == footer.region.y
        assert footer.region.height == 3


async def test_the_chat_overlay_at_80x24_leaves_the_input_and_the_footer(week_home):
    app = _app(week_home)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(pilot, app)
        await chat_keys(pilot, app, "h", "i")
        await pilot.pause()
        chat, footer = app.query_one(ChatPanel), app.query_one(KeyFooter)
        line = app.query_one("#chat-input")
        assert chat.has_class("-overlay") and line.value == "hi"
        assert chat.region.bottom <= footer.region.y and footer.region.height == 3
        assert line.region.height == 1 and line.region.bottom <= footer.region.y


async def test_a_resize_refits_the_footer_and_a_tab_switch_shows_it_once(week_home):
    """The footer fits itself at its own resize (a Resize reaches the widgets, not the App);
    a tab switch draws it exactly once."""
    app = _app(week_home)
    async with app.run_test(size=(235, 60)) as pilot:
        await settle(pilot, app)
        footer = app.query_one(KeyFooter)
        assert "r reload" in _footer(app)[2]
        await pilot.resize_terminal(80, 24)
        await settle(pilot, app)
        assert all(len(r) <= 78 for r in _footer(app))
        assert "r reload" not in _footer(app)[2]  # shed at 80
        await pilot.resize_terminal(235, 60)
        await settle(pilot, app)
        assert "r reload" in _footer(app)[2]
        shown, real = [], footer.show
        footer.show = lambda *a: (shown.append(a[0]), real(*a))
        await pilot.press("tab")
        await settle(pilot, app)
        assert shown == ["report"]


async def test_typing_in_the_chat_fires_no_key(week_home):
    """In the chat input w, m, z, t, T and q type, and nothing else happens."""
    app = _app(week_home)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot, app)
        before = app.cursor_date
        await chat_keys(pilot, app, "w", "m", "z", "t", "T", "q")
        await settle(pilot, app)
        assert app.is_running and app.query_one("#chat-input").value == "wmztTq"
        assert app.view == "week" and app.report_range == "month"
        assert app.cursor_date == before and not app.query_one(ThemePicker).is_open
        assert app.query_one("#tabs", ContentSwitcher).current == "week"


async def test_another_tabs_keys_do_nothing(week_home):
    """w and m on Report, z on Week: nothing changes and nothing is said."""
    app = _app(week_home)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot, app)
        await pilot.press("z")
        await settle(pilot, app)
        assert app.report_range == "month" and app.query_one(ReportView).range == "month"
        assert not app.query_one("#note").display, "z on Week says nothing"
        await _tab(pilot, app, "report")
        await pilot.press("m", "w")
        await settle(pilot, app)
        assert app.view == "week" and app.query_one(WeekView).display
        assert app.focused is app.query_one(ReportView)
        assert not app.query_one("#note").display
        assert app.query_one("#tabs", ContentSwitcher).current == "report"
