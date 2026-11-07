"""Tests for text formatting utilities."""

import datetime as dt

from helpers import add_agenda, keep_move

from orrery import board, channels, text

MON = dt.date(2026, 11, 9)
WED = dt.date(2026, 11, 11)
THU = dt.date(2026, 11, 12)
FRI = dt.date(2026, 11, 13)


def test_has_verdict():
    assert text.has_verdict("You're behind") and not text.has_verdict("behindhand")


def test_phrase_and_day():
    assert text.phrase("math", "learn", 2) == "math · learn ×2"
    assert text.phrase("geography", "geography", 1) == "geography ×1"
    assert text.fmt_day(dt.date(2026, 11, 12)) == "Thu 11/12"


def test_glyphs():
    assert text.GLYPHS == {"full": "✓", "half": "½", "none": ""}


def test_bar():
    # The default width is 10 cells
    assert text.bar(0, 0) == "░░░░░░░░░░"
    assert text.bar(1, 6) == "██░░░░░░░░"  # round(1*10/6) = round(1.67) = 2
    assert text.bar(3, 6) == "█████░░░░░"  # round(3*10/6) = round(5) = 5
    assert text.bar(6, 6) == "██████████"  # equal to planned fills the bar
    assert text.bar(7, 6) == "██████████"  # clamps at full
    assert text.bar(1, 0) == "██████████"  # any done with no planned fills the bar


def test_today_text_rest_day(week_db):
    """A rest day shows only 'rest'."""
    sun = dt.date(2026, 11, 15)
    b_sun = board.build(week_db, MON, sun)
    expected = "Sun 11/15 · today\nrest"
    assert text.today_text(b_sun, None) == expected


def test_today_text_shows_only_the_subjects_deadlines(week_db):
    add_agenda(week_db, "finance", [channels.AgendaEntry(THU, "pset 3", deadline=True)])
    add_agenda(week_db, "math", [channels.AgendaEntry(THU, "proof X", deadline=True)])
    b = board.build(week_db, MON, THU)
    assert text.today_text(b, "math").splitlines()[-1] == "due: math: proof X"
    assert "pset 3" not in text.today_text(b, "math")
    assert text.today_text(b, "finance") == "Thu 11/12 · today\ndue: finance: pset 3"
    assert "pset 3" in text.today_text(b, None) and "proof X" in text.today_text(b, None)


def test_today_text_with_fixture(week_db):
    """today_text with moved cells, added sessions."""
    from conftest import FROZEN_NOW

    # Build the fixture:
    # 1. Keep move THU math learn → FRI
    keep_move(week_db, THU, "math", "learn", FRI)

    # 2. Add topic on FRI
    add_agenda(
        week_db,
        "math",
        [channels.AgendaEntry(date=FRI, item="review integration methods", kind="learn")],
    )

    # 3. Report with note on WED (math rep)
    key = "test-wed-note"
    channels.report(
        week_db,
        channel="math",
        date=WED,
        kind="rep",
        sessions=1,
        status="done",
        note="did §2.3",
        key=key,
        now=FROZEN_NOW,
        boundary_hour=5,
    )
    channels.collect(week_db, now=FROZEN_NOW, today=THU)

    # 4. Extra on MON (finance rep - not in MON's plan, which only has finance learn)
    extra_key = "test-mon-extra"
    channels.report(
        week_db,
        channel="finance",
        date=MON,
        kind="rep",
        sessions=1,
        status="done",
        note="",
        key=extra_key,
        now=FROZEN_NOW,
        boundary_hour=5,
    )
    channels.collect(week_db, now=FROZEN_NOW, today=THU)

    # Build the board
    b = board.build(week_db, MON, THU)

    # today_text for THU with all subjects
    # THU baseline: math learn ×2 main, geography ×1 residual
    # After move: math learn struck=2, moved_to=FRI; geography ×1
    expected = """Thu 11/12 · today
main  math · learn ×2 moved → Fri 11/13
residual  geography ×1"""
    assert text.today_text(b, None) == expected

    # Test THU with subject="math" filter
    expected_thu_math = """Thu 11/12 · today
main  math · learn ×2 moved → Fri 11/13"""
    assert text.today_text(b, "math") == expected_thu_math


