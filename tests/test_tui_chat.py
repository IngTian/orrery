"""The organizer chat on screen, through the Pilot: a turn streamed into the docked panel, esc,
a second line while answering, x's confirm, remembered notes, the M and h popups, the
transcript across restarts, a proposal answered by typing y or n, the organizer's bold, and the
transcript read as messages: speaker lines, indented text, orrery's compact lines, the turn in
progress and the strip. The organizer is tests/fake_claude.py's fake, never the real CLI."""

import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from datetime import timedelta

import pytest
from fake_claude import (
    INIT_OK,
    block_start,
    delta,
    make_fake,
    read_calls,
    result_ok,
    transcript_tail,
)
from helpers import NOW, TODAY, chat_keys, propose_move, remember_at, settle, state_of
from test_tui import SIZE, _talk, _text, _until, _week
from textual.geometry import Region
from textual.strip import Strip

from orrery import channels, db, proposals, store, text
from orrery.organizer import prompt, sessions, turn
from orrery.organizer.sessions import Line
from orrery.tui import keymap
from orrery.tui.app import OrreryApp
from orrery.tui.chat import ChatInput, render_line, summary
from orrery.tui.diff import MUTED
from orrery.tui.views import Confirm

STILL = "the organizer is still answering; esc cancels"
QUESTION = "End this organizer session and start a new one? Its transcript is kept; memory stays."
FORGET = "ask the organizer to forget a note"
STARTED = "── new chat, Thu 11/12 ──"


def _app(home, fake=None) -> OrreryApp:
    return OrreryApp(
        root=lambda: home, now=lambda: NOW, claude_path=str(fake) if fake is not None else None
    )


def _head(app: OrreryApp) -> Strip:
    """The chat panel's top border as drawn: its title and caption."""
    body = app.query_one("#chat-body")
    return body.render_lines(Region(0, 0, *body.outer_size))[0]


def _organizer_done(app: OrreryApp) -> bool:
    return not [w for w in app.workers if w.group == "organizer"]


def _session_ids(home) -> list[int]:
    with closing(db.open_existing(home)) as conn:
        return [r[0] for r in conn.execute("select id from organizer_sessions order by id")]


async def test_chat_turn_streams_and_shows_proposal(week_home, tmp_path):
    fake = make_fake(
        tmp_path,
        {"events": [INIT_OK, delta("Proposed."), result_ok("Proposed.")]},
        side_effect="helpers:propose_move_at",
    )
    app = OrreryApp(root=lambda: week_home, now=lambda: NOW, claude_path=str(fake))
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await chat_keys(pilot, app, *"push thu math to fri", "enter")
        await _until(pilot, lambda: not [w for w in app.workers if w.group == "organizer"])
        await settle(pilot, app)
        assert "y keep · n discard" in _text(app, "#proposal")
        assert _talk(app)[-2:] == ["you: push thu math to fri", "organizer: Proposed."]
        assert app.shown_proposal is not None and app.turn_cancel is None
        assert len(app.screen_stack) == 1  # the typed u, h, m, f and r reached the input only
    assert len(read_calls(fake)) == 1


async def test_cancel_kills_and_records(week_home, tmp_path):
    """A silent turn (no output at all): esc ends it within 2 s, and the transcript says so."""
    fake = make_fake(tmp_path, {"events": [INIT_OK, result_ok("too late")], "sleep": 30})
    app = _app(week_home, fake)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await chat_keys(pilot, app, *"hello", "enter")
        await _until(pilot, lambda: read_calls(fake))  # the fake is running, and sleeping
        assert _talk(app)[-2:] == ["you: hello", "organizer: … working (esc cancels)"]
        start = time.monotonic()
        await pilot.press("escape")
        await _until(pilot, lambda: "orrery: turn cancelled" in _talk(app))
        assert time.monotonic() - start < 2
        await settle(pilot, app)
        assert _talk(app)[-2:] == ["you: hello", "orrery: turn cancelled"]
        assert app.turn_cancel is None
        assert not app.query_one("#note").display, "the chat says it: no note repeats it"
    assert [ln.text for ln in transcript_tail(week_home, 2)] == ["hello", "turn cancelled"]


