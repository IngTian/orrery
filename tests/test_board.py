"""Tests for the board (derived week view)."""

import datetime as dt

from conftest import TEMPLATE
from helpers import NOW, add_agenda, keep_move, propose_move, report_and_collect

from orrery import board, channels, ledger, ops, proposals, schedule, stats, text
from orrery.model import Block

MON = dt.date(2026, 11, 9)
TUE = dt.date(2026, 11, 10)
WED = dt.date(2026, 11, 11)
THU = dt.date(2026, 11, 12)
FRI = dt.date(2026, 11, 13)
SAT = dt.date(2026, 11, 14)
TODAY = dt.date(2026, 11, 12)


def test_struck_only_cell_after_full_move(week_db):
    keep_move(week_db, THU, "math", "learn", FRI)
    b = board.build(week_db, MON, TODAY)
    [c] = [c for c in b.days[3].cells if c.subject == "math"]
    assert (c.sessions, c.struck, c.moved_to) == (0, 2, FRI)
    [f] = [c for c in b.days[4].cells if c.subject == "math"]
    assert (f.sessions, f.added) == (4, 2)


def test_chain_thu_fri_sat(week_db):
    keep_move(week_db, THU, "math", "learn", FRI)
    keep_move(week_db, FRI, "math", "learn", SAT, 2)
    d = board.build(week_db, MON, TODAY).days
    thu, fri, sat = ([c for c in d[i].cells if c.kind == "learn"] for i in (3, 4, 5))
    assert (thu[0].struck, fri[0].struck, fri[0].added, sat[0].added) == (2, 0, 0, 2)


def test_move_back_clears_strike(week_db):
    keep_move(week_db, THU, "math", "learn", FRI)
    keep_move(week_db, FRI, "math", "learn", THU, 2)
    assert all(
        c.struck == 0 and c.moved_to is None for c in board.build(week_db, MON, TODAY).days[3].cells
    )


def test_marks_from_progress(week_db):
    report_and_collect(week_db, MON, "math", "learn", 1)
    assert [
        c.mark for c in board.build(week_db, MON, TODAY).days[0].cells if c.kind == "learn"
    ] == ["half"]
    report_and_collect(week_db, MON, "math", "learn", 1)
    assert [
        c.mark for c in board.build(week_db, MON, TODAY).days[0].cells if c.kind == "learn"
    ] == ["full"]


def test_unplanned_and_surplus_are_extras(week_db):
    report_and_collect(week_db, MON, "finance", "rep", 1)
    report_and_collect(week_db, MON, "math", "learn", 3)
    assert board.build(week_db, MON, TODAY).days[0].extras == (
        board.Extra("math", "learn", 1),
        board.Extra("finance", "rep", 1),
    )


def test_topic_and_deadline_from_agenda(week_db):
    add_agenda(
        week_db,
        "math",
        [
            channels.AgendaEntry(FRI, "Zorb rules §2", "learn"),
            channels.AgendaEntry(FRI, "proof X", deadline=True),
        ],
    )
    fri = board.build(week_db, MON, TODAY).days[4]
    assert [c.topic for c in fri.cells if c.subject == "math"] == ["Zorb rules §2"]
    assert fri.deadlines == ("math: proof X",)


def test_done_on_a_moved_away_day_is_an_extra(week_db):
    keep_move(week_db, THU, "math", "learn", FRI)
    report_and_collect(week_db, THU, "math", "learn", 1)
    assert board.build(week_db, MON, TODAY).days[3].extras == (board.Extra("math", "learn", 1),)


def test_struck_only_cells_keep_baseline_order(week_db):
    wed, thu = dt.date(2026, 11, 18), dt.date(2026, 11, 19)  # next week's: both still editable
    moves = [ops.Move(wed, "math", "rep", thu), ops.Move(wed, "finance", "rep", thu)]
    proposals.keep(week_db, propose_move(week_db, ops=moves).id, now=NOW, today=TODAY)
    cells = board.build(week_db, dt.date(2026, 11, 16), TODAY).days[2].cells
    assert [(c.subject, c.sessions, c.struck) for c in cells] == [
        ("math", 0, 1),
        ("finance", 0, 1),
        ("geography", 1, 0),
    ]


