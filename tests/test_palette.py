"""Tests for subject colour slots."""

from helpers import NOW

from orrery import ledger, palette, schedule


def test_slots_stable_when_a_subject_is_deactivated(week_db):
    before = palette.slots(week_db)
    assert before == {"math": 0, "finance": 1, "geography": 2}
    with ledger.change(week_db, actor="orrery", kind="setup", now=NOW):
        week_db.execute("update subjects set active = 0 where name = 'finance'")
    assert palette.slots(week_db) == before
    assert [s.name for s in schedule.subjects(week_db)] == ["math", "geography"]
    assert [(s.name, s.ord, s.active) for s in schedule.all_subjects(week_db)] == [
        ("math", 0, True),
        ("finance", 1, False),
        ("geography", 2, True),
    ]


def test_hex_for_dark_light_and_fold():
    assert palette.hex_for(0, dark=True) == "#3987e5"
    assert palette.hex_for(0, dark=False) == "#2a78d6"
    assert palette.hex_for(5, dark=True) == "#9085e9"
    assert palette.hex_for(5, dark=False) == "#6250d6"
    assert palette.hex_for(6, dark=True) is None and palette.hex_for(9, dark=False) is None
