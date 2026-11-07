import datetime as dt

from orrery import db, ledger

NOW = dt.datetime(2026, 11, 12, 9, 0)
LATER = dt.datetime(2026, 11, 13, 9, 0)


def _two_changes(c):
    with ledger.change(c, actor="orrery", kind="setup", now=NOW) as first:
        c.execute("insert into kinds (name) values ('learn')")
        c.execute("insert into settings values ('session_cap', '4')")
    with ledger.change(c, actor="you", kind="proposal", now=LATER, reason="tweak") as second:
        c.execute("update settings set value = '5'")
    return first, second


def test_history_filters_by_table(orrery_home):
    c = db.open_db(orrery_home)
    first, second = _two_changes(c)
    assert [(h.change_id, h.op) for h in ledger.history(c, tbl="settings")] == [
        (first, "insert"),
        (second, "update"),
    ]
    assert [h.tbl for h in ledger.history(c)] == ["kinds", "settings", "settings"]


def test_history_filters_since(orrery_home):
    c = db.open_db(orrery_home)
    _, second = _two_changes(c)
    [h] = ledger.history(c, since="2026-11-13")
    assert (h.change_id, h.actor, h.kind, h.reason, h.proposal_id) == (
        second,
        "you",
        "proposal",
        "tweak",
        None,
    )
    assert (h.old, h.new) == (
        {"key": "session_cap", "value": "4"},
        {"key": "session_cap", "value": "5"},
    )
    assert ledger.history(c, since="2026-11-14") == []


def test_last_change(orrery_home):
    c = db.open_db(orrery_home)
    assert ledger.last_change(c, "setup") is None
    first, _ = _two_changes(c)
    with ledger.change(c, actor="orrery", kind="setup", now=LATER) as third:
        pass
    assert ledger.last_change(c, "setup") == third != first
    assert ledger.last_change(c, "undo") is None


def test_change_stores_effective_and_links(orrery_home):
    c = db.open_db(orrery_home)
    with ledger.change(c, actor="you", kind="proposal", now=NOW) as target:
        pass
    with ledger.change(
        c, actor="you", kind="undo", now=NOW, undoes=target, effective=dt.date(2026, 11, 16)
    ) as cid:
        pass
    row = c.execute("select undoes, effective, at from changes where id = ?", (cid,)).fetchone()
    assert row == (target, "2026-11-16", "2026-11-12T09:00:00")
    assert c.execute("select count(*) from open_change").fetchone()[0] == 0
