"""Tests for schedule operations on the database."""

import datetime as dt

import pytest
from conftest import KINDS, TEMPLATE

from orrery import ledger, schedule
from orrery.model import Block

MON = dt.date(2026, 11, 9)
NOW = dt.datetime(2026, 11, 12, 9, 0)


def test_today_respects_boundary():
    assert schedule.today(dt.datetime(2026, 11, 12, 4, 59), 5) == dt.date(2026, 11, 11)
    assert schedule.today(dt.datetime(2026, 11, 12, 5, 0), 5) == dt.date(2026, 11, 12)


def test_unpinned_week_reads_template_and_writes_nothing(week_db):
    n = week_db.execute("select count(*) from changes").fetchone()[0]
    plan = schedule.read_plan(week_db, MON)
    assert plan[dt.date(2026, 11, 11)] == TEMPLATE[2] and plan[dt.date(2026, 11, 15)] == ()
    assert week_db.execute("select count(*) from changes").fetchone()[0] == n
    assert not schedule.is_pinned(week_db, MON)


def test_pin_copies_template_to_baseline_and_plan(week_db):
    assert schedule.ensure_pinned(week_db, [MON], NOW)
    assert schedule.read_baseline(week_db, MON) == schedule.read_plan(week_db, MON)
    assert schedule.ensure_pinned(week_db, [MON], NOW) is None


def test_template_change_after_pin_leaves_pinned_week(week_db):
    schedule.ensure_pinned(week_db, [MON], NOW)
    with ledger.change(week_db, actor="you", kind="setup", now=NOW):
        schedule.write_template(week_db, {6: (Block("main", "math", "rep", 1),)})
    assert schedule.read_plan(week_db, MON)[dt.date(2026, 11, 15)] == ()
    assert schedule.read_plan(week_db, dt.date(2026, 11, 16))[dt.date(2026, 11, 22)] == (
        Block("main", "math", "rep", 1),
    )


def test_write_days_on_unpinned_week_raises(week_db):
    with pytest.raises(ValueError):
        with ledger.change(week_db, actor="you", kind="proposal", now=NOW):
            schedule.write_days(week_db, {dt.date(2026, 11, 12): ()})


def test_block_order_is_part_then_ord(week_db):
    assert [b.part for b in schedule.read_plan(week_db, MON)[MON]] == [
        "warmup",
        "main",
        "residual",
    ]


def test_due_mondays(week_db):
    assert schedule.due_mondays(week_db, dt.date(2026, 11, 18)) == [
        MON,
        dt.date(2026, 11, 16),
    ]


def test_seed_sets_cap_starts_subjects(seeded_db):
    assert schedule.cap(seeded_db) == 4 and schedule.starts(seeded_db) == MON
    assert [s.name for s in schedule.subjects(seeded_db)] == ["math", "finance", "geography"]
    assert schedule.kinds(seeded_db) == frozenset(KINDS)