async def test_escape_without_a_turn_does_nothing(week_home):
    app = _app(week_home)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        before = _talk(app)
        await pilot.press("escape")
        await settle(pilot, app)
        assert app.turn_cancel is None and _talk(app) == before and app.is_running


async def test_second_line_while_answering(week_home, tmp_path):
    """The reply streams in while the turn runs; a second line and x start nothing, and the
    second line stays in the input."""
    fake = make_fake(
        tmp_path,
        {"events": [INIT_OK, delta("Let me look."), result_ok("Let me look.")], "linger": 30},
    )
    app = _app(week_home, fake)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await chat_keys(pilot, app, *"first", "enter")
        await _until(pilot, lambda: "organizer: … Let me look." in _talk(app))
        assert "organizer: … working (esc cancels)" not in _talk(app)
        await pilot.press(*"second", "enter")  # the input keeps the keyboard after a line
        await pilot.pause()
        assert _text(app, "#note") == STILL
        chat = app.query_one(ChatInput)
        assert chat.has_focus and chat.value == "second"
        app.set_focus(None)  # a click elsewhere: esc in the input would cancel the turn
        await pilot.press("x")
        await pilot.pause()
        assert len(app.screen_stack) == 1 and _text(app, "#note") == STILL
        assert app.turn_cancel is not None
        await pilot.press("escape")
        await _until(pilot, lambda: _organizer_done(app))
        await settle(pilot, app)
    assert len(read_calls(fake)) == 1
    assert [ln.text for ln in transcript_tail(week_home, 2)] == ["first", "turn cancelled"]


async def test_x_confirm_yes_restarts(week_home):
    with closing(db.open_existing(week_home)) as conn:
        old = sessions.ensure(conn, NOW)
        sessions.say(conn, old.id, "you", "an old line", NOW)
        p = propose_move(conn)
    app = _app(week_home)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        assert _talk(app) == [STARTED, "you: an old line"]
        assert "y keep · n discard" in _text(app, "#proposal")
        await pilot.press("x")
        await pilot.pause()
        assert isinstance(app.screen, Confirm)
        assert _text(app, "#popup-text") == f"{QUESTION}\n\n{keymap.footer('confirm').plain}"
        await pilot.press("y")
        await settle(pilot, app)
        assert len(app.screen_stack) == 1
        assert _talk(app) == [STARTED, "you: an old line", STARTED]  # the old chat stays above
        assert not app.query_one("#proposal").display and app.shown_proposal is None
    with closing(db.open_existing(week_home)) as conn:
        new = sessions.active(conn)
        assert new.id != old.id
        kept = sessions.transcript(conn, old.id)
        assert [ln.text for ln in kept] == ["new chat started", "an old line"]
        assert state_of(conn, p.id) == "discarded"


async def test_x_confirm_no_keeps(week_home):
    with closing(db.open_existing(week_home)) as conn:
        p = propose_move(conn)
    app = _app(week_home)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        ids = _session_ids(week_home)
        for answer in ("n", "escape"):  # the dialog's n, not the proposal's
            await pilot.press("x")
            await pilot.pause()
            assert isinstance(app.screen, Confirm)
            await pilot.press(answer)
            await settle(pilot, app)
            assert len(app.screen_stack) == 1
        assert _session_ids(week_home) == ids
        assert "y keep · n discard" in _text(app, "#proposal") and app.shown_proposal == p.id
    with closing(db.open_existing(week_home)) as conn:
        assert state_of(conn, p.id) == "waiting"


@contextmanager
def _another_window_answering():
    """The organizer lock held by another thread, as another window's turn holds it."""
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with store.file_lock("organizer"):
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(5)
        yield
    finally:
        release.set()
        holder.join()


