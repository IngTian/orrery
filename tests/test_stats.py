"""Tests for the stats built on the board: rates, the Report's days, range and best week,
rhythm, a day's sessions and changes."""

import datetime as dt
from contextlib import closing

from helpers import NOW, TODAY, propose_move, report_and_collect

from orrery import db, ledger, ops, proposals, stats, text
from orrery.organizer import sessions

MON, TUE, WED, THU = (dt.date(2026, 11, d) for d in (9, 10, 11, 12))
W2, W3 = dt.date(2026, 11, 16), dt.date(2026, 11, 23)


def _keep_report(conn, op: ops.Report) -> None:
    proposals.keep(conn, propose_move(conn, ops=[op]).id, now=NOW, today=TODAY)


def _timed(conn, d: dt.date, subject: str, kind: str, n: int, start: str, end: str) -> None:
    report_and_collect(
        conn, d, subject, kind, n, started_at=f"{d}T{start}:00", ended_at=f"{d}T{end}:00"
    )


def test_week_cache_follows_writes(week_db, week_home):
    a = stats.week(week_db, MON, TODAY)
    assert stats.week(week_db, MON, TODAY) is a
    report_and_collect(week_db, MON, "math", "learn", 1)  # this connection's own write
    b = stats.week(week_db, MON, TODAY)
    assert b is not a and b.days[0].cells[1].done == 1
    with closing(db.open_existing(week_home)) as other:  # another connection's commit
        report_and_collect(other, MON, "math", "learn", 1)
    assert stats.week(week_db, MON, TODAY).days[0].cells[1].done == 2


def test_week_rates_to_date_only_and_no_extras(week_db):
    report_and_collect(week_db, MON, "math", "learn", 3)  # plans 2: one surplus
    report_and_collect(week_db, TUE, "math", "rep", 1)  # Tuesday plans no math
    report_and_collect(week_db, MON, "finance", "rep", 1)  # unplanned
    assert stats.week_rates(week_db, MON, 1, TODAY) == {
        "math": [(MON, 2, 6)],  # Mon 3, Wed 1, Thu 2 planned to date
        "finance": [(MON, 0, 3)],
        "geography": [(MON, 0, 4)],
    }
    assert stats.week_rates(week_db, MON, 1, TUE)["math"] == [(MON, 2, 3)]


def test_week_rates_skip_before_starts(week_db):
    later = dt.date(2026, 11, 25)
    report_and_collect(week_db, W2, "math", "learn", 2, now=dt.datetime(2026, 11, 25, 9))
    rates = stats.week_rates(week_db, W3, 5, later)
    assert rates["math"] == [(MON, 0, 10), (W2, 2, 10), (W3, 0, 4)]
    assert stats.week_rates(week_db, MON - dt.timedelta(days=7), 2, TODAY) == {}
    with ledger.change(week_db, actor="orrery", kind="setup", now=NOW):  # starts on a Wednesday
        week_db.execute("update settings set value = ? where key = 'starts'", (WED.isoformat(),))
    assert stats.week_rates(week_db, MON, 1, TODAY)["math"] == [(MON, 0, 3)]


def test_rhythm_times_need_five(week_db):
    _timed(week_db, MON, "math", "rep", 1, "08:00", "08:30")
    _timed(week_db, MON, "math", "learn", 2, "09:00", "10:40")
    report_and_collect(week_db, MON, "geography", "geography", 1)  # untimed
    _timed(week_db, TUE, "finance", "learn", 2, "08:30", "10:00")
    _timed(week_db, TUE, "geography", "geography", 1, "20:00", "20:45")
    assert stats.rhythm(week_db, MON, WED, 5) == stats.Rhythm(4, None, None, None)
    _timed(week_db, WED, "math", "rep", 1, "07:30", "08:10")
    r = stats.rhythm(week_db, MON, WED, 5)
    assert r == stats.Rhythm(5, dt.time(8, 0), dt.time(9, 0), 45)


def test_rhythm_start_times_wrap_at_the_day_boundary(week_db):
    for at in ("09T23:30", "10T00:15", "10T00:45", "10T01:00", "10T01:30"):
        s = dt.datetime.fromisoformat(f"2026-11-{at}:00")
        e = s + dt.timedelta(minutes=30)
        report_and_collect(
            week_db, MON, "math", "learn", 1, started_at=s.isoformat(), ended_at=e.isoformat()
        )
    r = stats.rhythm(week_db, MON, MON, 5)
    assert (r.n, r.start_from, r.start_to, r.median_minutes) == (
        5,
        dt.time(0, 15),
        dt.time(1, 0),
        30,
    )


def _week_of_reports(conn) -> None:
    """Mon every block (math learn one past its 2); Tue finance and geography, and math rep
    off the plan; Wed math rep and geography (partial), not finance; Thu nothing."""
    report_and_collect(conn, MON, "math", "rep", 1)
    report_and_collect(conn, MON, "math", "learn", 3)  # one surplus
    report_and_collect(conn, MON, "geography", "geography", 1)
    report_and_collect(conn, TUE, "finance", "learn", 2)
    report_and_collect(conn, TUE, "geography", "geography", 1)
    report_and_collect(conn, TUE, "math", "rep", 1)  # Tuesday plans no math
    report_and_collect(conn, WED, "math", "rep", 1)
    report_and_collect(conn, WED, "geography", "geography", 1, status="partial")


