import datetime as dt
import json
import sqlite3
import threading
import time

import pytest
from conftest import KINDS, SUBJECTS, TEMPLATE
from helpers import TODAY, keep_move, store_waiting

from orrery import channels, db, ledger, proposals, schedule, store

NOW = dt.datetime(2026, 11, 12, 9, 0)
FRI = TODAY + dt.timedelta(days=1)


def test_connect_pragmas(tmp_path):
    c = db.connect(tmp_path / "x.db")
    assert c.execute("pragma foreign_keys").fetchone()[0] == 1
    assert c.execute("pragma journal_mode").fetchone()[0] == "wal"
    assert c.execute("pragma busy_timeout").fetchone()[0] == 5000
    assert c.execute("pragma recursive_triggers").fetchone()[0] == 1
    assert c.autocommit is True


def test_open_db_migrates_to_current(orrery_home):
    assert db.open_db(orrery_home).execute("pragma user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_open_existing_missing_is_unavailable(orrery_home):
    assert db.open_existing(orrery_home) == store.Unavailable("missing")


def test_open_existing_returns_a_migrated_connection(orrery_home):
    db.open_db(orrery_home).close()
    c = db.open_existing(orrery_home)
    assert isinstance(c, sqlite3.Connection)
    assert c.execute("pragma user_version").fetchone()[0] == db.SCHEMA_VERSION


@pytest.mark.parametrize("mode", ["wal", "delete"])
def test_newer_schema_is_unavailable_and_untouched(orrery_home, mode):
    p = db.db_path(orrery_home)
    c = sqlite3.connect(p)
    c.execute(f"pragma journal_mode={mode}")
    c.execute(f"pragma user_version = {db.SCHEMA_VERSION + 1}")
    c.close()
    before = p.read_bytes()
    assert db.open_existing(orrery_home) == store.Unavailable("made by a newer orrery")
    assert p.read_bytes() == before
    with pytest.raises(RuntimeError, match="made by a newer orrery"):
        db.open_db(orrery_home)


def test_corrupt_file_is_unavailable(orrery_home):
    db.db_path(orrery_home).write_bytes(b"this is not sqlite" * 100)
    assert db.open_existing(orrery_home) == store.Unavailable("file is not a database")


def test_open_existing_takes_no_write_lock_when_current(orrery_home):
    """A reader's open must not wait on a writer when nothing is pending."""
    a = db.open_db(orrery_home)
    a.execute("BEGIN IMMEDIATE")
    t0 = time.monotonic()
    b = db.open_existing(orrery_home)
    assert isinstance(b, sqlite3.Connection)
    assert time.monotonic() - t0 < 1
    a.execute("ROLLBACK")


def test_failed_migration_leaves_file_unchanged(orrery_home, monkeypatch):
    bad = (
        "CREATE TABLE t2 (x INTEGER) STRICT; INSERT INTO nope VALUES (1);\nPRAGMA user_version = 2;"
    )
    monkeypatch.setattr(db, "MIGRATIONS", [db.MIGRATIONS[0], bad])
    monkeypatch.setattr(db, "SCHEMA_VERSION", 2)
    with pytest.raises(sqlite3.OperationalError, match="no such table: nope"):
        db.open_db(orrery_home)
    c = sqlite3.connect(db.db_path(orrery_home))
    assert c.execute("pragma user_version").fetchone()[0] == 0
    assert c.execute("select count(*) from sqlite_master").fetchone()[0] == 0


def test_write_outside_change_is_refused(orrery_home):
    with pytest.raises(sqlite3.IntegrityError, match="write outside a change set"):
        db.open_db(orrery_home).execute("insert into kinds (name) values ('learn')")


def test_change_logs_history(orrery_home):
    c = db.open_db(orrery_home)
    with ledger.change(c, actor="you", kind="setup", now=NOW, reason="seed") as cid:
        c.execute("insert into kinds (name) values ('learn')")
    [h] = ledger.history(c, change_id=cid)
    assert (h.tbl, h.op, h.new, h.old, h.actor, h.at, h.reason) == (
        "kinds",
        "insert",
        {"name": "learn", "active": 1},
        None,
        "you",
        "2026-11-12T09:00:00",
        "seed",
    )


def test_update_and_delete_log_old_and_new(orrery_home):
    c = db.open_db(orrery_home)
    with ledger.change(c, actor="you", kind="setup", now=NOW):
        c.execute("insert into kinds (name) values ('learn')")
    with ledger.change(c, actor="you", kind="setup", now=NOW) as cid:
        c.execute("update kinds set active = 0")
        c.execute("delete from kinds")
    assert [(h.op, h.old, h.new) for h in ledger.history(c, change_id=cid)] == [
        ("update", {"name": "learn", "active": 1}, {"name": "learn", "active": 0}),
        ("delete", {"name": "learn", "active": 0}, None),
    ]


def test_history_is_append_only(orrery_home):
    c = db.open_db(orrery_home)
    with ledger.change(c, actor="you", kind="setup", now=NOW):
        c.execute("insert into kinds (name) values ('learn')")
    for sql in ("update history set tbl = 'x'", "delete from history", "delete from changes"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            c.execute(sql)


def _seed(c):
    with ledger.change(c, actor="orrery", kind="setup", now=NOW):
        c.execute("insert into settings values ('session_cap', '4')")
        c.execute("insert into subjects (name, label, ord) values ('math', 'Math', 0)")
        c.execute("insert into kinds (name) values ('learn')")
        c.execute("insert into weeks values ('2026-11-09', ?)", (NOW.isoformat(),))
        c.execute(
            "insert into baseline_blocks values ('2026-11-12', '2026-11-09', 'main', 0,"
            " 'math', 'learn', 2)"
        )
        c.execute(
            "insert into progress (date, subject, kind, sessions, status, actor, change_id,"
            " created_at) values ('2026-11-12', 'math', 'learn', 1, 'done', 'you', 1, '')"
        )
        c.execute(
            "insert into messages (channel, kind, author, body, state, created_at)"
            " values ('math', 'agenda', 'mentor:math', '{}', 'applied', '')"
        )
        c.execute(
            "insert into agenda (subject, date, item, message_id, change_id, created_at)"
            " values ('math', '2026-11-12', 'ch. 3', 1, 1, '')"
        )


def test_replace_cannot_bypass_triggers(orrery_home):
    """REPLACE deletes the conflicting row; delete triggers must still see it."""
    c = db.open_db(orrery_home)
    _seed(c)
    with ledger.change(c, actor="you", kind="setup", now=NOW) as cid:
        for sql in (
            "insert or replace into history (id, change_id, tbl, op)"
            " values (1, 1, 'forged', 'insert')",
            "insert or replace into messages (id, channel, kind, author, body, state, created_at)"
            " values (1, 'math', 'report', 'x', '[]', 'staged', '')",
            "insert or replace into weeks values ('2026-11-09', 'x')",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                c.execute(sql)
        c.execute("insert or replace into settings values ('session_cap', '5')")
    assert [(h.op, h.tbl) for h in ledger.history(c, change_id=cid)] == [
        ("delete", "settings"),
        ("insert", "settings"),
    ]


@pytest.mark.parametrize("table", ["weeks", "baseline_blocks", "progress", "agenda"])
def test_log_once_tables_refuse_update_and_delete(orrery_home, table):
    c = db.open_db(orrery_home)
    _seed(c)
    with ledger.change(c, actor="you", kind="setup", now=NOW):
        for sql in (f"update {table} set rowid = rowid", f"delete from {table}"):
            with pytest.raises(sqlite3.IntegrityError, match=f"{table} is append-only"):
                c.execute(sql)


def test_messages_insert_free_body_frozen_state_logged(orrery_home):
    c = db.open_db(orrery_home)
    _seed(c)
    c.execute(
        "insert into messages (channel, kind, author, body, state, created_at)"
        " values ('math', 'report', 'mentor:math', '{}', 'staged', '')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="messages is append-only"):
        with ledger.change(c, actor="orrery", kind="collect", now=NOW):
            c.execute("update messages set body = '[]'")
    with pytest.raises(sqlite3.IntegrityError, match="write outside a change set"):
        c.execute("update messages set state = 'applied'")
    with ledger.change(c, actor="orrery", kind="collect", now=NOW) as cid:
        c.execute("update messages set state = 'applied' where id = 2")
    [h] = ledger.history(c, change_id=cid)
    assert (h.tbl, h.old["state"], h.new["state"]) == ("messages", "staged", "applied")
    with pytest.raises(sqlite3.IntegrityError, match="messages is append-only"):
        c.execute("delete from messages")


def test_proposals_update_free_delete_forbidden(orrery_home):
    c = db.open_db(orrery_home)
    c.execute(
        "insert into proposals (created_at, author, ops, expected, state)"
        " values ('', 'organizer', '[]', '{}', 'waiting')"
    )
    c.execute("update proposals set state = 'discarded'")
    with pytest.raises(sqlite3.IntegrityError, match="proposals is append-only"):
        c.execute("delete from proposals")


def test_cap_trigger(orrery_home):
    c = db.open_db(orrery_home)
    _seed(c)
    with ledger.change(c, actor="you", kind="setup", now=NOW):
        with pytest.raises(sqlite3.IntegrityError, match="over the cap"):
            c.execute("insert into template_blocks values (3, 'main', 0, 'math', 'learn', 5)")
        c.execute("insert into template_blocks values (3, 'main', 0, 'math', 'learn', 4)")
        with pytest.raises(sqlite3.IntegrityError, match="over the cap"):
            c.execute("update template_blocks set sessions = 5")
        with pytest.raises(sqlite3.IntegrityError, match="over the cap"):
            c.execute(
                "insert into plan_blocks values ('2026-11-12', '2026-11-09', 'main', 0,"
                " 'math', 'learn', 5)"
            )


def test_no_cap_setting_means_unlimited(orrery_home):
    c = db.open_db(orrery_home)
    with ledger.change(c, actor="you", kind="setup", now=NOW):
        c.execute("insert into subjects (name, label, ord) values ('math', 'Math', 0)")
        c.execute("insert into kinds (name) values ('learn')")
        c.execute("insert into template_blocks values (3, 'main', 0, 'math', 'learn', 99)")


def test_name_check(orrery_home):
    c = db.open_db(orrery_home)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        with ledger.change(c, actor="you", kind="setup", now=NOW):
            c.execute("insert into subjects (name, label, ord) values ('mATH notes!', 'x', 0)")


@pytest.mark.parametrize("monday", ["not-a-date", "2026-11-10"])
def test_date_check_refuses_non_dates(orrery_home, monday):
    c = db.open_db(orrery_home)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        with ledger.change(c, actor="you", kind="setup", now=NOW):
            c.execute("insert into weeks values (?, '2026-11-12T09:00:00')", (monday,))


def test_foreign_keys_enforced(orrery_home):
    c = db.open_db(orrery_home)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        with ledger.change(c, actor="you", kind="setup", now=NOW):
            c.execute(
                "insert into plan_blocks values ('2026-11-12', '2026-11-09', 'main', 0,"
                " 'math', 'learn', 1)"
            )


def test_failed_change_rolls_back(orrery_home):
    c = db.open_db(orrery_home)
    with pytest.raises(RuntimeError):
        with ledger.change(c, actor="you", kind="setup", now=NOW):
            c.execute("insert into kinds (name) values ('learn')")
            raise RuntimeError
    assert c.execute("select count(*) from kinds").fetchone()[0] == 0
    assert c.execute("select count(*) from changes").fetchone()[0] == 0


def test_nested_change_raises(orrery_home):
    c = db.open_db(orrery_home)
    with ledger.change(c, actor="you", kind="setup", now=NOW):
        with pytest.raises(ledger.ChangeError):
            with ledger.change(c, actor="you", kind="setup", now=NOW):
                pass


def test_two_connections_write_concurrently(orrery_home):
    a = db.open_db(orrery_home)

    def post():  # its own connection, in its own thread
        b = db.open_existing(orrery_home)
        with ledger.change(b, actor="mentor:math", kind="message", now=NOW):
            b.execute("insert into settings values ('k2', 'v')")
        b.close()

    with ledger.change(a, actor="you", kind="setup", now=NOW):
        a.execute("insert into settings values ('k1', 'v')")
        t = threading.Thread(target=post)
        t.start()
        time.sleep(0.2)
    t.join(timeout=10)
    assert {r[0] for r in a.execute("select key from settings")} == {"k1", "k2"}


def test_migration_scripts_set_their_version():
    assert len(db.MIGRATIONS) == db.SCHEMA_VERSION
    for i, script in enumerate(db.MIGRATIONS):
        assert script.rstrip().endswith(f"PRAGMA user_version = {i + 1};")
        assert "COMMIT" not in script.upper()  # trigger bodies use BEGIN ... END


def test_audited_cols_are_every_column(orrery_home):
    c = db.open_db(orrery_home)
    for table, cols, *_ in db._AUDIT:
        if cols:
            assert cols.split() == [r[1] for r in c.execute(f"pragma table_info({table})")], table


_ASSESSMENT = (
    "insert into messages (channel, kind, author, body, state, created_at)"
    " values ('math', ?, 'mentor:math', '{}', 'applied', '')"
)
_ROW = {"date": "2026-11-12", "subject": "math", "kind": "learn"}


def _rows(c, skip_proposal):
    """Every row of every table, but overrides and one proposal."""
    names = [t for (t,) in c.execute("select name from sqlite_schema where type = 'table'")]
    rows = {t: c.execute(f"select * from {t} order by rowid").fetchall() for t in names}
    rows.pop("overrides", None)
    rows["proposals"] = [r for r in rows["proposals"] if r[0] != skip_proposal]
    return rows


_MARK = {"op": "mark", **_ROW, "mark": "full"}


# one_waiting allows a single waiting proposal, so each case stores its own. Ops no orrery
# wrote (not a list, or holding what isn't an op) stay waiting and never stop the migration.
@pytest.mark.parametrize(
    ("ops", "state"),
    [
        ([_MARK], "discarded"),
        ([{"op": "move", **_ROW, "to": "2026-11-13"}], "waiting"),
        (_MARK, "waiting"),
        ([1, "mark", ["mark"], None, {"op": ["mark"]}], "waiting"),
    ],
)
def test_schema_2_db_migrates_to_3(orrery_home, ops, state):
    c = db.connect(db.db_path(orrery_home))
    c.executescript(db.MIGRATIONS[0])
    c.executescript(db.MIGRATIONS[1])  # leaves user_version = 2
    schedule.seed(c, subjects=SUBJECTS, kinds=KINDS, cap=4, starts=dt.date(2026, 11, 9), now=NOW)
    with ledger.change(c, actor="orrery", kind="setup", now=NOW):
        schedule.write_template(c, TEMPLATE)
    keep_move(c, TODAY, "math", "learn", FRI)
    channels.report(
        c,
        channel="math",
        date=TODAY,
        kind="learn",
        sessions=1,
        status="done",
        note="",
        key="k",
        now=NOW,
        boundary_hour=5,
    )  # staged, not collected
    kept = c.execute(
        "insert into proposals (created_at, author, ops, expected, state, decided_at)"
        " values ('2026-11-11T09:00:00', 'organizer', ?, '{}', 'kept', '2026-11-11T09:05:00')",
        (json.dumps([_MARK]),),
    ).lastrowid
    pid = store_waiting(c, ops)
    before = _rows(c, pid)
    c.close()

    c = db.open_existing(orrery_home)
    assert c.execute("pragma user_version").fetchone()[0] == 3
    assert c.execute("pragma integrity_check").fetchall() == [("ok",)]
    assert c.execute("select name from sqlite_schema where sql like '%overrides%'").fetchall() == []
    got = c.execute("select state, decided_at is not null from proposals where id = ?", (pid,))
    assert got.fetchone() == (state, state == "discarded")
    got = c.execute("select state, decided_at from proposals where id = ?", (kept,))
    assert got.fetchone() == ("kept", "2026-11-11T09:05:00")  # decided before: left as it was
    assert _rows(c, pid) == before
    c.execute(_ASSESSMENT, ("assessment",))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        c.execute(_ASSESSMENT, ("bogus",))
    monday = schedule.week_start(TODAY)
    assert schedule.read_plan(c, monday) != schedule.read_baseline(c, monday)
    assert proposals.undo_last(c, now=NOW, today=TODAY) is not None
    assert schedule.read_plan(c, monday) == schedule.read_baseline(c, monday)


def test_fresh_db_is_schema_3(orrery_home):
    c = db.open_db(orrery_home)
    assert db.SCHEMA_VERSION == 3 == c.execute("pragma user_version").fetchone()[0]
    assert c.execute("select 1 from sqlite_schema where name = 'overrides'").fetchall() == []
    _seed(c)
    c.execute(_ASSESSMENT, ("assessment",))


def test_undo_skips_rows_of_a_dropped_table(week_db):
    cid = keep_move(week_db, TODAY, "math", "learn", FRI)
    week_db.execute(
        "insert into history (change_id, tbl, op, new) values (?, 'overrides', 'insert', ?)",
        (cid, json.dumps({**_ROW, "mark": "full"})),
    )
    assert proposals.undo_last(week_db, now=NOW, today=TODAY) is not None
    monday = schedule.week_start(TODAY)
    assert schedule.read_plan(week_db, monday) == schedule.read_baseline(week_db, monday)
