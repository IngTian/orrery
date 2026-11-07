"""Tests for organizer sessions, transcript, and memory."""

import sqlite3

import pytest
from conftest import FROZEN_NOW
from helpers import NOW, propose_move

from orrery import db, ledger, proposals
from orrery.organizer import sessions

NOW2 = FROZEN_NOW.replace(hour=10)


def test_schema_1_db_migrates_to_current(orrery_home):
    c = db.connect(db.db_path(orrery_home))
    c.executescript(db.MIGRATIONS[0])  # leaves user_version = 1
    with ledger.change(c, actor="orrery", kind="setup", now=NOW):
        c.execute("insert into kinds (name) values ('learn')")
    c.close()
    c = db.open_existing(orrery_home)
    assert c.execute("pragma user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert c.execute("select name from kinds").fetchall() == [("learn",)]
    c.close()


def test_exactly_one_active(week_db):
    assert sessions.ensure(week_db, NOW).id == sessions.ensure(week_db, NOW).id


def test_second_active_session_refused(week_db):
    """The unique index, not only ensure's re-read, keeps one active session."""
    sessions.ensure(week_db, NOW)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        week_db.execute(
            "insert into organizer_sessions (started_at, claude_id) values ('t', 'u-2')"
        )
    assert week_db.execute("select count(*) from organizer_sessions").fetchone()[0] == 1


def test_end_and_restart_discards_waiting_and_keeps_transcript(week_db):
    s = sessions.ensure(week_db, NOW)
    sessions.say(week_db, s.id, "you", "hi", NOW)
    propose_move(week_db)
    s2 = sessions.end_and_restart(week_db, NOW2)
    assert s2.id != s.id and proposals.waiting(week_db) is None
    assert [ln.text for ln in sessions.transcript(week_db, s.id)] == ["new chat started", "hi"]


def test_transcript_append_only(week_db):
    s = sessions.ensure(week_db, NOW)
    tid = sessions.say(week_db, s.id, "you", "hello", NOW)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        week_db.execute("update transcript set text = 'changed' where id = ?", (tid,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        week_db.execute("delete from transcript where id = ?", (tid,))


def test_remember_gated_and_logged(week_db):
    with pytest.raises(ValueError, match="reword"):
        sessions.remember(week_db, "the user is behind", NOW)
    nid = sessions.remember(week_db, "protect Monday math", NOW)
    assert nid > 0
    hist = ledger.history(week_db, tbl="memory")
    assert len(hist) == 1
    assert hist[0].actor == "organizer" and hist[0].kind == "memory"


def test_remember_empty_note(week_db):
    with pytest.raises(ValueError, match="^empty note$"):
        sessions.remember(week_db, "", NOW)
    assert sessions.memory(week_db) == [] and not week_db.in_transaction


def test_forget_logged(week_db):
    nid = sessions.remember(week_db, "protect Monday math", NOW)
    sessions.forget(week_db, nid, NOW2)
    hist = ledger.history(week_db, tbl="memory")
    assert len(hist) == 2
    assert hist[1].op == "delete" and hist[1].actor == "organizer"


def test_forget_unknown_note(week_db):
    with pytest.raises(ValueError, match="no note #999"):
        sessions.forget(week_db, 999, NOW)


def test_reseed_keeps_session_id(week_db):
    s = sessions.ensure(week_db, NOW)
    old_claude_id = s.claude_id
    s2 = sessions.reseed(week_db, s.id)
    assert s2.id == s.id
    assert s2.claude_id != old_claude_id
    assert s2.claude_live is False


def test_propose_stores_session_id(week_db):
    s = sessions.ensure(week_db, NOW)
    p = propose_move(week_db, session_id=s.id)
    row = week_db.execute("select session_id from proposals where id = ?", (p.id,)).fetchone()
    assert row[0] == s.id


def test_active_returns_none_when_no_session(week_db):
    assert sessions.active(week_db) is None


def test_ensure_is_idempotent(week_db):
    s1 = sessions.ensure(week_db, NOW)
    s2 = sessions.ensure(week_db, NOW2)
    assert s1.id == s2.id


def test_ensure_creates_transcript_row(week_db):
    s = sessions.ensure(week_db, NOW)
    rows = week_db.execute(
        "select speaker, text from transcript where session_id = ?", (s.id,)
    ).fetchall()
    assert rows == [("orrery", "new chat started")]


def test_say_returns_id(week_db):
    s = sessions.ensure(week_db, NOW)
    tid = sessions.say(week_db, s.id, "you", "hello", NOW)
    assert tid > 0
    row = week_db.execute("select speaker, text from transcript where id = ?", (tid,)).fetchone()
    assert row == ("you", "hello")


def test_transcript_limit(week_db):
    s = sessions.ensure(week_db, NOW)
    sessions.say(week_db, s.id, "you", "a", NOW)
    sessions.say(week_db, s.id, "organizer", "b", NOW)
    sessions.say(week_db, s.id, "you", "c", NOW)
    lines = sessions.transcript(week_db, s.id, limit=2)
    assert [ln.text for ln in lines] == ["b", "c"]


def test_has_talk_checks_you_and_organizer(week_db):
    s = sessions.ensure(week_db, NOW)
    assert not sessions.has_talk(week_db, s.id)
    sessions.say(week_db, s.id, "orrery", "system message", NOW)
    assert not sessions.has_talk(week_db, s.id)
    sessions.say(week_db, s.id, "you", "hello", NOW)
    assert sessions.has_talk(week_db, s.id)


def test_memory_returns_all_notes(week_db):
    n1 = sessions.remember(week_db, "note one", NOW)
    n2 = sessions.remember(week_db, "note two", NOW2)
    notes = sessions.memory(week_db)
    assert len(notes) == 2
    assert notes[0].id == n1 and notes[0].text == "note one"
    assert notes[1].id == n2 and notes[1].text == "note two"


def test_mark_live_sets_flag(week_db):
    s = sessions.ensure(week_db, NOW)
    assert s.claude_live is False
    sessions.mark_live(week_db, s.id)
    s2 = sessions.active(week_db)
    assert s2.claude_live is True


def test_end_and_restart_creates_new_session(week_db):
    s = sessions.ensure(week_db, NOW)
    sessions.say(week_db, s.id, "you", "hi", NOW)
    s2 = sessions.end_and_restart(week_db, NOW2)
    assert s2.id != s.id
    # Old session is inactive
    old = week_db.execute("select active from organizer_sessions where id = ?", (s.id,)).fetchone()
    assert old[0] == 0
    # New session has transcript
    lines = sessions.transcript(week_db, s2.id)
    assert len(lines) == 1 and lines[0].text == "new chat started"


def test_ensure_raises_when_in_transaction(week_db):
    week_db.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(Exception, match="transaction"):
            sessions.ensure(week_db, NOW)
    finally:
        week_db.execute("ROLLBACK")


def test_end_and_restart_raises_when_in_transaction(week_db):
    sessions.ensure(week_db, NOW)
    week_db.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(Exception, match="transaction"):
            sessions.end_and_restart(week_db, NOW)
    finally:
        week_db.execute("ROLLBACK")


def test_all_lines_pages_across_sessions(week_db):
    s1 = sessions.ensure(week_db, NOW)
    sessions.say(week_db, s1.id, "you", "a", NOW)
    sessions.say(week_db, s1.id, "organizer", "b", NOW)
    s2 = sessions.end_and_restart(week_db, NOW2)
    sessions.say(week_db, s2.id, "you", "c", NOW2)
    page = sessions.all_lines(week_db, limit=3)
    assert [(ln.session_id, ln.text) for ln in page] == [
        (s2.id, "c"),
        (s2.id, "new chat started"),
        (s1.id, "b"),
    ]
    assert page[0].id > page[1].id > page[2].id and page[2].at == NOW.isoformat()
    rest = sessions.all_lines(week_db, before_id=page[-1].id, limit=3)
    assert [(ln.session_id, ln.text) for ln in rest] == [(s1.id, "a"), (s1.id, "new chat started")]
    assert sessions.all_lines(week_db, before_id=rest[-1].id) == []
