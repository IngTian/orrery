"""Test helpers for orrery."""

import datetime as dt
import itertools
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from conftest import FROZEN_NOW

from orrery import channels, db, ops, proposals
from orrery.organizer import sessions

NOW = FROZEN_NOW
TODAY = dt.date(2026, 11, 12)

_counter = itertools.count()


def propose_move(conn: sqlite3.Connection, **kw) -> proposals.Proposal:
    """Propose Move(THU math learn → FRI) as the organizer; kw overrides propose's arguments."""
    args = dict(
        ops=[ops.Move(TODAY, "math", "learn", TODAY + dt.timedelta(days=1))],
        rationale="",
        dispositions=[],
        author="organizer",
        now=NOW,
        today=TODAY,
    )
    return proposals.propose(conn, **{**args, **kw})


def keep_move(
    conn: sqlite3.Connection,
    date: dt.date,
    subject: str,
    kind: str,
    to: dt.date,
    sessions: int | None = None,
) -> int:
    """Propose one move and keep it; returns the change id."""
    p = propose_move(conn, ops=[ops.Move(date, subject, kind, to, sessions)])
    return proposals.keep(conn, p.id, now=NOW, today=TODAY)


def state_of(conn: sqlite3.Connection, pid: int) -> str:
    return conn.execute("select state from proposals where id = ?", (pid,)).fetchone()[0]


def reinsert_waiting(conn: sqlite3.Connection, p: proposals.Proposal) -> proposals.Proposal:
    """A copy of p as the waiting proposal, as if it had never been replaced."""
    cur = conn.execute(
        "insert into proposals (created_at, author, ops, rationale, dispositions, expected, state)"
        " select created_at, author, ops, rationale, dispositions, expected, 'waiting'"
        " from proposals where id = ?",
        (p.id,),
    )
    return proposals.get(conn, cur.lastrowid)


def store_waiting(conn: sqlite3.Connection, raw_ops: list[dict]) -> int:
    """A waiting proposal stored as is, past propose's checks (as an older orrery might have)."""
    return conn.execute(
        "insert into proposals (created_at, author, ops, expected, state)"
        " values ('2026-11-12T09:00:00', 'organizer', ?, '{}', 'waiting')",
        (json.dumps(raw_ops),),
    ).lastrowid


def report_and_collect(
    conn: sqlite3.Connection, date: dt.date, subject: str, kind: str, sessions: int, **kw
) -> None:
    """Report progress and collect it; kw overrides report's arguments (status, note, times)."""
    key = f"t:{date}:{subject}:{kind}:{next(_counter)}"
    args = dict(status="done", note="", key=key, now=NOW, boundary_hour=5)
    channels.report(
        conn, channel=subject, date=date, kind=kind, sessions=sessions, **{**args, **kw}
    )
    channels.collect(conn, now=NOW, today=TODAY)


def add_agenda(conn: sqlite3.Connection, subject: str, entries: list[channels.AgendaEntry]) -> None:
    """Add agenda entries and collect them."""
    channels.agenda(conn, channel=subject, entries=entries, now=NOW)
    channels.collect(conn, now=NOW, today=TODAY)


def keep_move_at(
    home: Path,
    date: dt.date,
    subject: str,
    kind: str,
    to: dt.date,
    sessions: int | None = None,
) -> int:
    """keep_move on a connection of its own, closed after."""
    with closing(db.open_existing(home)) as conn:
        return keep_move(conn, date, subject, kind, to, sessions)


def add_agenda_at(home: Path, subject: str, entries: list[channels.AgendaEntry]) -> None:
    """add_agenda on a connection of its own, closed after."""
    with closing(db.open_existing(home)) as conn:
        add_agenda(conn, subject, entries)


def propose_move_at(home: Path) -> None:
    """propose_move on a connection of its own, closed after."""
    with closing(db.open_existing(home)) as conn:
        propose_move(conn)


def remember_at(home: Path, text: str = "protect Monday math") -> None:
    """sessions.remember on a connection of its own, closed after."""
    with closing(db.open_existing(home)) as conn:
        sessions.remember(conn, text, NOW)


async def settle(pilot, app) -> None:
    """The messages handled (a key's Shift reaches the app, which may start a reload), every
    worker finished (a finished worker leaves the manager), then the screen redrawn."""
    await pilot.pause()
    for _ in range(500):
        if not app.workers:
            break
        await pilot.pause(0.01)
    else:
        raise AssertionError("workers never finished")
    await pilot.pause()


async def leave_chat(pilot, app, *keys: str) -> None:
    """escape, then keys once the chat input has let go of the keyboard: pressed in one go
    they race escape's binding as chat_keys' race c's."""
    from orrery.tui.chat import ChatInput

    await pilot.press("escape")
    for _ in range(500):
        if not isinstance(app.focused, ChatInput):
            break
        await pilot.pause(0.01)
    else:
        raise AssertionError("escape never took the keyboard from the chat input")
    await pilot.press(*keys)


async def chat_keys(pilot, app, *keys: str) -> None:
    """c, then keys once the chat input has the keyboard. Pressed in one go they race c's
    binding, which lands only after the key has bubbled back up from the view: a key the
    app reads before then goes to the view, which still has the keyboard."""
    from orrery.tui.chat import ChatInput

    await pilot.press("c")
    for _ in range(500):
        if isinstance(app.focused, ChatInput):
            break
        await pilot.pause(0.01)
    else:
        raise AssertionError("c never gave the chat input the keyboard")
    await pilot.press(*keys)