def test_week_text(week_db):
    """week_text shows 7 days with > marker on today."""
    from conftest import FROZEN_NOW

    # Setup fixture
    keep_move(week_db, THU, "math", "learn", FRI)
    add_agenda(
        week_db, "math", [channels.AgendaEntry(date=FRI, item="review integration", kind="learn")]
    )

    # Add a done report to test glyph rendering
    channels.report(
        week_db,
        channel="math",
        date=MON,
        kind="rep",
        sessions=1,
        status="done",
        note="",
        key="test-week-mon-rep",
        now=FROZEN_NOW,
        boundary_hour=5,
    )
    channels.collect(week_db, now=FROZEN_NOW, today=THU)

    b = board.build(week_db, MON, THU)

    # week_text with all subjects
    # MON: math rep ×1 warmup (marked ✓), math learn ×2 main, geography ×1 residual
    # Each day shows phrase + glyph for cells with sessions > 0, and "X moved" for struck
    expected = """  Mon 11/9   math · rep ×1 ✓, math · learn ×2, geography ×1
  Tue 11/10  finance · learn ×2, geography ×1
  Wed 11/11  math · rep ×1, finance · rep ×1, geography ×1
> Thu 11/12  math · learn ×2 moved, geography ×1
  Fri 11/13  math · learn ×4, geography ×1
  Sat 11/14  math · fun ×1, math · rep ×1
  Sun 11/15  rest"""
    assert text.week_text(b, None) == expected


def test_inbox_text_empty(week_db):
    """Empty inbox shows 'nothing waiting'."""
    inbox = channels.inbox(week_db, "math")
    assert text.inbox_text(inbox) == "nothing waiting"


def test_inbox_text_with_messages(week_db):
    """inbox_text shows questions, outcomes, and requests."""
    from conftest import FROZEN_NOW

    # Post a question
    q_id = channels.ask(
        week_db,
        channel="math",
        text="Should we focus on analysis or algebra next?",
        author="orrery",
        now=FROZEN_NOW,
    )

    # Post a request
    r_id = channels.request(
        week_db,
        channel="math",
        text="Add one more session",
        kind="learn",
        delta=1,
        by=None,
        key="test-req",
        now=FROZEN_NOW,
    )

    inbox = channels.inbox(week_db, "math")
    expected = f"""open questions:
  #{q_id} Should we focus on analysis or algebra next?
awaiting the user:
  request #{r_id} Add one more session"""
    assert text.inbox_text(inbox) == expected


def test_inbox_text_shows_assessment_due_even_when_nothing_else_waits(week_db):
    from conftest import FROZEN_NOW

    next_mon = MON + dt.timedelta(days=7)
    due = text.inbox_text(channels.inbox(week_db, "math", next_mon))
    assert due == "assessment due: week of 2026-11-09" and not text.has_verdict(due)
    q = channels.ask(week_db, channel="math", text="which topic?", author="orrery", now=FROZEN_NOW)
    assert text.inbox_text(channels.inbox(week_db, "math", next_mon)) == (
        f"{due}\nopen questions:\n  #{q} which topic?"
    )


def test_history_text_empty(week_db):
    """history_text with no changes says so."""
    # The fixture has setup changes, so query a far-future date
    result = text.history_text(week_db, since=dt.date(2027, 1, 1))
    assert result == "no changes"


def test_history_text_shows_changes(week_db):
    """history_text shows one line per change."""
    from helpers import keep_move

    # Make a change: move THU math learn → FRI
    keep_move(week_db, THU, "math", "learn", FRI)

    # Query history
    result = text.history_text(week_db, since=MON)
    lines = result.splitlines()
    # Should have setup changes plus the move
    assert len(lines) >= 2  # At least setup and the move


def test_history_text_with_request(week_db):
    """history_text includes 'for request #X (channel: "text")'."""
    from conftest import FROZEN_NOW

    from orrery import ops, proposals
    from orrery.organizer import sessions

    # Post a request
    r = channels.request(
        week_db,
        channel="math",
        text="need more time",
        kind="learn",
        delta=1,
        by=None,
        key="test-hist-req",
        now=FROZEN_NOW,
    )
    # Propose and keep a move with for_request
    p = proposals.propose(
        week_db,
        ops=[ops.Move(THU, "math", "learn", FRI, for_request=r)],
        rationale="granting",
        dispositions=[proposals.Disposition(r, "grant")],
        author="organizer",
        now=FROZEN_NOW,
        today=THU,
        session_id=sessions.ensure(week_db, FROZEN_NOW).id,
    )
    proposals.keep(week_db, p.id, now=FROZEN_NOW, today=THU)

    result = text.history_text(week_db, since=MON)
    assert f"for request #{r}" in result
    assert "math:" in result
    assert "need more time" in result


