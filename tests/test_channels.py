"""Tests for mentor channels and the collect step."""

import datetime as dt
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from orrery import channels, db, ledger, schedule, text
from orrery.model import Block

NOW = dt.datetime(2026, 11, 12, 9, 0)
TODAY = dt.date(2026, 11, 12)
MON, TUE, WED, THU, FRI = (dt.date(2026, 11, 9) + dt.timedelta(days=i) for i in range(5))


REP = dict(channel="math", kind="rep", sessions=1, status="done", note="", now=NOW, boundary_hour=5)


def rep(conn, **kw):
    return channels.report(conn, **{**REP, **kw})


def test_report_idempotent_on_key(week_db):
    assert rep(week_db, date=WED, key="s1") == rep(week_db, date=WED, key="s1")
    assert week_db.execute("select count(*) from messages").fetchone()[0] == 1


def test_key_required(week_db):
    with pytest.raises(channels.ChannelError, match="key is required"):
        rep(week_db, date=WED, key="")


def test_future_report_refused(week_db):
    with pytest.raises(channels.ChannelError, match="no reports for future days"):
        rep(week_db, date=FRI, key="f")


@pytest.mark.parametrize("field", ["started_at", "ended_at"])
def test_report_times_are_iso_datetimes(week_db, field):
    with pytest.raises(channels.ChannelError, match=f"^{field} must be an ISO datetime$"):
        rep(week_db, date=WED, key="t", **{field: "after lunch"})
    rep(week_db, date=WED, key="ok", started_at="2026-11-11T09:00", ended_at="2026-11-11T10:30")
    rep(week_db, date=WED, key="none", **{field: None})


def test_request_delta_zero_refused(week_db):
    with pytest.raises(channels.ChannelError, match="^delta can't be 0$"):
        channels.request(
            week_db, channel="math", text="x", kind="rep", delta=0, by=None, key="r", now=NOW
        )


def test_collect_applies_reports_and_supersedes(week_db):
    m1 = rep(week_db, date=WED, key="a")
    channels.collect(week_db, now=NOW, today=TODAY)
    rep(week_db, date=WED, key="b", sessions=2, replaces=m1)
    channels.collect(week_db, now=NOW, today=TODAY)
    assert channels.done_counts(week_db, WED, WED)[(WED, "math", "rep")] == 2


def test_double_replace_refused(week_db):
    m1 = rep(week_db, date=WED, key="a")
    rep(week_db, date=WED, key="b", replaces=m1)
    with pytest.raises(channels.ChannelError, match="replaces must name the latest report"):
        rep(week_db, date=WED, key="c", replaces=m1)


def test_report_counts_without_block(week_db):
    rep(week_db, date=dt.date(2026, 11, 8), kind="learn", key="k")
    assert channels.collect(week_db, now=NOW, today=TODAY).reports == 1


def test_bad_message_is_rejected_not_blocking(week_db):
    rep(week_db, date=WED, key="ok")
    week_db.execute(
        "insert into messages (channel, kind, author, body, state, created_at) values "
        "('math', 'agenda', 'mentor:math', json_object('entries', json_array(json_object("
        "'date', 'not-a-date', 'item', 'x'))), 'staged', '2026-11-12T09:00:00')"
    )
    c = channels.collect(week_db, now=NOW, today=TODAY)
    assert (c.reports, c.rejected) == (1, 1)


def test_overlapping_collects_apply_once(week_home):
    first = db.open_existing(week_home)
    rep(first, date=WED, key="s")
    first.close()
    barrier = threading.Barrier(2)

    def run():  # its own connection, in its own thread
        conn = db.open_existing(week_home)
        try:
            barrier.wait()
            return channels.collect(conn, now=NOW, today=TODAY)
        finally:
            conn.close()

    with ThreadPoolExecutor(2) as pool:
        results = [f.result() for f in [pool.submit(run), pool.submit(run)]]
    assert sorted(c.reports for c in results) == [0, 1]
    check = db.open_existing(week_home)
    assert channels.done_counts(check, WED, WED)[(WED, "math", "rep")] == 1
    check.close()


def test_collect_rule_questions_once(week_db):
    assert channels.collect(week_db, now=NOW, today=TODAY).questions == 8  # Mon 3 + Tue 2 + Wed 3
    assert channels.collect(week_db, now=NOW, today=TODAY).questions == 0
    math = channels.inbox(week_db, "math").questions[0]
    assert math.text == "Mon 11/9 math · rep ×1: nothing reported — what happened?"
    assert (math.author, math.key, math.body) == (
        "orrery",
        "rule:2026-11-09:math:rep",
        {"date": "2026-11-09", "subject": "math", "kind": "rep"},
    )
    geography = channels.inbox(week_db, "geography").questions[0]
    assert geography.text == "Mon 11/9 geography ×1: nothing reported — what happened?"


