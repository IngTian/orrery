"""Tests for orrery.ops — pure schedule edit operations."""

from datetime import date, timedelta

import pytest
from conftest import KINDS as SEED_KINDS
from conftest import TEMPLATE

from orrery import ops
from orrery.model import Block

MON = date(2026, 11, 9)
THU, FRI, SAT, SUN = (date(2026, 11, d) for d in (12, 13, 14, 15))
TODAY = date(2026, 11, 12)
SUBJ = {"math", "finance", "geography"}
KINDS = set(SEED_KINDS)
CAP = 4


@pytest.fixture
def plan():
    """A 2-week plan starting Monday 2026-11-09."""
    return {MON + timedelta(i): TEMPLATE[i % 7] for i in range(14)}


def ap(plan, xs, done=None):
    """Shortcut for apply_plan with test defaults."""
    return ops.apply_plan(
        plan, xs, today=TODAY, done=done or {}, subjects=SUBJ, kinds=KINDS, cap=CAP
    )


def test_move_whole_block_merges(plan):
    out = ap(plan, [ops.Move(THU, "math", "learn", FRI)])
    assert [b for b in out[THU] if b.subject == "math"] == []
    assert Block("main", "math", "learn", 4) in out[FRI]


def test_partial_move_and_done_stays(plan):
    out = ap(plan, [ops.Move(THU, "math", "learn", SUN)], done={(THU, "math", "learn"): 1})
    assert Block("main", "math", "learn", 1) in out[THU] and out[SUN] == (
        Block("main", "math", "learn", 1),
    )


def test_move_back_restores(plan):
    out = ap(
        plan,
        [
            ops.Move(THU, "math", "learn", FRI),
            ops.Move(FRI, "math", "learn", THU, sessions=2),
        ],
    )
    assert out[THU] == plan[THU] and out[FRI] == plan[FRI]


def test_over_cap_refused(plan):
    with pytest.raises(ops.OpError, match="over the cap of 4"):
        ap(plan, [ops.Add(FRI, "main", "math", "learn", 3)])


def test_past_day_refused(plan):
    with pytest.raises(ops.OpError, match="past days keep their plan"):
        ap(plan, [ops.Move(date(2026, 11, 11), "math", "rep", FRI)])


def test_nothing_left_to_move(plan):
    with pytest.raises(ops.OpError, match="nothing left to move on Thu 11/12: 2 already done"):
        ap(plan, [ops.Move(THU, "math", "learn", FRI)], done={(THU, "math", "learn"): 2})


def test_new_block_keeps_source_part(plan):
    assert ap(plan, [ops.Move(THU, "geography", "geography", SUN)])[SUN] == (
        Block("residual", "geography", "geography", 1),
    )


def test_unknown_subject(plan):
    with pytest.raises(ops.OpError, match="unknown subject art"):
        ap(plan, [ops.Add(FRI, "main", "art", "learn", 1)])


def test_template_set_add_change_remove():
    t2 = ops.apply_template(
        dict(TEMPLATE),
        [
            ops.TemplateSet(6, "main", "math", "rep", 2),
            ops.TemplateSet(1, "main", "finance", "learn", 3),
            ops.TemplateSet(5, "residual", "math", "rep", 0),
        ],
        subjects=SUBJ,
        kinds=KINDS,
        cap=CAP,
    )
    assert t2[6] == (Block("main", "math", "rep", 2),)
    assert Block("main", "finance", "learn", 3) in t2[1]
    assert t2[5] == (Block("main", "math", "fun", 1),)


def test_check_meta_refuses_planned_subject_and_low_cap():
    with pytest.raises(ops.OpError, match="math is still planned"):
        ops.check_meta(
            [ops.SubjectSet("math", "Math", False)],
            subjects=SUBJ,
            kinds=KINDS,
            plan_and_template_max=2,
            used_subjects={"math"},
            used_kinds=set(),
        )
    with pytest.raises(ops.OpError, match="a block already has 2 sessions"):
        ops.check_meta(
            [ops.CapSet(1)],
            subjects=SUBJ,
            kinds=KINDS,
            plan_and_template_max=2,
            used_subjects=set(),
            used_kinds=set(),
        )


def test_check_meta_refuses_a_subject_named_all():
    """all is the Report's every-subject pick: a subject can't take its name."""
    with pytest.raises(ops.OpError, match="bad name all"):
        ops.check_meta(
            [ops.SubjectSet("all", "All")],
            subjects=SUBJ,
            kinds=KINDS,
            plan_and_template_max=2,
            used_subjects=set(),
            used_kinds=set(),
        )


def test_json_round_trip():
    xs = [
        ops.Move(THU, "math", "learn", FRI, 1, for_request=7),
        ops.Report(THU, "math", "learn", 2, "x"),
        ops.Withdraw(5),
        ops.TemplateSet(0, "main", "math", "learn", 2),
        ops.SubjectSet("art", "Art"),
        ops.KindSet("drill"),
        ops.CapSet(5),
    ]
    assert ops.from_json(ops.to_json(xs)) == xs


def test_parse():
    assert ops.parse(
        {"op": "move", "date": "2026-11-12", "subject": "math", "kind": "learn", "to": "2026-11-13"}
    ) == ops.Move(THU, "math", "learn", date(2026, 11, 13), None, None)
    with pytest.raises(ops.OpError, match="unknown op"):
        ops.parse({"op": "teleport"})
    with pytest.raises(ops.OpError, match="missing field"):
        ops.parse({"op": "move", "date": "2026-11-12", "subject": "math", "kind": "learn"})


def test_parse_type_validation():
    # sessions as string rejected
    with pytest.raises(ops.OpError, match="must be an int"):
        ops.parse(
            {
                "op": "move",
                "date": "2026-11-12",
                "subject": "math",
                "kind": "learn",
                "to": "2026-11-13",
                "sessions": "2",
            }
        )
    # sessions as bool rejected
    with pytest.raises(ops.OpError, match="must be an int"):
        ops.parse(
            {
                "op": "add",
                "date": "2026-11-12",
                "part": "main",
                "subject": "math",
                "kind": "learn",
                "sessions": True,
            }
        )
    # weekday as string rejected
    with pytest.raises(ops.OpError, match="must be an int"):
        ops.parse(
            {
                "op": "template",
                "weekday": "1",
                "part": "main",
                "subject": "math",
                "kind": "learn",
                "sessions": 2,
            }
        )
    # bad part rejected
    with pytest.raises(ops.OpError, match="must be one of"):
        ops.parse(
            {
                "op": "add",
                "date": "2026-11-12",
                "part": "intro",
                "subject": "math",
                "kind": "learn",
                "sessions": 1,
            }
        )
    # non-ISO date rejected
    with pytest.raises(ops.OpError, match="must be an ISO date"):
        ops.parse(
            {
                "op": "move",
                "date": "11/12/2026",
                "subject": "math",
                "kind": "learn",
                "to": "2026-11-13",
            }
        )


@pytest.mark.parametrize("n", [0, -1])
@pytest.mark.parametrize(
    "make",
    [
        lambda n: ops.Move(THU, "math", "learn", FRI, n),
        lambda n: ops.Add(SAT, "main", "finance", "learn", n),
        lambda n: ops.Remove(THU, "math", "learn", n),
    ],
    ids=["move", "add", "remove"],
)
def test_sessions_below_one_refused(plan, make, n):
    with pytest.raises(ops.OpError, match="^sessions must be at least 1$"):
        ap(plan, [make(n)])
