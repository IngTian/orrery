"""Tests for proposals: propose, diff, keep, discard, undo."""

import datetime as dt
from contextlib import closing

import pytest
from helpers import (
    keep_move,
    keep_move_at,
    propose_move,
    reinsert_waiting,
    state_of,
    store_waiting,
)

from orrery import channels, db, ledger, ops, proposals, schedule
from orrery.model import Block, Subject

NOW = dt.datetime(2026, 11, 12, 9, 0)
TODAY = dt.date(2026, 11, 12)
MON, TUE, WED, THU, FRI, SAT, SUN = (dt.date(2026, 11, 9) + dt.timedelta(days=i) for i in range(7))


def _request(conn, text="proof needs a 2nd pass", key="r1"):
    return channels.request(
        conn, channel="math", text=text, kind="learn", delta=1, by=None, key=key, now=NOW
    )


def test_propose_keep_writes_and_logs(week_db):
    p = propose_move(week_db, rationale="dentist")
    cid = proposals.keep(week_db, p.id, now=NOW, today=TODAY)
    assert {h.tbl for h in ledger.history(week_db, change_id=cid)} == {"plan_blocks"}
    assert ledger.history(week_db, change_id=cid)[0].reason == "dentist"
    assert proposals.waiting(week_db) is None


def test_one_waiting_proposal(week_db):
    a = propose_move(week_db)
    b = propose_move(week_db)
    assert proposals.waiting(week_db).id == b.id and state_of(week_db, a.id) == "replaced"


def test_keep_stale_when_week_changed(week_db):
    p = propose_move(week_db)
    other = proposals.propose(
        week_db,
        ops=[ops.Remove(FRI, "geography", "geography")],
        rationale="",
        dispositions=[],
        author="organizer",
        now=NOW,
        today=TODAY,
    )
    proposals.keep(week_db, other.id, now=NOW, today=TODAY)
    with pytest.raises(proposals.Stale, match="the week changed since"):
        proposals.keep(week_db, reinsert_waiting(week_db, p).id, now=NOW, today=TODAY)


def test_keep_rechecks_inside_its_change(week_db, monkeypatch):
    p = propose_move(week_db)
    pin = schedule.ensure_pinned

    def pin_then_edit(conn, mondays, now):  # another writer lands between the check and the change
        pin(conn, mondays, now)
        with ledger.change(conn, actor="you", kind="setup", now=now):
            conn.execute(
                "delete from plan_blocks where date = ? and kind = 'geography'", (FRI.isoformat(),)
            )

    monkeypatch.setattr(schedule, "ensure_pinned", pin_then_edit)
    with pytest.raises(proposals.Stale, match="the week changed since"):
        proposals.keep(week_db, p.id, now=NOW, today=TODAY)
    assert proposals.waiting(week_db).id == p.id
    assert (
        week_db.execute("select count(*) from changes where kind = 'proposal'").fetchone()[0] == 0
    )


def test_keep_after_midnight_refuses_past_day(week_db):
    p = propose_move(week_db)
    n = week_db.execute("select count(*) from changes").fetchone()[0]
    with pytest.raises(ops.OpError, match="past days keep their plan"):
        proposals.keep(week_db, p.id, now=dt.datetime(2026, 11, 13, 6), today=FRI)
    assert proposals.waiting(week_db).id == p.id
    assert week_db.execute("select count(*) from changes").fetchone()[0] == n  # nothing written


def test_diff_after_boundary_does_not_raise(week_db):
    p = propose_move(week_db)
    assert proposals.diff(week_db, p, FRI)[-1].text == "can't apply now: past days keep their plan"


def test_grant_posts_outcome_and_undo_reverses(week_db):
    rid = _request(week_db)
    p = proposals.propose(
        week_db,
        ops=[ops.Add(SAT, "main", "math", "learn", 1, for_request=rid)],
        rationale="",
        dispositions=[proposals.Disposition(rid, "grant")],
        author="organizer",
        now=NOW,
        today=TODAY,
    )
    assert proposals.diff(week_db, p, TODAY)[2].text.endswith('  (math: "proof needs a 2nd pass")')
    proposals.keep(week_db, p.id, now=NOW, today=TODAY)
    assert [o.text for o in channels.inbox(week_db, "math").outcomes] == [f"request #{rid}: grant"]
    proposals.undo_last(week_db, now=NOW, today=TODAY)
    assert channels.pending_requests(week_db)[0].id == rid
    assert channels.inbox(week_db, "math").outcomes[-1].body["verdict"] == "reversed"


