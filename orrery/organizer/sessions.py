"""Organizer sessions, transcript, and memory."""

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from orrery import ledger
from orrery.text import has_verdict


@dataclass(frozen=True)
class Session:
    id: int
    claude_id: str
    claude_live: bool
    started_at: str


@dataclass(frozen=True)
class Line:
    at: str
    speaker: str
    text: str
    id: int = 0  # set by all_lines
    session_id: int = 0


@dataclass(frozen=True)
class Note:
    id: int
    text: str
    created_at: str


def active(conn: sqlite3.Connection) -> Session | None:
    """The active session, or None."""
    row = conn.execute(
        "select id, claude_id, claude_live, started_at from organizer_sessions where active = 1"
    ).fetchone()
    if row is None:
        return None
    return Session(row[0], row[1], bool(row[2]), row[3])


def ensure(conn: sqlite3.Connection, now: datetime) -> Session:
    """Inside one BEGIN IMMEDIATE, re-read active; if there is none, insert one with a uuid4
    claude_id plus a transcript row ("orrery", "new chat started")."""
    if conn.in_transaction:
        raise RuntimeError("ensure must not be called while a transaction is open")
    conn.execute("BEGIN IMMEDIATE")
    try:
        s = active(conn)
        if s is not None:
            conn.execute("COMMIT")
            return s
        at = now.isoformat(timespec="seconds")
        sid = conn.execute(
            "insert into organizer_sessions (started_at, claude_id) values (?, ?)",
            (at, str(uuid.uuid4())),
        ).lastrowid
        conn.execute(
            "insert into transcript (session_id, at, speaker, text) values (?, ?, 'orrery',"
            " 'new chat started')",
            (sid, at),
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    assert sid is not None
    s = active(conn)
    assert s is not None
    return s


def end_and_restart(conn: sqlite3.Connection, now: datetime) -> Session:
    """One BEGIN IMMEDIATE: set the active session's active=0 and ended_at; set any waiting
    proposal to discarded, whatever its session_id (proposals is a free table); insert a new
    session and its "new chat started" row."""
    if conn.in_transaction:
        raise RuntimeError("end_and_restart must not be called while a transaction is open")
    conn.execute("BEGIN IMMEDIATE")
    try:
        at = now.isoformat(timespec="seconds")
        conn.execute(
            "update organizer_sessions set active = 0, ended_at = ? where active = 1", (at,)
        )
        conn.execute(
            "update proposals set state = 'discarded', decided_at = ? where state = 'waiting'",
            (at,),
        )
        sid = conn.execute(
            "insert into organizer_sessions (started_at, claude_id) values (?, ?)",
            (at, str(uuid.uuid4())),
        ).lastrowid
        conn.execute(
            "insert into transcript (session_id, at, speaker, text) values (?, ?, 'orrery',"
            " 'new chat started')",
            (sid, at),
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    assert sid is not None
    s = active(conn)
    assert s is not None
    return s


def mark_live(conn: sqlite3.Connection, session_id: int) -> None:
    """Mark the session as claude_live."""
    conn.execute("update organizer_sessions set claude_live = 1 where id = ?", (session_id,))


def reseed(conn: sqlite3.Connection, session_id: int) -> Session:
    """A new uuid4 claude_id with claude_live=0, under the same id."""
    conn.execute(
        "update organizer_sessions set claude_id = ?, claude_live = 0 where id = ?",
        (str(uuid.uuid4()), session_id),
    )
    row = conn.execute(
        "select id, claude_id, claude_live, started_at from organizer_sessions where id = ?",
        (session_id,),
    ).fetchone()
    return Session(row[0], row[1], bool(row[2]), row[3])


def say(
    conn: sqlite3.Connection,
    session_id: int,
    speaker: Literal["you", "organizer", "orrery"],
    text: str,
    now: datetime,
) -> int:
    """Insert one transcript row (free insert; no change needed) and return its id."""
    tid = conn.execute(
        "insert into transcript (session_id, at, speaker, text) values (?, ?, ?, ?)",
        (session_id, now.isoformat(timespec="seconds"), speaker, text),
    ).lastrowid
    assert tid is not None
    return tid


def transcript(conn: sqlite3.Connection, session_id: int, limit: int | None = None) -> list[Line]:
    """Transcript lines, oldest first, the last limit."""
    query = "select at, speaker, text from transcript where session_id = ? order by id"
    if limit is not None:
        offset_query = f"select max(0, count(*) - {limit}) from transcript where session_id = ?"
        query += f" limit {limit} offset ({offset_query})"
        rows = conn.execute(query, (session_id, session_id)).fetchall()
    else:
        rows = conn.execute(query, (session_id,)).fetchall()
    return [Line(at, speaker, text) for at, speaker, text in rows]


def all_lines(
    conn: sqlite3.Connection, *, before_id: int | None = None, limit: int = 100
) -> list[Line]:
    """Every session's transcript, newest first, a page of limit rows older than before_id.
    Each session's first row is its "new chat started"."""
    rows = conn.execute(
        "select at, speaker, text, id, session_id from transcript"
        " where ? is null or id < ? order by id desc limit ?",
        (before_id, before_id, limit),
    )
    return [Line(*r) for r in rows]


def unheard(conn: sqlite3.Connection, session_id: int) -> list[str]:
    """orrery's rows since the organizer's newest reply in the session, oldest first: what a
    resumed Claude session was never told, such as a keep or a discard."""
    # ponytail: a row said mid-turn sits before that turn's reply, so it is missed; the keep or
    # discard it would report reaches prompt.state from the proposals table anyway. A heard_id
    # on organizer_sessions if another mid-turn line comes to matter.
    rows = conn.execute(
        "select text from transcript where session_id = ? and speaker = 'orrery' and id >"
        " (select coalesce(max(id), 0) from transcript where session_id = ?"
        " and speaker = 'organizer') order by id",
        (session_id, session_id),
    )
    return [r[0] for r in rows]


def has_talk(conn: sqlite3.Connection, session_id: int) -> bool:
    """True if any rows by you or the organizer exist."""
    row = conn.execute(
        "select 1 from transcript where session_id = ? and speaker in ('you', 'organizer') limit 1",
        (session_id,),
    ).fetchone()
    return row is not None


def memory(conn: sqlite3.Connection) -> list[Note]:
    """All memory notes."""
    rows = conn.execute("select id, text, created_at from memory order by id").fetchall()
    return [Note(nid, text, created_at) for nid, text, created_at in rows]


def remember(conn: sqlite3.Connection, text: str, now: datetime) -> int:
    """Its own change: actor="organizer", kind="memory". It raises ValueError("reword: " + text)
    when has_verdict, and ValueError("empty note") for an empty one."""
    if not text:
        raise ValueError("empty note")
    if has_verdict(text):
        raise ValueError("reword: " + text)
    with ledger.change(conn, actor="organizer", kind="memory", now=now):
        nid = conn.execute(
            "insert into memory (text, created_at) values (?, ?)",
            (text, now.isoformat(timespec="seconds")),
        ).lastrowid
    assert nid is not None
    return nid


def forget(conn: sqlite3.Connection, note_id: int, now: datetime) -> None:
    """Its own change. An unknown id gives ValueError("no note #{id}")."""
    row = conn.execute("select 1 from memory where id = ?", (note_id,)).fetchone()
    if row is None:
        raise ValueError(f"no note #{note_id}")
    with ledger.change(conn, actor="organizer", kind="memory", now=now):
        conn.execute("delete from memory where id = ?", (note_id,))