def test_zero_done_makes_no_extra_and_undo_leaves_no_note(week_db):
    report_and_collect(week_db, TUE, "math", "rep", 0)  # Tue has no math rep
    xs = [ops.Report(WED, "finance", "learn", 1), ops.Report(WED, "math", "rep", 1)]
    proposals.keep(week_db, propose_move(week_db, ops=xs).id, now=NOW, today=TODAY)
    proposals.undo_last(week_db, now=NOW, today=TODAY)
    days = board.build(week_db, MON, TODAY).days
    assert days[1].extras == () and days[2].extras == ()


def test_build_never_writes(week_db):
    # Capture counts before
    changes_count = week_db.execute("select count(*) from changes").fetchone()[0]
    messages_count = week_db.execute("select count(*) from messages").fetchone()[0]
    progress_count = week_db.execute("select count(*) from progress").fetchone()[0]

    # Build the board
    board.build(week_db, MON, TODAY)

    # Verify no writes happened
    assert week_db.execute("select count(*) from changes").fetchone()[0] == changes_count
    assert week_db.execute("select count(*) from messages").fetchone()[0] == messages_count
    assert week_db.execute("select count(*) from progress").fetchone()[0] == progress_count


def _cell(b: board.WeekBoard, day: int, subject: str, kind: str) -> board.Cell:
    return next(c for c in b.days[day].cells if (c.subject, c.kind) == (subject, kind))


def test_cell_partial_done_first_capping(week_db):
    # Monday plans math learn 2 (main) and 1 (residual): 2 done fill main first, the residual
    # cell takes a partial, and 1 is left over
    with ledger.change(week_db, actor="orrery", kind="setup", now=NOW):
        schedule.write_template(
            week_db, {**TEMPLATE, 0: (*TEMPLATE[0], Block("residual", "math", "learn", 1))}
        )
    report_and_collect(week_db, MON, "math", "learn", 2, status="partial")
    report_and_collect(week_db, MON, "math", "learn", 2)
    report_and_collect(week_db, MON, "math", "rep", 1, status="partial")  # plans 1
    b = board.build(week_db, MON, TODAY)
    learn = [
        (c.part, c.sessions, c.done, c.partial)
        for c in b.days[0].cells
        if (c.subject, c.kind) == ("math", "learn")
    ]
    assert learn == [("main", 2, 2, 0), ("residual", 1, 1, 1)]
    rep = _cell(b, 0, "math", "rep")
    assert (rep.sessions, rep.done, rep.partial) == (1, 1, 1)
    assert b.days[0].extras == (board.Extra("math", "learn", 1),)
    assert all(c.partial == 0 for c in b.days[1].cells)


def test_partial_zero_session_report_adds_no_pip_but_note_shows(week_db):
    report_and_collect(week_db, MON, "math", "learn", 0, status="partial", note="ran out of time")
    c = _cell(board.build(week_db, MON, TODAY), 0, "math", "learn")
    assert (c.done, c.partial) == (0, 0)
    row = stats.SessionRow("math", "learn", 0, "partial", None, None, "ran out of time")
    assert stats.day_sessions(week_db, MON) == [row]


def test_mcp_text_unchanged(week_db):
    """Golden strings from the today and week text before partial counts existed."""
    report_and_collect(week_db, MON, "math", "learn", 1)
    report_and_collect(week_db, MON, "math", "rep", 1, status="partial")
    report_and_collect(week_db, TUE, "finance", "learn", 3, status="partial", note="ch 2")
    report_and_collect(week_db, THU, "geography", "geography", 1, status="partial")
    keep_move(week_db, THU, "math", "learn", FRI)
    add_agenda(week_db, "math", [channels.AgendaEntry(FRI, "Zorb rules §2", "learn")])
    b = board.build(week_db, MON, THU)
    assert (
        text.today_text(b, "math") == "Thu 11/12 · today\nmain  math · learn ×2 moved → Fri 11/13"
    )
    assert text.today_text(b, None) == (
        "Thu 11/12 · today\nmain  math · learn ×2 moved → Fri 11/13\nresidual  geography ×1 ✓"
    )
    assert text.week_text(b, None) == "\n".join(
        [
            "  Mon 11/9   math · rep ×1 ✓, math · learn ×2 ½, geography ×1",
            "  Tue 11/10  finance · learn ×2 ✓, geography ×1",
            "  Wed 11/11  math · rep ×1, finance · rep ×1, geography ×1",
            "> Thu 11/12  math · learn ×2 moved, geography ×1 ✓",
            "  Fri 11/13  math · learn ×4, geography ×1",
            "  Sat 11/14  math · fun ×1, math · rep ×1",
            "  Sun 11/15  rest",
        ]
    )
