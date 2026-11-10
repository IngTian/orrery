"""The organizer's once-a-day morning review: on the first suitable reload (no turn, no proposal,
requests pending), it upserts last_review and starts a turn with the review line as orrery."""

import sqlite3
import time
from contextlib import closing
from datetime import timedelta

from fake_claude import INIT_OK, delta, make_fake, read_calls, result_ok
from helpers import NOW, TODAY, chat_keys, propose_move, settle
from test_tui import SIZE, _text, _until

from orrery import channels, db
from orrery.organizer import prompt
from orrery.tui.app import OrreryApp
from orrery.tui.chat import ChatInput


def _app(home, fake=None) -> OrreryApp:
    return OrreryApp(
        root=lambda: home, now=lambda: NOW, claude_path=str(fake) if fake is not None else None
    )


def _organizer_done() -> bool:
    """Callable condition for _until."""
    # Will be set by tests
    return _organizer_done.app is not None and not [
        w for w in _organizer_done.app.workers if w.group == "organizer"
    ]


_organizer_done.app = None


def _last_review(home) -> str | None:
    with closing(db.open_existing(home)) as conn:
        row = conn.execute("select value from settings where key = 'last_review'").fetchone()
        return row[0] if row else None


def _add_request(home, channel, text, kind, delta, key) -> None:
    with closing(db.open_existing(home)) as conn:
        channels.request(
            conn, channel=channel, text=text, kind=kind, delta=delta, by=None, key=key, now=NOW
        )


async def test_one_review_per_day(week_home, tmp_path):
    """Two reloads same day: first starts review, second doesn't; no empty changes."""
    _add_request(week_home, "math", "more learn", "learn", 1, "r1")
    fake = make_fake(tmp_path, {"events": [INIT_OK, delta("Sure."), result_ok("Sure.")]})
    app = _app(week_home, fake)
    _organizer_done.app = app
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await _until(pilot, _organizer_done)
        await settle(pilot, app)
        assert len(read_calls(fake)) == 1
        assert "morning review: 1 pending request (math)" in read_calls(fake)[0]["stdin"]
        assert _last_review(week_home) == TODAY.isoformat()
        # Exactly one review change with history
        with closing(db.open_existing(week_home)) as conn:
            cnt = conn.execute(
                "select count(*) from changes c join history h on h.change_id = c.id"
                " where c.kind = 'review'"
            ).fetchone()[0]
            assert cnt == 1
        # Second reload: no new turn, no new change
        app.app_reload()
        await settle(pilot, app)
        await _until(pilot, _organizer_done)
        await settle(pilot, app)
        assert len(read_calls(fake)) == 1  # still just one call
        with closing(db.open_existing(week_home)) as conn:
            cnt = conn.execute("select count(*) from changes where kind = 'review'").fetchone()[0]
            assert cnt == 1  # still just one review change


async def test_no_review_with_no_requests(week_home, tmp_path):
    """No pending requests: no review turn, but the first open claims the day."""
    fake = make_fake(tmp_path, {"events": [INIT_OK, result_ok("reply")]})
    app = _app(week_home, fake)
    _organizer_done.app = app
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await _until(pilot, _organizer_done)
        await settle(pilot, app)
        assert len(read_calls(fake)) == 0
        assert _last_review(week_home) == TODAY.isoformat()


async def test_late_request_waits_through_a_chat_turn(week_home, tmp_path):
    """No requests at the first open; one filed later; the user's chat turn ends and its reload
    starts no review: exactly one call, the chat turn."""
    fake = make_fake(tmp_path, {"events": [INIT_OK, delta("Sure."), result_ok("Sure.")]})
    app = _app(week_home, fake)
    _organizer_done.app = app
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        _add_request(week_home, "math", "more learn", "learn", 1, "late")
        await chat_keys(pilot, app, *"what's on friday?", "enter")
        await _until(pilot, lambda: read_calls(fake) and _organizer_done())
        await settle(pilot, app)
        await _until(pilot, _organizer_done)  # a review would have started on that reload
        await settle(pilot, app)
        calls = read_calls(fake)
        assert len(calls) == 1 and calls[0]["stdin"].endswith("\n\nwhat's on friday?")