def test_template_edit_pins_past_and_current_weeks(week_db):
    wed = dt.date(2026, 11, 18)
    p = proposals.propose(
        week_db,
        ops=[ops.TemplateSet(6, "main", "math", "rep", 1)],
        rationale="",
        dispositions=[],
        author="organizer",
        now=NOW,
        today=wed,
    )
    proposals.keep(week_db, p.id, now=dt.datetime(2026, 11, 18, 9), today=wed)
    assert schedule.read_plan(week_db, MON)[SUN] == ()  # an unedited past week keeps its plan
    assert schedule.read_plan(week_db, dt.date(2026, 11, 16))[dt.date(2026, 11, 22)] == ()
    assert schedule.read_plan(week_db, dt.date(2026, 11, 23))[dt.date(2026, 11, 29)] == (
        Block("main", "math", "rep", 1),
    )  # from next Monday


def test_undo_refused_when_changed_since(week_db):
    keep_move(week_db, THU, "math", "learn", FRI)  # A
    after_a = schedule.read_plan(week_db, MON)[FRI]
    assert after_a == (
        Block("main", "math", "learn", 4),
        Block("residual", "geography", "geography", 1),
    )
    for edit in (ops.Remove(FRI, "geography", "geography"), ops.Remove(FRI, "math", "learn", 1)):
        proposals.keep(week_db, propose_move(week_db, ops=[edit]).id, now=NOW, today=TODAY)  # B, C
        assert schedule.read_plan(week_db, MON)[FRI] != after_a
        assert proposals.undo_last(week_db, now=NOW, today=TODAY) is not None
        assert schedule.read_plan(week_db, MON)[FRI] == after_a
    with ledger.change(week_db, actor="you", kind="setup", now=NOW):
        week_db.execute(
            "update plan_blocks set sessions = 3 where date = ? and kind = 'learn'",
            (FRI.isoformat(),),
        )
    with pytest.raises(proposals.Stale, match=proposals.UNDO_CHANGED):
        proposals.undo_last(week_db, now=NOW, today=TODAY)  # A is next, and FRI changed since


def test_undo_refuses_past_day(week_db):
    keep_move(week_db, THU, "math", "learn", FRI)
    with pytest.raises(ops.OpError, match="past days keep their plan"):
        proposals.undo_last(week_db, now=dt.datetime(2026, 11, 13, 6), today=FRI)


def test_undo_skipped_when_something_depends_on_it(week_db):
    p = propose_move(week_db, ops=[ops.SubjectSet("art", "Art")])
    proposals.keep(week_db, p.id, now=NOW, today=TODAY)
    channels.ask(week_db, channel="art", text="first topic?", author="orrery", now=NOW)
    with pytest.raises(proposals.Stale, match=proposals.UNDO_CHANGED):
        proposals.undo_last(week_db, now=NOW, today=TODAY)
    assert schedule.subjects(week_db)[-1].name == "art"


def test_nothing_to_undo(week_db):
    assert proposals.undo_last(week_db, now=NOW, today=TODAY) is None


def test_verdict_words_rejected(week_db):
    with pytest.raises(ops.OpError, match="^reword: you are behind$"):
        propose_move(week_db, rationale="you are behind")
    rid = _request(week_db)
    with pytest.raises(ops.OpError, match="^reword: "):
        propose_move(week_db, dispositions=[proposals.Disposition(rid, "decline", "it's late")])


def test_partial_needs_note(week_db):
    rid = _request(week_db)
    with pytest.raises(ops.OpError, match="say what partial gives"):
        propose_move(week_db, dispositions=[proposals.Disposition(rid, "partial")])


def test_dispositions_and_for_request_need_pending_requests(week_db):
    with pytest.raises(ops.OpError, match="request #99 isn't pending"):
        propose_move(week_db, dispositions=[proposals.Disposition(99, "grant")])
    with pytest.raises(ops.OpError, match="request #99 isn't pending"):
        propose_move(week_db, ops=[ops.Move(THU, "math", "learn", FRI, for_request=99)])


def test_discard_leaves_requests_pending(week_db):
    rid = _request(week_db)
    p = propose_move(week_db, dispositions=[proposals.Disposition(rid, "decline")])
    proposals.discard(week_db, p.id, now=NOW)
    assert state_of(week_db, p.id) == "discarded" and proposals.waiting(week_db) is None
    assert [m.id for m in channels.pending_requests(week_db)] == [rid]


