"""The organizer chat on screen, through the Pilot: a turn streamed into the docked panel, esc,
a second line while answering, x's confirm, remembered notes, the M and h popups, and the
transcript across restarts. The organizer is tests/fake_claude.py's fake, never the real CLI."""

import threading
import time
from contextlib import closing, contextmanager
from datetime import timedelta

from fake_claude import INIT_OK, delta, make_fake, read_calls, result_ok, transcript_tail
from helpers import NOW, TODAY, chat_keys, propose_move, remember_at, settle, state_of
from test_tui import SIZE, _text, _until
from textual.geometry import Region
from textual.strip import Strip

from orrery import db, store, text
from orrery.organizer import sessions, turn
from orrery.tui import keymap
from orrery.tui.app import OrreryApp
from orrery.tui.chat import ChatInput
from orrery.tui.views import Confirm

STILL = "the organizer is still answering; esc cancels"
QUESTION = "End this organizer session and start a new one? Its transcript is kept; memory stays."
FORGET = "ask the organizer to forget a note"
STARTED = "── new chat, Thu 11/12 ──"


def _app(home, fake=None) -> OrreryApp:
    return OrreryApp(
        root=lambda: home, now=lambda: NOW, claude_path=str(fake) if fake is not None else None
    )


def _talk(app: OrreryApp) -> list[str]:
    """The chat's rows as shown, oldest first: the transcript, then the reply streaming in."""
    return [str(w.content) for w in app.query("#log > .row") if w.display]


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
        assert _talk(app)[-2:] == ["you: hello", "organizer: working… (esc cancels)"]
        start = time.monotonic()
        await pilot.press("escape")
        await _until(pilot, lambda: "orrery: turn cancelled" in _talk(app))
        assert time.monotonic() - start < 2
        await settle(pilot, app)
        assert _talk(app)[-2:] == ["you: hello", "orrery: turn cancelled"]
        assert app.turn_cancel is None and _text(app, "#note") == "turn cancelled"
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
        await _until(pilot, lambda: "organizer: Let me look." in _talk(app))
        assert "organizer: working… (esc cancels)" not in _talk(app)
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