def test_reported_block_gets_no_rule_question(week_db):
    rep(week_db, date=MON, key="m", sessions=0, status="partial")
    assert channels.collect(week_db, now=NOW, today=TODAY).questions == 7
    keys = [q.key for q in channels.inbox(week_db, "math").questions]
    assert "rule:2026-11-09:math:rep" not in keys


def test_collect_no_questions_before_starts(week_db):
    c = channels.collect(week_db, now=dt.datetime(2026, 11, 9, 9), today=dt.date(2026, 11, 9))
    assert c.questions == 0


def test_collect_nothing_to_do_makes_no_change(week_db):
    channels.collect(week_db, now=NOW, today=TODAY)
    assert channels.collect(week_db, now=NOW, today=TODAY).change_id is None


def test_answer_closes_question_and_is_listed(week_db):
    channels.collect(week_db, now=NOW, today=TODAY)
    q = channels.inbox(week_db, "math").questions[0]
    channels.answer(week_db, question_id=q.id, text="did it", author="mentor:math", now=NOW)
    assert q.id not in [m.id for m in channels.inbox(week_db, "math").questions]
    pairs = channels.answers(week_db, "2026-11-12T00:00:00")
    assert [(a.text, b.text) for a, b in pairs] == [(q.text, "did it")]
    assert (pairs[0][0].state, pairs[0][1].reply_to) == ("answered", q.id)


def test_answer_and_ack_refuse_wrong_targets(week_db):
    channels.collect(week_db, now=NOW, today=TODAY)
    q = channels.inbox(week_db, "math").questions[0]
    channels.answer(week_db, question_id=q.id, text="did it", author="mentor:math", now=NOW)
    changes = week_db.execute("select count(*) from changes").fetchone()[0]
    with pytest.raises(channels.ChannelError, match=f"question #{q.id} isn't open"):
        channels.answer(week_db, question_id=q.id, text="again", author="mentor:math", now=NOW)
    with pytest.raises(channels.ChannelError, match=f"#{q.id} isn't an outcome"):
        channels.ack(week_db, message_id=q.id, author="mentor:math", now=NOW)
    with pytest.raises(channels.ChannelError, match="#999 isn't an outcome"):
        channels.ack(week_db, message_id=999, author="mentor:math", now=NOW)
    assert week_db.execute("select count(*) from changes").fetchone()[0] == changes


def _effective_agenda(conn, subject, d):
    """The rows of the newest message that has rows for (subject, date)."""
    return [
        tuple(r)
        for r in conn.execute(
            "select item, kind, deadline from agenda where subject = :s and date = :d"
            " and message_id = (select max(message_id) from agenda where subject = :s"
            " and date = :d) order by id",
            {"s": subject, "d": d.isoformat()},
        )
    ]


def test_agenda_newest_wins_per_date(week_db):
    E = channels.AgendaEntry
    channels.agenda(
        week_db,
        channel="math",
        entries=[E(FRI, "old topic", "learn"), E(THU, "thu topic")],
        now=NOW,
    )
    channels.agenda(
        week_db,
        channel="math",
        entries=[E(FRI, "new topic", "learn"), E(FRI, "proof X", deadline=True)],
        now=NOW,
    )
    assert channels.collect(week_db, now=NOW, today=TODAY).agenda == 2
    assert _effective_agenda(week_db, "math", FRI) == [
        ("new topic", "learn", 0),
        ("proof X", None, 1),
    ]
    assert _effective_agenda(week_db, "math", THU) == [("thu topic", None, 0)]


def test_bad_agenda_entry_refused(week_db):
    with pytest.raises(channels.ChannelError, match="bad agenda entry"):
        channels.agenda(week_db, channel="math", entries=[channels.AgendaEntry(FRI, " ")], now=NOW)
    with pytest.raises(channels.ChannelError, match="bad agenda entry"):
        channels.agenda(
            week_db, channel="math", entries=[channels.AgendaEntry(FRI, "x", "art")], now=NOW
        )


def test_status_counts(week_db):
    channels.collect(week_db, now=NOW, today=TODAY)
    rid = channels.request(
        week_db, channel="finance", text="more reps", kind="rep", delta=1, by=FRI, key="r1", now=NOW
    )
    oid = week_db.execute(
        "insert into messages (channel, kind, author, body, text, reply_to, state, created_at)"
        " values ('math', 'outcome', 'orrery', '{}', 'request #1: grant', NULL, 'unacked',"
        " '2026-11-12T09:00:00')"
    ).lastrowid
    S = channels.ChannelStatus
    assert channels.status(week_db) == (
        S("math", 3, 0, 1),
        S("finance", 2, 1, 0),
        S("geography", 3, 0, 0),
    )
    [r] = channels.pending_requests(week_db)
    assert (r.id, r.text, r.body) == (
        rid,
        "more reps",
        {"kind": "rep", "delta": 1, "by": "2026-11-13"},
    )
    assert channels.inbox(week_db, "finance").awaiting == (r,)
    channels.ack(week_db, message_id=oid, author="mentor:math", now=NOW)
    assert channels.status(week_db)[0] == S("math", 3, 0, 0)