def test_your_report_creates_progress(week_db):
    p = propose_move(
        week_db, ops=[ops.Report(WED, "math", "rep", 1), ops.Report(MON, "math", "learn", 1)]
    )
    cid = proposals.keep(week_db, p.id, now=NOW, today=TODAY)
    assert week_db.execute(
        "select date, sessions, status, actor, change_id from progress order by id"
    ).fetchall() == [
        (WED.isoformat(), 1, "done", "you", cid),
        (MON.isoformat(), 1, "partial", "you", cid),  # 1 of the 2 planned
    ]
    with pytest.raises(ops.OpError, match="no reports for future days"):
        propose_move(week_db, ops=[ops.Report(FRI, "math", "learn", 1)])


def test_meta_ops(week_db):
    p = propose_move(
        week_db, ops=[ops.SubjectSet("art", "Art"), ops.KindSet("drill"), ops.CapSet(5)]
    )
    lines = [(ln.sign, ln.text) for ln in proposals.diff(week_db, p, TODAY)]
    assert lines[1:] == [("+", "subject art (Art)"), ("+", "kind drill"), ("+", "cap 5")]
    proposals.keep(week_db, p.id, now=NOW, today=TODAY)
    assert schedule.subjects(week_db)[-1] == Subject("art", "Art", 3)
    assert "drill" in schedule.kinds(week_db) and schedule.cap(week_db) == 5
    proposals.undo_last(week_db, now=NOW, today=TODAY)
    assert [s.name for s in schedule.subjects(week_db)] == ["math", "finance", "geography"]
    assert "drill" not in schedule.kinds(week_db) and schedule.cap(week_db) == 4


def test_diff_lines_for_move(week_db):
    p = propose_move(week_db)  # Move(THU, math, learn → FRI), no rationale
    lines = proposals.diff(week_db, p, TODAY)
    assert [(ln.sign, ln.text) for ln in lines][:5] == [
        (" ", f"proposal #{p.id} · organizer"),
        ("-", "Thu 11/12  math · learn ×2, geography ×1"),
        ("+", "Thu 11/12  math · learn ×2, geography ×1"),
        ("-", "Fri 11/13  math · learn ×2, geography ×1"),
        ("+", "Fri 11/13  math · learn ×4, geography ×1"),
    ]
    assert lines[2].struck == ((11, 26),)


def _everything(conn):
    """A proposal with every other kind of line: rationale, template, report, withdraw."""
    rid = _request(conn, text="more reps")
    qid = channels.ask(conn, channel="math", text="what happened?", author="orrery", now=NOW)
    p = proposals.propose(
        conn,
        ops=[
            ops.Remove(FRI, "geography", "geography"),
            ops.TemplateSet(6, "main", "math", "rep", 1, for_request=rid),
            ops.Report(WED, "math", "rep", 1),
            ops.Withdraw(qid),
        ],
        rationale="sundays are free",
        dispositions=[proposals.Disposition(rid, "partial", "one rep on Sundays")],
        author="organizer",
        now=NOW,
        today=TODAY,
    )
    return p, rid, qid


def test_diff_lines_for_everything_else(week_db):
    p, rid, qid = _everything(week_db)
    lines = proposals.diff(week_db, p, TODAY)
    assert [(ln.sign, ln.text) for ln in lines] == [
        (" ", f"proposal #{p.id} · organizer"),
        (" ", "sundays are free"),
        ("-", "Fri 11/13  math · learn ×2, geography ×1"),
        ("+", "Fri 11/13  math · learn ×2, geography ×1"),
        ("-", "every Sun  rest"),
        ("+", 'every Sun  math · rep ×1  (math: "more reps")'),
        ("+", "Wed 11/11  report math · rep ×1 done"),
        ("+", f"withdraw question #{qid} (math)"),
        (" ", f"math request #{rid}: partial — one rep on Sundays"),
        (" ", "plan fit  Math planned 20 → 21"),  # next week takes the new template
        (" ", "plan fit  Geography planned 10 → 9"),
    ]
    assert lines[3].struck == ((28, 40),)