def test_daily_caps_per_block_and_skips_unplanned_and_future(week_db):
    """Per day with planned blocks, from starts up to today: each planned subject's done
    (capped per block, partial counted) of planned. Extras never count, and a subject with no
    block planned that day isn't there."""
    _week_of_reports(week_db)
    got = stats.daily(week_db, MON - dt.timedelta(days=7), MON + dt.timedelta(days=6), TODAY)
    assert got == {
        MON: {"math": (3, 3), "geography": (1, 1)},
        TUE: {"finance": (2, 2), "geography": (1, 1)},
        WED: {"math": (1, 1), "finance": (0, 1), "geography": (1, 1)},
        THU: {"math": (0, 2), "geography": (0, 1)},
    }
    assert stats.daily(week_db, TUE, WED, TODAY).keys() == {TUE, WED}
    assert stats.daily(week_db, MON, THU, TUE).keys() == {MON, TUE}  # nothing after today
    with ledger.change(week_db, actor="orrery", kind="setup", now=NOW):  # starts on a Wednesday
        week_db.execute("update settings set value = ? where key = 'starts'", (WED.isoformat(),))
    assert stats.daily(week_db, MON, THU, TODAY).keys() == {WED, THU}


def test_range_summary_rows_runs_and_extras(week_db):
    """all, then each subject planned in range by ord: done of planned, the study days, the
    longest run of them with every planned session done, and the extras."""
    _week_of_reports(week_db)
    got = stats.range_summary(week_db, MON, MON + dt.timedelta(days=6), TODAY)
    assert list(got) == ["all", "math", "finance", "geography"]
    assert got["all"] == stats.Summary(done=9, planned=13, study_days=4, longest_run=2, extras=2)
    assert got["math"] == stats.Summary(4, 6, 3, 2, 2)  # Tue's off the plan: an extra, no day
    assert got["finance"] == stats.Summary(2, 3, 2, 1, 0)
    assert got["geography"] == stats.Summary(3, 4, 4, 3, 0)
    with ledger.change(week_db, actor="orrery", kind="setup", now=NOW):
        week_db.execute("update settings set value = '2026-11-16' where key = 'starts'")
    assert stats.range_summary(week_db, MON, THU, TODAY) == {"all": stats.Summary(0, 0, 0, 0, 0)}


def test_best_week_ties_to_latest(week_db):
    """The week with the highest rate to date among those with sessions planned; a tie goes to
    the later week; none planned, None."""
    later = dt.datetime(2026, 11, 25, 9)  # Wednesday of the third week
    for monday in (MON, W2):
        for kind, n in (("rep", 1), ("learn", 2)):
            report_and_collect(week_db, monday, "math", kind, n, now=later)
    end = W3 + dt.timedelta(days=6)
    assert stats.best_week(week_db, MON, end, later.date()) == (W2, 3, 18)
    report_and_collect(week_db, MON, "geography", "geography", 1, now=later)
    assert stats.best_week(week_db, MON, end, later.date()) == (MON, 4, 18)
    assert stats.best_week(week_db, W3, end, later.date()) == (W3, 0, 10)
    before = MON - dt.timedelta(days=7), MON - dt.timedelta(days=1)
    assert stats.best_week(week_db, *before, TODAY) is None


def test_day_sessions_exclude_undo_rows(week_db):
    _timed(week_db, WED, "math", "rep", 1, "08:00", "08:40")
    _keep_report(week_db, ops.Report(WED, "finance", "rep", 1, "on my own"))
    assert [r.subject for r in stats.day_sessions(week_db, WED)] == ["math", "finance"]
    proposals.undo_last(week_db, now=NOW, today=TODAY)
    assert stats.day_sessions(week_db, WED) == [
        stats.SessionRow(
            "math",
            "rep",
            1,
            "done",
            dt.datetime(2026, 11, 11, 8),
            dt.datetime(2026, 11, 11, 8, 40),
            "",
        )
    ]


def test_day_sessions_drop_a_stray_offset(week_db):
    report_and_collect(
        week_db,
        WED,
        "math",
        "rep",
        1,
        started_at=f"{WED}T08:00:00-05:00",
        ended_at=f"{WED}T08:40:00",
    )
    (row,) = stats.day_sessions(week_db, WED)
    assert (row.started_at, row.ended_at) == (
        dt.datetime(2026, 11, 11, 8),
        dt.datetime(2026, 11, 11, 8, 40),
    )
    assert row.ended_at - row.started_at == dt.timedelta(minutes=40)


def test_day_changes_bucketed_by_boundary(week_db):
    early = dt.datetime(2026, 11, 12, 2, 0)  # before the 5:00 boundary, so still Wednesday
    p = propose_move(week_db, rationale="a lighter Thursday", now=early)
    proposals.keep(week_db, p.id, now=early, today=TODAY)
    sessions.remember(week_db, "protect Monday math", NOW)
    undo_at = NOW.replace(hour=10)
    proposals.undo_last(week_db, now=undo_at, today=TODAY)
    assert stats.day_changes(week_db, WED, 5) == [
        (early, f"kept proposal #{p.id} · a lighter Thursday")
    ]
    assert stats.day_changes(week_db, THU, 5) == [
        (NOW, "noted: protect Monday math"),
        (undo_at, f"undid proposal #{p.id}"),
    ]
    assert len(stats.day_changes(week_db, THU, 0)) == 3
    facts = [t for d in (WED, THU) for _, t in stats.day_changes(week_db, d, 5)]
    assert not any(text.has_verdict(t) for t in facts)


def test_day_changes_forget_and_a_memory_change_with_no_row(week_db):
    nid = sessions.remember(week_db, "protect Monday math", NOW)
    sessions.forget(week_db, nid, NOW)
    with ledger.change(week_db, actor="organizer", kind="memory", now=NOW):
        pass  # wrote no memory row
    assert stats.day_changes(week_db, THU, 5) == [
        (NOW, "noted: protect Monday math"),
        (NOW, "note dropped: protect Monday math"),
    ]