async def test_x_busy_while_another_window_answers(week_home):
    with _another_window_answering():
        app = _app(week_home)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            ids = _session_ids(week_home)
            await pilot.press("x", "y")
            await settle(pilot, app)
            assert _text(app, "#note") == turn.BUSY
            assert _session_ids(week_home) == ids


async def test_a_line_the_organizer_never_got_does_not_stay_as_if_sent(week_home, tmp_path):
    """Busy in another window, the turn neither recorded the line nor ran claude: the line
    goes from the chat and the strip, and the note says why."""
    fake = make_fake(tmp_path, {"events": [INIT_OK, result_ok("never asked")]})
    with _another_window_answering():
        app = _app(week_home, fake)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await chat_keys(pilot, app, *"hello", "enter")
            await _until(pilot, lambda: _organizer_done(app))
            await settle(pilot, app)
            assert _text(app, "#note") == turn.BUSY
            assert _talk(app) == [STARTED]
            app.query_one("#chat").note("")  # the next key clears the note
            assert _text(app, "#strip") == STARTED, "the strip still shows the line"
    assert not read_calls(fake)
    assert [ln.text for ln in transcript_tail(week_home, 5)] == ["new chat started"]


async def test_remembered_line(week_home, tmp_path):
    remember_at(week_home, "keep Tuesdays light")  # already there: not shown as new
    fake = make_fake(
        tmp_path,
        {"events": [INIT_OK, delta("Noted."), result_ok("Noted.")]},
        side_effect="helpers:remember_at",
    )
    app = _app(week_home, fake)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await chat_keys(pilot, app, *"remember: protect Monday math", "enter")
        await _until(pilot, lambda: _organizer_done(app))
        await settle(pilot, app)
        assert _text(app, "#note") == "remembered: protect Monday math"
        assert _talk(app)[-1] == "organizer: Noted."


async def test_session_created_on_mount(week_home):
    with closing(db.open_existing(week_home)) as conn:
        assert sessions.active(conn) is None
    app = _app(week_home)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        assert _talk(app) == [STARTED]
        assert _head(app).text.startswith("╭─ Organizer ─")
        assert _head(app).text.endswith(" since Thu 11/12 ─╮")
        chat, week = app.query_one("#chat"), app.query_one("#week")
        assert chat.region.x >= week.region.right and chat.region.width == 37  # docked right
        await pilot.press("r")
        await settle(pilot, app)
    with closing(db.open_existing(week_home)) as conn:
        s = sessions.active(conn)
        assert s is not None and not s.claude_live
    assert len(_session_ids(week_home)) == 1


async def test_transcript_survives_restart(week_home, tmp_path):
    fake = make_fake(tmp_path, {"events": [INIT_OK, delta("Hello."), result_ok("Hello.")]})
    app = _app(week_home, fake)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await chat_keys(pilot, app, *"hi", "enter")
        await _until(pilot, lambda: _organizer_done(app))
        await settle(pilot, app)
    again = _app(week_home)
    async with again.run_test(size=SIZE) as pilot:
        await settle(pilot, again)
        assert _talk(again) == [STARTED, "you: hi", "organizer: Hello."]


async def test_quitting_mid_turn_cancels_it(week_home, tmp_path):
    fake = make_fake(tmp_path, {"events": [INIT_OK, result_ok("too late")], "sleep": 30})
    app = _app(week_home, fake)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await chat_keys(pilot, app, *"hello", "enter")
        await _until(pilot, lambda: read_calls(fake))
    for _ in range(300):  # the worker outlives the app by the kill's 0.1 s poll
        if transcript_tail(week_home, 1)[0].text == "turn cancelled":
            break
        time.sleep(0.01)
    assert [ln.text for ln in transcript_tail(week_home, 2)] == ["hello", "turn cancelled"]