def test_history_text_date_filter(week_db):
    """history_text with date= filters to changes touching that date."""
    from helpers import keep_move

    SAT = dt.date(2026, 11, 14)

    # Move THU math learn → FRI
    keep_move(week_db, THU, "math", "learn", FRI)
    # Move FRI math learn → SAT (different date)
    keep_move(week_db, FRI, "math", "learn", SAT)

    # Query with date=FRI should show changes that touched FRI
    result = text.history_text(week_db, since=MON, date=FRI)
    lines = result.splitlines()
    # Should include changes involving FRI
    assert len(lines) >= 1


def test_history_text_subject_filter(week_db):
    """history_text with subject= filters to changes touching that subject."""
    from helpers import keep_move

    SAT = dt.date(2026, 11, 14)

    keep_move(week_db, THU, "math", "learn", FRI)
    keep_move(week_db, FRI, "geography", "geography", SAT)

    # Get all history
    result_all = text.history_text(week_db, since=MON)
    lines_all = result_all.splitlines()

    # Filter by math - should have fewer or equal lines
    result_math = text.history_text(week_db, since=MON, subject="math")
    lines_math = result_math.splitlines()

    # Filter by geography - should have fewer or equal lines
    result_geography = text.history_text(week_db, since=MON, subject="geography")
    lines_geography = result_geography.splitlines()

    # Filtered results should not be empty and should be <= all
    assert len(lines_math) > 0
    assert len(lines_math) <= len(lines_all)
    assert len(lines_geography) > 0
    assert len(lines_geography) <= len(lines_all)


def test_requests_text_all(week_db):
    """requests_text(state='all') shows all requests."""
    from conftest import FROZEN_NOW

    r1 = channels.request(
        week_db,
        channel="math",
        text="more sessions",
        kind="learn",
        delta=2,
        by=None,
        key="r1",
        now=FROZEN_NOW,
    )
    result = text.requests_text(week_db, state="all")
    assert f"request #{r1}" in result
    assert "math" in result
    assert "more sessions" in result
    assert "ask learn +2" in result


def test_requests_text_pending_only(week_db):
    """requests_text(state='pending') shows only pending requests."""
    from conftest import FROZEN_NOW

    from orrery import ops, proposals
    from orrery.organizer import sessions

    r1 = channels.request(
        week_db,
        channel="math",
        text="more sessions",
        kind="learn",
        delta=2,
        by=None,
        key="r1",
        now=FROZEN_NOW,
    )
    # Grant r1 by proposing and keeping
    p = proposals.propose(
        week_db,
        ops=[ops.Add(FRI, "main", "math", "learn", 2, for_request=r1)],
        rationale="granting",
        dispositions=[proposals.Disposition(r1, "grant")],
        author="organizer",
        now=FROZEN_NOW,
        today=THU,
        session_id=sessions.ensure(week_db, FROZEN_NOW).id,
    )
    proposals.keep(week_db, p.id, now=FROZEN_NOW, today=THU)

    # r1 should now be granted, not pending
    result = text.requests_text(week_db, state="pending")
    assert f"request #{r1}" not in result


def test_requests_text_with_subject_filter(week_db):
    """requests_text(subject='math') shows only math requests."""
    from conftest import FROZEN_NOW

    r1 = channels.request(
        week_db,
        channel="math",
        text="more math",
        kind="learn",
        delta=1,
        by=None,
        key="r1",
        now=FROZEN_NOW,
    )
    r2 = channels.request(
        week_db,
        channel="finance",
        text="more finance",
        kind="learn",
        delta=1,
        by=None,
        key="r2",
        now=FROZEN_NOW,
    )
    result = text.requests_text(week_db, subject="math")
    assert f"request #{r1}" in result
    assert "more math" in result
    assert f"request #{r2}" not in result


def test_overview_text_sections(week_db):
    """overview_text has all five sections."""
    result = text.overview_text(week_db, THU)
    assert "this week:" in result
    assert "next week:" in result
    assert "math:" in result
    assert "finance:" in result
    assert "geography:" in result
    assert "pending requests:" in result
    assert "answers (last 7 days):" in result


def test_overview_text_with_answer(week_db):
    """overview_text shows recent answers."""
    from conftest import FROZEN_NOW

    q = channels.ask(
        week_db, channel="math", text="which topic next?", author="orrery", now=FROZEN_NOW
    )
    channels.answer(week_db, question_id=q, text="topology", author="mentor:math", now=FROZEN_NOW)

    result = text.overview_text(week_db, THU)
    assert "answers (last 7 days):" in result
    assert f"#{q}" in result
    assert "which topic next?" in result
    assert "topology" in result