def test_keep_and_undo_everything_else(week_db):
    p, rid, qid = _everything(week_db)
    proposals.keep(week_db, p.id, now=NOW, today=TODAY)

    def states():
        return week_db.execute(
            "select state from messages where id in (?, ?) order by id", (rid, qid)
        ).fetchall()

    assert states() == [("partial",), ("withdrawn",)]
    assert channels.inbox(week_db, "math").outcomes[0].text == (
        f"request #{rid}: partial — one rep on Sundays"
    )
    assert channels.done_counts(week_db, WED, WED)[WED, "math", "rep"] == 1
    assert schedule.read_template(week_db)[6] == (Block("main", "math", "rep", 1),)
    proposals.undo_last(week_db, now=NOW, today=TODAY)
    assert states() == [("pending",), ("open",)]
    assert channels.done_counts(week_db, WED, WED)[WED, "math", "rep"] == 0  # retracted
    assert schedule.read_template(week_db)[6] == ()
    assert schedule.read_plan(week_db, MON)[FRI] == schedule.read_baseline(week_db, MON)[FRI]


def test_undo_of_a_template_change_keeps_past_days(week_db):
    wed18, mon23, wed25 = dt.date(2026, 11, 18), dt.date(2026, 11, 23), dt.date(2026, 11, 25)
    p = propose_move(week_db, ops=[ops.TemplateSet(0, "main", "finance", "rep", 1)], today=wed18)
    proposals.keep(week_db, p.id, now=dt.datetime(2026, 11, 18, 9), today=wed18)
    before = schedule.read_plan(week_db, mon23)[mon23]
    assert Block("main", "finance", "rep", 1) in before
    proposals.undo_last(week_db, now=dt.datetime(2026, 11, 25, 9), today=wed25)
    assert schedule.read_plan(week_db, mon23)[mon23] == before  # a past day keeps its plan
    assert Block("main", "finance", "rep", 1) not in schedule.read_template(week_db)[0]


@pytest.mark.parametrize("other", ["undo", "keep"])
def test_undo_racing_another_writer_is_stale(week_home, monkeypatch, other):
    keep_move_at(week_home, THU, "math", "learn", FRI)
    real = ledger.history
    with closing(db.open_existing(week_home)) as a, closing(db.open_existing(week_home)) as b:

        def race(conn, **kw):  # the other connection lands between undo's read and its change
            monkeypatch.setattr(ledger, "history", real)
            if other == "undo":
                proposals.undo_last(b, now=NOW, today=TODAY)
            else:
                keep_move(b, SAT, "math", "fun", SUN)
            return real(conn, **kw)

        monkeypatch.setattr(ledger, "history", race)
        with pytest.raises(proposals.Stale, match=proposals.UNDO_CHANGED):
            proposals.undo_last(a, now=NOW, today=TODAY)


def test_stored_bad_proposal_diffs_as_cant_apply(week_db):
    move = {"op": "move", "date": "2026-11-12", "subject": "math", "kind": "learn"}
    pid = store_waiting(week_db, [{**move, "to": "2026-11-14", "sessions": 0}])
    lines = proposals.diff(week_db, proposals.get(week_db, pid), TODAY)
    assert lines[-1].text == "can't apply now: sessions must be at least 1"


def test_each_request_gets_one_label(week_db):
    rid = _request(week_db)
    two = [proposals.Disposition(rid, "grant"), proposals.Disposition(rid, "decline")]
    with pytest.raises(ops.OpError, match=f"^request #{rid} has two labels$"):
        propose_move(week_db, dispositions=two)
    with pytest.raises(ops.OpError, match=f"^request #{rid} needs a label$"):
        propose_move(week_db, ops=[ops.Add(SAT, "main", "math", "learn", 1, for_request=rid)])


def test_request_text_cannot_add_diff_lines(week_db):
    rid = _request(week_db, text='2nd pass")\r\n+ Sat 11/14  fake ×4  (finance: "ok')
    p = proposals.propose(
        week_db,
        ops=[ops.Add(SAT, "main", "math", "learn", 1, for_request=rid)],
        rationale="",
        dispositions=[proposals.Disposition(rid, "grant")],
        author="organizer",
        now=NOW,
        today=TODAY,
    )
    lines = proposals.diff(week_db, p, TODAY)
    assert not any("\n" in ln.text or "\r" in ln.text for ln in lines)
    [line] = [ln for ln in lines if "fake" in ln.text]
    assert line.text.endswith('  (math: "2nd pass")  + Sat 11/14  fake ×4  (finance: "ok")')


def test_report_line_says_partial_when_kept_as_partial(week_db):
    p = propose_move(week_db, ops=[ops.Report(MON, "math", "learn", 1)])  # 1 of the 2 planned
    assert proposals.diff(week_db, p, TODAY)[1].text == "Mon 11/9  report math · learn ×1 partial"