async def test_memory_and_history_popups(week_home):
    app = _app(week_home)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await pilot.press("M")
        await pilot.pause()
        assert _text(app, "#popup-text") == f"no notes\n\n{FORGET}"
        await pilot.press("escape")
        remember_at(week_home)
        await pilot.press("M")
        await pilot.pause()
        assert _text(app, "#popup-text") == f"#1 protect Monday math\n\n{FORGET}"
        await pilot.press("escape", "h")
        await pilot.pause()
        with closing(db.open_existing(week_home)) as conn:
            history = text.history_text(conn, since=TODAY - timedelta(days=7))
        assert history and _text(app, "#popup-text") == history
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1


async def test_the_empty_input_says_message_orrery_focused_or_not(week_home):
    """The empty input shows `› message orrery`, the same with the keyboard or without (the
    key line already says c chat)."""
    app = _app(week_home)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        line = app.query_one(ChatInput)
        assert line.render_line(0).text.strip() == "› message orrery" and not line.has_focus
        await pilot.press("c")
        await pilot.pause()
        assert line.render_line(0).text.strip() == "› message orrery" and line.has_focus


KEPT_LINE = "orrery: kept proposal #{}"
DISCARDED_LINE = "orrery: discarded proposal #{}; its requests stay pending"


@pytest.mark.parametrize(
    "typed, state, said",
    [
        ("y", "kept", KEPT_LINE),
        (" Y ", "kept", KEPT_LINE),
        ("n", "discarded", DISCARDED_LINE),
        ("N", "discarded", DISCARDED_LINE),
    ],
)
async def test_typing_y_or_n_answers_the_shown_proposal(week_home, tmp_path, typed, state, said):
    """A bare y or n typed while the diff shows is the y or n key: never sent to the
    organizer, no turn, and orrery says what happened in the chat, and only there."""
    with closing(db.open_existing(week_home)) as conn:
        p = propose_move(conn)
    fake = make_fake(tmp_path, {"events": [INIT_OK, result_ok("Kept.")]})
    app = _app(week_home, fake)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        assert "→ Fri" not in _week(app)
        await chat_keys(pilot, app, *typed, "enter")
        assert app.turn_cancel is None
        await settle(pilot, app)
        assert app.query_one(ChatInput).value == "" and not app.query_one("#note").display
        assert not app.query_one("#proposal").display and app.shown_proposal is None
        assert _talk(app) == [STARTED, said.format(p.id)]
        assert ("→ Fri 13" in _week(app)) == (state == "kept")  # the board shows the new plan
    assert not read_calls(fake)
    with closing(db.open_existing(week_home)) as conn:
        assert state_of(conn, p.id) == state


@pytest.mark.parametrize(
    "keys, said",
    [("y", KEPT_LINE), ("n", DISCARDED_LINE), ("yu", "orrery: undid the last kept proposal")],
)
async def test_the_y_n_and_u_keys_say_so_in_the_chat(week_home, keys, said):
    """orrery's line in the transcript: the answer, as news for the organizer's next turn. No
    note under it says it again."""
    with closing(db.open_existing(week_home)) as conn:
        p = propose_move(conn)
    app = _app(week_home)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        for key in keys:
            await pilot.press(key)
            await settle(pilot, app)
        assert _talk(app)[-1] == said.format(p.id) and not app.query_one("#note").display
    assert [(ln.speaker, ln.text) for ln in transcript_tail(week_home, 1)] == [
        ("orrery", said.format(p.id).removeprefix("orrery: "))
    ]


async def test_n_on_a_proposal_kept_in_another_window_says_stale(week_home):
    """Nothing is discarded, so orrery says nothing was, in the note or the chat."""
    with closing(db.open_existing(week_home)) as conn:
        p = propose_move(conn)
    app = _app(week_home)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        with closing(db.open_existing(week_home)) as conn:
            proposals.keep(conn, p.id, now=NOW, today=TODAY)
        await pilot.press("n")
        await settle(pilot, app)
        assert _text(app, "#note") == proposals.STALE
    assert [ln.text for ln in transcript_tail(week_home, 1)] == ["new chat started"]
    with closing(db.open_existing(week_home)) as conn:
        assert state_of(conn, p.id) == "kept"