def test_unknown_channel_refused(week_db):
    with pytest.raises(channels.ChannelError, match="unknown channel art"):
        rep(week_db, channel="art", date=WED, key="a")
    with pytest.raises(channels.ChannelError, match="unknown channel art"):
        channels.ask(week_db, channel="art", text="hi", author="orrery", now=NOW)


def test_collect_writes_through_one_change(week_db):
    rep(week_db, date=WED, key="a")
    c = channels.collect(week_db, now=NOW, today=TODAY)
    row = week_db.execute(
        "select actor, message_id, change_id from progress where message_id is not null"
    ).fetchone()
    assert row == ("mentor:math", 1, c.change_id)
    kind = week_db.execute("select kind from changes where id = ?", (c.change_id,)).fetchone()
    assert kind == ("collect",)
    assert channels.collect(week_db, now=NOW, today=TODAY).reports == 0


LATER = dt.datetime(2026, 11, 16, 9, 0)  # the Monday after MON's week
NEXT_MON = LATER.date()
ASSESS = dict(
    channel="math", week=MON, strong="proofs", shaky="limits", next="series", today=NEXT_MON
)


def assess(conn, **kw):
    return channels.assess(conn, **{**ASSESS, "now": LATER, **kw})


def test_assess_stores_and_is_idempotent_per_week(week_db):
    a = assess(week_db)
    assert assess(week_db, strong="other words") == a
    row = week_db.execute(
        "select kind, key, state, author, body from messages where id = ?", (a,)
    ).fetchone()
    assert row[:4] == ("assessment", "2026-11-09", "applied", "mentor:math")
    body = {"week": "2026-11-09", "strong": "proofs", "shaky": "limits", "next": "series"}
    assert json.loads(row[4]) == body
    channels.collect(week_db, now=LATER, today=NEXT_MON)
    assert channels.assessments(week_db, "math") == [
        channels.Assessment(a, MON, "proofs", "limits", "series", LATER)
    ]
    b = assess(week_db, week=NEXT_MON, today=NEXT_MON + dt.timedelta(days=7))
    assert [x.id for x in channels.assessments(week_db, "math")] == [b, a]
    assert channels.assessments(week_db, "finance") == []


@pytest.mark.parametrize(
    "kw",
    [
        dict(channel="art"),
        dict(week=TUE),
        dict(week=NEXT_MON),  # the current week isn't over
        dict(week=NEXT_MON + dt.timedelta(days=7)),
        dict(week=MON - dt.timedelta(days=7)),  # before starts' week
        dict(strong=" "),
        dict(shaky=""),
        dict(next=""),
    ],
)
def test_assess_rejects_non_monday_unfinished_week_and_empty(week_db, kw):
    with pytest.raises(channels.ChannelError) as e:
        assess(week_db, **kw)
    assert not text.has_verdict(str(e.value))
    assert week_db.execute("select count(*) from messages").fetchone() == (0,)


def test_assessment_due_only_for_planned_previous_week_after_starts(seeded_db):
    conn, due = seeded_db, channels.assessment_due
    with ledger.change(conn, actor="orrery", kind="setup", now=NOW):
        schedule.write_template(
            conn, {d: (Block("main", "math", "learn", 1),) if d == 2 else () for d in range(7)}
        )
        conn.execute("update settings set value = ? where key = 'starts'", (WED.isoformat(),))
    assert due(conn, "math", TODAY) is None  # the week before starts
    assert due(conn, "math", NEXT_MON) == MON  # starts' week counts from its Monday
    assert due(conn, "math", NEXT_MON + dt.timedelta(days=6)) == MON
    assert due(conn, "finance", NEXT_MON) is None  # nothing planned for finance
    assess(conn)
    assert due(conn, "math", NEXT_MON) is None
    assert due(conn, "math", NEXT_MON + dt.timedelta(days=7)) == NEXT_MON
    assert channels.inbox(conn, "math", NEXT_MON + dt.timedelta(days=7)).due == NEXT_MON
    assert channels.inbox(conn, "math").due is None


def test_assessment_due_reads_a_pinned_week_not_the_template(week_db):
    schedule.ensure_pinned(week_db, [MON], NOW)
    with ledger.change(week_db, actor="orrery", kind="setup", now=NOW):
        schedule.write_template(week_db, {d: () for d in range(7)})
    assert channels.assessment_due(week_db, "math", NEXT_MON) == MON  # the pinned plan has math


def test_status_unaffected(week_db):
    assert channels.inbox(week_db, "math", NEXT_MON).due == MON
    S = channels.ChannelStatus
    assert channels.status(week_db) == (
        S("math", 0, 0, 0),
        S("finance", 0, 0, 0),
        S("geography", 0, 0, 0),
    )