async def test_late_request_is_reviewed_next_morning(week_home, tmp_path):
    """A request filed after the first open waits; the next day's first reload reviews it."""
    clock = [NOW]
    fake = make_fake(tmp_path, {"events": [INIT_OK, delta("Sure."), result_ok("Sure.")]})
    app = OrreryApp(root=lambda: week_home, now=lambda: clock[0], claude_path=str(fake))
    _organizer_done.app = app
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        _add_request(week_home, "math", "more learn", "learn", 1, "late")
        app.app_reload()
        await settle(pilot, app)
        assert read_calls(fake) == []
        clock[0] = NOW + timedelta(days=1)
        app._check_day()  # the minute tick, past the day boundary
        await _until(pilot, lambda: read_calls(fake) and _organizer_done())
        await settle(pilot, app)
        (call,) = read_calls(fake)
        assert "morning review: 1 pending request (math)" in call["stdin"]
        assert _last_review(week_home) == (TODAY + timedelta(days=1)).isoformat()


async def test_no_review_while_proposal_waiting(week_home, tmp_path):
    """A waiting proposal blocks the review; once discarded, the next reload starts it."""
    _add_request(week_home, "math", "more learn", "learn", 1, "r1")
    # Add a waiting proposal
    with closing(db.open_existing(week_home)) as conn:
        propose_move(conn)
    fake = make_fake(tmp_path, {"events": [INIT_OK, delta("Sure."), result_ok("Sure.")]})
    app = _app(week_home, fake)
    _organizer_done.app = app
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await _until(pilot, _organizer_done)
        await settle(pilot, app)
        assert len(read_calls(fake)) == 0  # no review while proposal is waiting
        # Discard the proposal
        await pilot.press("n")
        await settle(pilot, app)
        await _until(pilot, _organizer_done)
        await settle(pilot, app)
        assert len(read_calls(fake)) == 1  # now the review runs
        assert "morning review: 1 pending request (math)" in read_calls(fake)[0]["stdin"]


async def test_new_request_after_review_starts_no_turn(week_home, tmp_path):
    """First request triggers review; a second request the same day starts no turn."""
    _add_request(week_home, "math", "more learn", "learn", 1, "r1")
    fake = make_fake(tmp_path, {"events": [INIT_OK, delta("Sure."), result_ok("Sure.")]})
    app = _app(week_home, fake)
    _organizer_done.app = app
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await _until(pilot, _organizer_done)
        await settle(pilot, app)
        assert len(read_calls(fake)) == 1
        # Add another request the same day
        _add_request(week_home, "finance", "more rep", "rep", 1, "r2")
        app.app_reload()
        await settle(pilot, app)
        await _until(pilot, _organizer_done)
        await settle(pilot, app)
        assert len(read_calls(fake)) == 1  # still just one review


async def test_two_apps_start_one_review(week_home, tmp_path):
    """Two apps mounting concurrently: exactly one review turn, one change, no BUSY."""
    _add_request(week_home, "math", "more learn", "learn", 1, "r1")
    fake = make_fake(tmp_path, {"events": [INIT_OK, delta("Sure."), result_ok("Sure.")]})
    app1 = _app(week_home, fake)
    app2 = _app(week_home, fake)

    def both_done():
        return not [w for w in app1.workers if w.group == "organizer"] and not [
            w for w in app2.workers if w.group == "organizer"
        ]

    async with app1.run_test(size=SIZE) as pilot1:
        async with app2.run_test(size=SIZE) as pilot2:
            await settle(pilot1, app1)
            await settle(pilot2, app2)
            await _until(pilot1, both_done)
            await settle(pilot1, app1)
            await settle(pilot2, app2)
            # Exactly one review change with history
            with closing(db.open_existing(week_home)) as conn:
                cnt = conn.execute(
                    "select count(*) from changes c join history h on h.change_id = c.id"
                    " where c.kind = 'review'"
                ).fetchone()[0]
                assert cnt == 1
            # Exactly one review turn
            calls = read_calls(fake)
            review_calls = [c for c in calls if "morning review:" in c["stdin"]]
            assert len(review_calls) == 1
            # Neither app shows BUSY in note
            assert "busy" not in _text(app1, "#note").lower()
            assert "busy" not in _text(app2, "#note").lower()


async def test_review_line_text_exact(week_home):
    """The review line is a fact, its count and channels: the transcript shows it, and the
    instruction it carries is the system prompt's, never the owner's to read."""
    # One request
    pending = [
        channels.Message(
            1, "math", "request", "mentor:math", {}, "more learn", None, "r1", "pending", ""
        )
    ]
    assert prompt.review_line(pending) == "morning review: 1 pending request (math)"
    assert "When orrery's line is the morning review" in prompt.system()
    # Multiple requests from different channels
    pending = [
        channels.Message(
            1, "math", "request", "mentor:math", {}, "more learn", None, "r1", "pending", ""
        ),
        channels.Message(
            2, "finance", "request", "mentor:finance", {}, "more rep", None, "r2", "pending", ""
        ),
        channels.Message(
            3, "math", "request", "mentor:math", {}, "even more", None, "r3", "pending", ""
        ),
    ]
    assert prompt.review_line(pending) == "morning review: 3 pending requests (finance, math)"