async def test_a_replacement_shows_in_the_chat_as_orrery_says_it(week_home):
    """The organizer's new proposal names the one waiting: the chat says the old was not kept."""
    with closing(db.open_existing(week_home)) as conn:
        a = propose_move(conn)
    app = _app(week_home)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        with closing(db.open_existing(week_home)) as conn:
            b = propose_move(conn, rationale="lorem ipsum", replaces=a.id)
        await pilot.press("r")
        await settle(pilot, app)
        assert _talk(app) == [
            STARTED,
            f"orrery: proposal #{b.id} replaces #{a.id} (#{a.id} was not kept)",
        ]
        assert app.shown_proposal == b.id and not app.query_one("#note").display


async def test_a_keep_stands_when_its_chat_line_cannot_be_written(week_home, monkeypatch):
    """The keep commits before orrery's line: a locked chat can't make it read as failed."""
    with closing(db.open_existing(week_home)) as conn:
        p = propose_move(conn)
    app = _app(week_home)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)

        def locked(*_a, **_k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(sessions, "say", locked)
        await pilot.press("y")
        await settle(pilot, app)
        assert _text(app, "#note") == "kept" and "→ Fri 13" in _week(app)
    with closing(db.open_existing(week_home)) as conn:
        assert state_of(conn, p.id) == "kept"


@pytest.mark.parametrize("waiting, typed", [(False, "y"), (True, "yes please"), (True, "yes")])
async def test_other_lines_and_y_without_a_proposal_go_to_the_organizer(
    week_home, tmp_path, waiting, typed
):
    if waiting:
        with closing(db.open_existing(week_home)) as conn:
            p = propose_move(conn)
    fake = make_fake(tmp_path, {"events": [INIT_OK, delta("Press y."), result_ok("Press y.")]})
    app = _app(week_home, fake)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await chat_keys(pilot, app, *typed, "enter")
        await _until(pilot, lambda: _organizer_done(app))
        await settle(pilot, app)
        assert _talk(app)[-2:] == [f"you: {typed}", "organizer: Press y."]
        assert app.query_one("#proposal").display == waiting
    (call,) = read_calls(fake)
    assert call["stdin"].endswith(f"\n\n{typed}")
    if waiting:
        with closing(db.open_existing(week_home)) as conn:
            assert state_of(conn, p.id) == "waiting"


async def test_typing_y_while_the_organizer_answers_keeps_as_the_key_does(week_home, tmp_path):
    """The y key keeps mid-turn, so a typed y does too; the turn runs on."""
    with closing(db.open_existing(week_home)) as conn:
        p = propose_move(conn)
    fake = make_fake(
        tmp_path,
        {"events": [INIT_OK, delta("Let me look."), result_ok("Let me look.")], "linger": 30},
    )
    app = _app(week_home, fake)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await chat_keys(pilot, app, *"first", "enter")
        await _until(pilot, lambda: "organizer: … Let me look." in _talk(app))
        await pilot.press("y", "enter")
        await _until(pilot, lambda: KEPT_LINE.format(p.id) in _talk(app))
        assert app.query_one(ChatInput).value == "" and app.turn_cancel is not None
        await pilot.press("escape")
        await _until(pilot, lambda: _organizer_done(app))
        await settle(pilot, app)
    assert len(read_calls(fake)) == 1
    with closing(db.open_existing(week_home)) as conn:
        assert state_of(conn, p.id) == "kept"


def _plain(lines) -> list[str]:
    return [c.plain for c in lines]


def _styles(c) -> list[str]:
    """Each printed character's styles, from the spans over it."""
    return [
        " ".join(str(s.style) for s in c.spans if s.start <= i < s.end)
        for i, ch in enumerate(c.plain)
        if ch != " "
    ]


def _spans(c) -> list[tuple[str, str]]:
    return [(c.plain[s.start : s.end], str(s.style)) for s in c.spans]


def test_a_message_is_its_speaker_then_its_text_indented_at_any_width():
    """The speaker on a line of its own, then the text two columns in, its wrapped lines too;
    the organizer's **pairs** bold, its line breaks kept."""
    you = Line("", "you", "lorem ipsum dolor sit amet consectetur")
    assert _plain(render_line(you, 20)) == [
        "you",
        "  lorem ipsum dolor",
        "  sit amet",
        "  consectetur",
    ]
    assert _plain(render_line(you, 60)) == ["you", "  lorem ipsum dolor sit amet consectetur"]
    said = Line("", "organizer", "**Proposal #3 ready.** Tuesday gets Monday's math\nPress y.")
    assert _plain(render_line(said, 20)) == [
        "organizer",
        "  Proposal #3 ready.",
        "  Tuesday gets",
        "  Monday's math",
        "  Press y.",
    ]
    assert _plain(render_line(said, 60)) == [
        "organizer",
        "  Proposal #3 ready. Tuesday gets Monday's math",
        "  Press y.",
    ]
    head, first = render_line(said, 60)[:2]
    assert _spans(head) == [("organizer", "bold $text-primary")]
    assert _spans(first) == [("Proposal #3 ready.", "bold")]


def test_your_text_is_not_bold_and_reads_as_typed():
    you = render_line(Line("", "you", "**as typed** [b]x[/b]"), 40)
    assert _plain(you) == ["you", "  **as typed** [b]x[/b]"]
    assert _spans(you[0]) == [("you", "bold $text-accent")] and not you[1].spans


def test_orrery_says_its_lines_muted_after_a_dot():
    """orrery's own lines are `· <text>`, muted, wrapped two columns in; a chat's first line is
    its divider, as before."""
    line = Line("", "orrery", "discarded proposal #2; its requests stay pending")
    assert _plain(render_line(line, 20)) == [
        "· discarded proposal",
        "  #2; its requests",
        "  stay pending",
    ]
    assert _plain(render_line(line, 60)) == ["· discarded proposal #2; its requests stay pending"]
    assert all(set(_styles(c)) == {MUTED} for c in render_line(line, 20))
    divider = Line("2026-11-12T09:00:00", "orrery", "new chat started")
    assert _plain(render_line(divider, 60)) == [STARTED]


def test_a_folded_word_keeps_the_hanging_indent():
    """A word folded to fill its line leaves no space at the next one's start; CJK text,
    folded whole, neither; an indent after a line break stays as written."""
    long = render_line(Line("", "organizer", "a" * 76 + " bb cc"), 40)
    assert _plain(long) == ["organizer", "  " + "a" * 38, "  " + "a" * 38, "  bb cc"]
    cjk = render_line(Line("", "organizer", "面" * 38 + " y 保留。"), 40)
    assert _plain(cjk) == ["organizer", "  " + "面" * 19, "  " + "面" * 19, "  y 保留。"]
    dot = render_line(Line("", "orrery", "a" * 76 + " bb"), 40)
    assert _plain(dot) == ["· " + "a" * 38, "  " + "a" * 38, "  bb"]
    kept = render_line(Line("", "you", "one\n  indented two"), 40)
    assert _plain(kept) == ["you", "  one", "    indented two"]


def test_an_organizer_line_shows_its_bold_and_drops_unpaired_markers():
    """Content spans built by hand: only ** pairs are read, never markup; a stray ** goes."""
    said = Line("", "organizer", "**Proposal #1 ready.** see **Mon** [b]and[/b] **")
    c = render_line(said, 80)[1]
    assert c.plain.rstrip() == "  Proposal #1 ready. see Mon [b]and[/b]"
    assert [t for t, style in _spans(c) if style == "bold"] == ["Proposal #1 ready.", "Mon"]
    assert render_line(Line("", "organizer", "a ** b"), 80)[1].plain == "  a  b"
    maths = render_line(Line("", "organizer", "x**2 + y**2"), 80)[1]  # inside words: as written
    assert maths.plain == "  x**2 + y**2" and not maths.spans


def test_bold_next_to_chinese_text_is_still_bold():
    """CJK characters aren't ASCII word characters: a pair beside them is bold, and a stray
    marker beside them goes whole."""
    c = render_line(Line("", "organizer", "今天**数学**很好"), 80)[1]
    assert c.plain == "  今天数学很好" and _spans(c) == [("数学", "bold")]
    assert render_line(Line("", "organizer", "提案**已就绪"), 80)[1].plain == "  提案已就绪"


def test_the_turn_in_progress_is_its_latest_words_muted_after_an_ellipsis():
    live = render_line(Line("", "organizer", "reading the **pending** requests"), 20, live=True)
    assert _plain(live) == ["organizer", "  … reading the", "    pending requests"]
    assert _spans(live[0]) == [("organizer", "bold $text-primary")]
    assert all(MUTED in s.split() for c in live[1:] for s in _styles(c))
    assert _styles(live[2])[0] == f"{MUTED} bold"  # "pending": muted, and still bold


def test_the_strip_says_the_newest_speaker_and_first_line():
    said = Line("", "organizer", "**Proposal #3 ready.**\nTuesday gets Monday's math.")
    assert summary(said) == "organizer: Proposal #3 ready. …"
    assert summary(Line("", "you", "**as typed**")) == "you: **as typed**"
    assert summary(Line("", "orrery", "kept proposal #3")) == "orrery: kept proposal #3"
    assert summary(Line("", "organizer", "Reading."), live=True) == "organizer: … Reading."
    assert summary(Line("2026-11-12T09:00:00", "orrery", "new chat started")) == STARTED


async def test_the_strip_shows_the_newest_message_on_one_line(week_home):
    with closing(db.open_existing(week_home)) as conn:
        s = sessions.ensure(conn, NOW)
        sessions.say(conn, s.id, "you", "lorem ipsum?", NOW)
        sessions.say(conn, s.id, "organizer", "**Proposal #3 ready.**\nPress y to keep it.", NOW)
    app = _app(week_home)
    async with app.run_test(size=(100, 32)) as pilot:
        await settle(pilot, app)
        assert app.query_one("#chat").region.height == 1
        assert _text(app, "#strip") == "organizer: Proposal #3 ready. …"


REVIEW = prompt.review_line(
    [
        channels.Message(i, ch, "request", f"mentor:{ch}", {}, "x", None, f"r{i}", "pending", "")
        for i, ch in enumerate(("math", "finance", "math"))
    ]
)
TALK = (
    ("you", "have you updated the schedule?"),
    ("organizer", "Not yet: #1 is still waiting."),
    ("orrery", "discarded proposal #1; its requests stay pending"),
    ("orrery", REVIEW),  # the line the morning review records, as it is
    (
        "organizer",
        "**Proposal #3 ready.** Math learn moves from Thursday to Friday; finance rep stays on"
        " Saturday.",
    ),
    ("orrery", "kept proposal #2"),
)


def _drawn(app: OrreryApp) -> list[str]:
    """The log as drawn, a line per screen row from its top, scrolled or not: each row's lines
    at its place, so the blank lines between them are their margins."""
    out: list[str] = []
    for w in app.query("#log > .row"):
        if w.display:
            out += [""] * (w.virtual_region.y - len(out))
            out += [s.text.rstrip() for s in w.render_lines(Region(0, 0, *w.size))]
    return out


async def test_the_chat_reads_as_messages_a_blank_line_apart(week_home):
    """Each message its speaker's line and its text indented, a blank line between; orrery's
    lines together, a blank line around the group; all wrapped to the panel, as wide as it is
    after a resize."""
    with closing(db.open_existing(week_home)) as conn:
        s = sessions.ensure(conn, NOW)
        for speaker, text in TALK:
            sessions.say(conn, s.id, speaker, text, NOW)
    app = _app(week_home)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        assert {w.size.width for w in app.query("#log > .row") if w.display} == {33}
        assert _drawn(app) == [
            STARTED,
            "",
            "you",
            "  have you updated the schedule?",
            "",
            "organizer",
            "  Not yet: #1 is still waiting.",
            "",
            "· discarded proposal #1; its",
            "  requests stay pending",
            "· morning review: 3 pending",
            "  requests (finance, math)",
            "",
            "organizer",
            "  Proposal #3 ready. Math learn",
            "  moves from Thursday to Friday;",
            "  finance rep stays on Saturday.",
            "",
            "· kept proposal #2",
        ]
        await pilot.resize_terminal(235, 60)
        await _until(pilot, lambda: app.query_one("#chat").region.width > 37)
        await settle(pilot, app)
        assert _drawn(app) == [
            STARTED,
            "",
            "you",
            "  have you updated the schedule?",
            "",
            "organizer",
            "  Not yet: #1 is still waiting.",
            "",
            "· discarded proposal #1; its requests stay pending",
            "· morning review: 3 pending requests (finance, math)",
            "",
            "organizer",
            "  Proposal #3 ready. Math learn moves from Thursday to Friday;",
            "  finance rep stays on Saturday.",
            "",
            "· kept proposal #2",
        ]


async def test_a_line_said_just_now_keeps_the_spacing(week_home, tmp_path):
    """Your line, sent: a blank line above it, after orrery's; the turn in progress under it
    a blank line apart too, and the line saying it was cancelled after it."""
    with closing(db.open_existing(week_home)) as conn:
        sessions.say(conn, sessions.ensure(conn, NOW).id, "orrery", "kept proposal #1", NOW)
    fake = make_fake(tmp_path, {"events": [INIT_OK, result_ok("x")], "sleep": 30})
    app = _app(week_home, fake)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await chat_keys(pilot, app, *"hello", "enter")
        await _until(pilot, lambda: read_calls(fake))
        await pilot.pause()
        assert _drawn(app) == [
            STARTED,
            "",
            "· kept proposal #1",
            "",
            "you",
            "  hello",
            "",
            "organizer",
            "  … working (esc cancels)",
        ]
        await pilot.press("escape")
        await _until(pilot, lambda: _organizer_done(app))
        await settle(pilot, app)
        assert _drawn(app)[-4:] == ["you", "  hello", "", "· turn cancelled"]


async def test_the_commentary_shows_while_the_turn_runs_and_only_the_reply_stays(
    week_home, tmp_path
):
    """What the organizer says before a tool call shows, muted, while it works; the reply is
    what it says last, and only that is kept."""
    working = [INIT_OK, block_start(), delta("Reading the requests."), block_start("tool_use")]
    done = [*working, block_start(), delta("Moved."), result_ok("Moved.")]
    fake = make_fake(tmp_path, {"events": working, "linger": 30}, {"events": done})
    app = _app(week_home, fake)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await chat_keys(pilot, app, *"hi", "enter")
        await _until(pilot, lambda: "organizer: … Reading the requests." in _talk(app))
        assert _drawn(app)[-2:] == ["organizer", "  … Reading the requests."]
        await pilot.press("escape")
        await _until(pilot, lambda: _organizer_done(app))
        await settle(pilot, app)
        assert _talk(app)[-2:] == ["you: hi", "orrery: turn cancelled"]  # none of it kept
        await pilot.press(*"again", "enter")
        await _until(pilot, lambda: _organizer_done(app))
        await settle(pilot, app)
        assert _talk(app)[-2:] == ["you: again", "organizer: Moved."]
        assert not app.query_one("#live").display
    assert "Reading the requests." not in [ln.text for ln in transcript_tail(week_home, 10)]