async def test_claim_review_direct(week_home):
    """The claim function's upsert rejects if last_review == today."""
    app = _app(week_home)
    # First claim succeeds
    assert app._claim_review(week_home, TODAY, NOW)
    assert _last_review(week_home) == TODAY.isoformat()
    # Second claim (same day, bypassing cheap read) returns False, no change
    with closing(db.open_existing(week_home)) as conn:
        before_cnt = conn.execute("select count(*) from changes where kind = 'review'").fetchone()[
            0
        ]
    assert not app._claim_review(week_home, TODAY, NOW)
    with closing(db.open_existing(week_home)) as conn:
        after_cnt = conn.execute("select count(*) from changes where kind = 'review'").fetchone()[0]
    assert after_cnt == before_cnt  # no new change


async def test_review_setup_crash_keeps_app_up(week_home, tmp_path):
    """If the claim fails (db error), the app stays up."""
    _add_request(week_home, "math", "more learn", "learn", 1, "r1")
    fake = make_fake(tmp_path, {"events": [INIT_OK, result_ok("reply")]})
    app = _app(week_home, fake)
    _organizer_done.app = app

    # Make _claim_review raise

    def crash_claim(root, today, now):
        raise sqlite3.OperationalError("test crash")

    app._claim_review = crash_claim

    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await _until(pilot, _organizer_done)
        await settle(pilot, app)
        # App is still running
        assert app.is_running
        # No review turn
        assert len(read_calls(fake)) == 0


async def test_busy_review_does_not_loop(week_home, tmp_path):
    """Hold the organizer lock for ~1s: exactly 1 review change, then app is usable."""
    import threading

    from orrery import store
    from orrery.organizer import turn

    _add_request(week_home, "math", "more learn", "learn", 1, "r1")
    fake = make_fake(tmp_path, {"events": [INIT_OK, result_ok("reply")]})
    app = _app(week_home, fake)
    _organizer_done.app = app

    lock_held = threading.Event()
    lock_released = threading.Event()

    def hold_lock():
        with store.file_lock("organizer", blocking=True):
            lock_held.set()
            time.sleep(1)
            lock_released.set()

    lock_thread = threading.Thread(target=hold_lock, daemon=True)
    lock_thread.start()
    assert lock_held.wait(timeout=2), "Lock thread didn't start"

    async with app.run_test(size=SIZE) as pilot:
        # Let the app try to review while lock is held
        await settle(pilot, app)
        await _until(pilot, _organizer_done)
        await settle(pilot, app)
        # Note shows BUSY while lock is held
        assert _text(app, "#note") == turn.BUSY
        # Exactly 1 review change written (BUSY doesn't release and retry)
        with closing(db.open_existing(week_home)) as conn:
            review_cnt = conn.execute(
                "select count(*) from changes where kind = 'review'"
            ).fetchone()[0]
            assert review_cnt == 1
        # Wait for lock release
        assert lock_released.wait(timeout=2), "Lock thread didn't release"
        lock_thread.join(timeout=1)
        # App is usable: turn_cancel is None, c works
        assert app.turn_cancel is None
        await pilot.press("c")
        await pilot.pause(0.1)
        assert app.query_one(ChatInput).has_focus


async def test_lost_claim_starts_no_turn(week_home, tmp_path):
    """The reload read last_review before today's claim committed (another window's, or this
    one's at open): the claim loses, so no turn starts, nothing is called, nothing is
    written."""
    fake = make_fake(tmp_path, {"events": [INIT_OK, delta("Sure."), result_ok("Sure.")]})
    app = _app(week_home, fake)
    _organizer_done.app = app
    pending = (
        channels.Message(
            1, "math", "request", "mentor:math", {}, "more learn", None, "r1", "pending", ""
        ),
    )
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)  # no requests at mount: the day is claimed, no review
        assert _last_review(week_home) == TODAY.isoformat()
        assert app.turn_cancel is None and app.shown_proposal is None
        app._maybe_review(pending, TODAY, None)  # stale read: last_review not today
        assert app.turn_cancel is None
        await _until(pilot, _organizer_done)
        await settle(pilot, app)
        assert read_calls(fake) == []
        assert "busy" not in _text(app, "#note").lower()
        with closing(db.open_existing(week_home)) as conn:
            cnt = conn.execute("select count(*) from changes where kind = 'review'").fetchone()[0]
            assert cnt == 1  # only the claim at open
