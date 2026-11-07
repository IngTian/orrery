"""Tests for the MCP server."""

import datetime as dt
import re
from contextlib import closing

import pytest
from mcp import Client

from orrery import channels, db, ledger, ops, proposals, settings, text
from orrery.mcp_server import MENTOR_INSTRUCTIONS, UNAVAILABLE, build_server

NOW = dt.datetime(2026, 11, 12, 9, 0)


async def test_mentor_tools_listed(week_home):
    """The mentor role registers 9 channel tools."""
    async with Client(build_server(lambda: week_home, lambda: NOW), mode="legacy") as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert names == {
        "today",
        "week",
        "channel",
        "report",
        "agenda",
        "request",
        "answer",
        "ack",
        "assess",
    }


async def test_organizer_role_has_read_and_organizer_tools(week_home):
    """The organizer role registers the 3 read tools plus organizer-only tools, and no mentor
    write tools."""
    async with Client(
        build_server(lambda: week_home, lambda: NOW, role="organizer"), mode="legacy"
    ) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert names == {
        "today",
        "week",
        "channel",
        "overview",
        "history",
        "requests",
        "memory",
        "propose",
        "ask",
        "remember",
        "forget",
    }


async def test_results_are_plain_text(week_home):
    """Each tool has no output_schema and returns plain text content."""
    async with Client(build_server(lambda: week_home, lambda: NOW), mode="legacy") as c:
        tools = (await c.list_tools()).tools
        for tool in tools:
            assert tool.output_schema is None, f"{tool.name} should have no output_schema"

        # Only test calling read-only tools
        for name in ["today", "week", "channel"]:
            result = await c.call_tool(name, {"subject": "math"})
            assert result.structured_content is None, f"{name} should have no structured_content"
            assert len(result.content) > 0, f"{name} should return content"
            assert all(hasattr(b, "text") for b in result.content), f"{name} should return text"


async def test_report_then_collect_counts(week_home):
    """report() files a staged report; collect() turns it into effective progress."""
    from orrery import channels, db

    async with Client(build_server(lambda: week_home, lambda: NOW), mode="legacy") as c:
        result = await c.call_tool(
            "report",
            {
                "subject": "math",
                "date": "2026-11-11",
                "kind": "rep",
                "sessions": 1,
                "status": "done",
                "key": "s",
                "note": "",
            },
        )
        text = "".join(b.text for b in result.content)
        assert "report #1 filed" in text
        assert "next collect" in text

    # Verify collect sees it
    conn = db.open_existing(week_home)
    assert conn is not None
    try:
        collected = channels.collect(conn, now=NOW, today=dt.date(2026, 11, 12))
        assert collected.reports == 1
    finally:
        conn.close()


async def test_future_report_is_tool_error(week_home):
    """A report for a future day is rejected."""
    async with Client(build_server(lambda: week_home, lambda: NOW), mode="legacy") as c:
        result = await c.call_tool(
            "report",
            {
                "subject": "math",
                "date": "2026-11-13",
                "kind": "learn",
                "sessions": 1,
                "status": "done",
                "key": "s",
            },
        )
        assert result.is_error
        text = "".join(b.text for b in result.content)
        assert "no reports for future days" in text


async def test_missing_key_is_tool_error(week_home):
    """A report with an empty key is rejected."""
    async with Client(build_server(lambda: week_home, lambda: NOW), mode="legacy") as c:
        result = await c.call_tool(
            "report",
            {
                "subject": "math",
                "date": "2026-11-11",
                "kind": "learn",
                "sessions": 1,
                "status": "done",
                "key": "",
            },
        )
        assert result.is_error
        text = "".join(b.text for b in result.content)
        assert "key is required" in text


async def test_read_without_db(orrery_home):
    """Read tools with no DB return UNAVAILABLE text, not an error."""
    async with Client(build_server(lambda: orrery_home, lambda: NOW), mode="legacy") as c:
        result = await c.call_tool("today", {"subject": "math"})
        assert not result.is_error
        text = "".join(b.text for b in result.content)
        assert UNAVAILABLE.format(reason="missing") == text


async def test_read_tools_write_nothing(week_home):
    """Read tools leave the database unchanged."""
    from orrery import db

    conn = db.open_existing(week_home)
    assert conn is not None
    try:
        before_changes = conn.execute("select count(*) from changes").fetchone()[0]
        before_messages = conn.execute("select count(*) from messages").fetchone()[0]
        before_progress = conn.execute("select count(*) from progress").fetchone()[0]
    finally:
        conn.close()

    async with Client(build_server(lambda: week_home, lambda: NOW), mode="legacy") as c:
        await c.call_tool("today", {"subject": "math"})
        await c.call_tool("week", {"subject": ""})
        await c.call_tool("channel", {"subject": "finance"})

    conn = db.open_existing(week_home)
    assert conn is not None
    try:
        after_changes = conn.execute("select count(*) from changes").fetchone()[0]
        after_messages = conn.execute("select count(*) from messages").fetchone()[0]
        after_progress = conn.execute("select count(*) from progress").fetchone()[0]
        assert after_changes == before_changes
        assert after_messages == before_messages
        assert after_progress == before_progress
    finally:
        conn.close()


def _text(result) -> str:
    return "".join(b.text for b in result.content)


async def test_reads_name_an_unknown_subject(week_home):
    async with Client(build_server(lambda: week_home, lambda: NOW), mode="legacy") as c:
        for name, subject in [("today", "Math"), ("week", "mathh"), ("channel", "maths")]:
            result = await c.call_tool(name, {"subject": subject})
            assert not result.is_error and _text(result) == f"unknown subject {subject}"
        result = await c.call_tool("week", {"subject": ""})  # every subject
        assert "math · learn ×2" in _text(result) and "finance · learn ×2" in _text(result)


async def test_today_and_week_have_nothing_planned_before_starts(week_home):
    """As the overview: a day before starts has nothing planned, whatever its template says."""
    with closing(db.open_existing(week_home)) as conn:
        with ledger.change(conn, actor="orrery", kind="setup", now=NOW):
            conn.execute("update settings set value = '2026-11-14' where key = 'starts'")
    async with Client(build_server(lambda: week_home, lambda: NOW), mode="legacy") as c:
        today = _text(await c.call_tool("today", {"subject": "math"}))
        week = _text(await c.call_tool("week", {"subject": ""}))
    assert today.startswith("sitting's day: 2026-11-12 ")
    assert "Thu 11/12 · today\nnothing planned: orrery starts Sat 11/14" in today
    assert "math" not in today
    assert "> Thu 11/12  nothing planned: orrery starts Sat 11/14\n" in week
    assert "  Sat 11/14  math · fun ×1, math · rep ×1\n" in week


async def test_answer_and_ack_stay_on_their_channel(week_home):
    with closing(db.open_existing(week_home)) as conn:
        qid = channels.ask(conn, channel="math", text="what happened?", author="orrery", now=NOW)
        oid = conn.execute(
            "insert into messages (channel, kind, author, body, text, state, created_at)"
            " values ('math', 'outcome', 'orrery', '{}', 'request #1: grant', 'unacked',"
            " '2026-11-12T09:00:00')"
        ).lastrowid
    async with Client(build_server(lambda: week_home, lambda: NOW), mode="legacy") as c:
        for subject in ("finance", "nonsense"):
            args = {"subject": subject, "question_id": qid, "text": "done"}
            result = await c.call_tool("answer", args)
            assert result.is_error
            assert _text(result).endswith(f"question #{qid} isn't on {subject}'s channel")
            result = await c.call_tool("ack", {"subject": subject, "message_id": oid})
            assert result.is_error
            assert _text(result).endswith(f"outcome #{oid} isn't on {subject}'s channel")
    with closing(db.open_existing(week_home)) as conn:
        assert [m.id for m in channels.inbox(conn, "math").questions] == [qid]
        assert [m.id for m in channels.inbox(conn, "math").outcomes] == [oid]
        assert not conn.execute("select 1 from changes where actor like 'mentor:%'").fetchone()


async def test_mcp_assess_tool_files_and_channel_clears_due(week_home):
    args = {"subject": "math", "week": "2026-11-09", "strong": "a", "shaky": "b", "next": "c"}
    early = dt.datetime(2026, 11, 16, 4, 0)  # before the day boundary, so still Sunday
    async with Client(build_server(lambda: week_home, lambda: early), mode="legacy") as c:
        assert _text(await c.call_tool("channel", {"subject": "math"})) == "nothing waiting"
        result = await c.call_tool("assess", args)
        assert result.is_error and _text(result).endswith("the week of 2026-11-09 isn't over yet")
    later = dt.datetime(2026, 11, 16, 9, 0)
    async with Client(build_server(lambda: week_home, lambda: later), mode="legacy") as c:
        result = await c.call_tool("channel", {"subject": "math"})
        assert _text(result).splitlines()[0] == "assessment due: week of 2026-11-09"
        result = await c.call_tool("assess", args)
        assert _text(result) == "assessment #1 filed for the week of 2026-11-09"
        assert _text(await c.call_tool("channel", {"subject": "math"})) == "nothing waiting"
        result = await c.call_tool("assess", {**args, "week": "11/9"})
        assert result.is_error and _text(result).endswith("bad date 11/9")


async def _mentor(home, name: str, args: dict, at: dt.datetime = NOW) -> str:
    async with Client(build_server(lambda: home, lambda: at), mode="legacy") as c:
        return _text(await c.call_tool(name, args))


async def _docs(home, role: str = "mentor") -> tuple[dict[str, str], str | None]:
    """Each tool's description, on one line however it wraps, and the server's instructions."""
    async with Client(build_server(lambda: home, lambda: NOW, role=role), mode="legacy") as c:
        tools = (await c.list_tools()).tools
        return {t.name: " ".join(t.description.split()) for t in tools}, c.instructions


async def test_no_tool_defers_to_session_instructions(week_home):
    """Each tool says its own rules; the mentor's text names no vault command or file."""
    for role in ("mentor", "organizer"):
        docs, instructions = await _docs(week_home, role)
        for said in [*docs.values(), instructions or ""]:
            assert "session instructions" not in said.lower(), said
            if role == "mentor":
                assert "/plan" not in said and ".md" not in said, said


async def test_mentor_instructions_are_short_and_mentor_only(week_home):
    assert len(MENTOR_INSTRUCTIONS) <= 1500
    assert (await _docs(week_home))[1] == MENTOR_INSTRUCTIONS
    assert (await _docs(week_home, "organizer"))[1] is None
    for step in ("today(", "channel(", "act on every item", "one report", "facts"):
        assert step in MENTOR_INSTRUCTIONS, step


async def test_report_description_carries_the_key_and_fix_rules(week_home):
    doc = (await _docs(week_home))[0]["report"]
    for rule in (
        "the sitting's day",
        "an increment",
        "partial only if it ended early",
        "<subject>:<kind>:<started_at>",
        "<subject>:<kind>:<ended_at> if started_at was lost",
        "A repeated key is a no-op",
        'key + ":fix", with replaces=<the id its reply gave>',
        "the learner's to fix in orrery; never guess an id",
    ):
        assert rule in doc, rule


@pytest.mark.parametrize(
    ("tool", "rules"),
    [
        ("today", ("the sitting's day", "never refuse")),
        ("channel", ("act on every item",)),
        ("answer", ("facts, never judgement", "ask the learner once")),
        ("ack", ("once it has been acted on", "says nothing is needed", "not the request #n")),
        ("assess", ("one or two sentences", "vault's own record", "your own voice", "not of")),
    ],
)
async def test_mentor_descriptions_carry_their_rules(week_home, tool, rules):
    doc = (await _docs(week_home))[0][tool]
    for rule in rules:
        assert rule in doc, rule


def _set_starts(home, starts: str) -> None:
    with closing(db.open_existing(home)) as conn:
        with ledger.change(conn, actor="orrery", kind="setup", now=NOW):
            conn.execute("update settings set value = ? where key = 'starts'", (starts,))


@pytest.mark.parametrize(
    ("starts", "at", "subject", "kind", "day"),
    [
        ("2026-11-09", NOW, "math", "rep", "2026-11-09"),
        ("2026-11-09", NOW, "geography", "geography", "2026-11-09"),  # its block: geography ×1
        ("2025-12-29", dt.datetime(2026, 1, 2, 9, 0), "math", "rep", "2025-12-29"),  # Mon 12/29
    ],
)
async def test_channel_nothing_reported_question_carries_its_recipe(
    week_home, starts, at, subject, kind, day
):
    """The step under a "nothing reported" question has that question's id, ISO date and
    kind; the rules for every such question come once, after the items."""
    _set_starts(week_home, starts)
    with closing(db.open_existing(week_home)) as conn:
        channels.collect(conn, now=at, today=at.date())
        qid, said = conn.execute(
            "select id, text from messages where key = ?", (f"rule:{day}:{subject}:{kind}",)
        ).fetchone()
    lines = (await _mentor(week_home, "channel", {"subject": subject}, at)).splitlines()
    step = lines[lines.index(f"  #{qid} {said}") + 1]
    assert step == (
        f'    → if {kind} ran that day: report(date="{day}", kind="{kind}",'
        f' key="{subject}:late:q{qid}", …), then answer(question_id={qid},'
        f' text="filed report #<report id> for {day}")'
    )
    assert sum("nothing reported — what happened?" in ln for ln in lines) > 1
    assert sum("its own report covers it" in ln for ln in lines) == 1
    assert not any("any other question" in ln for ln in lines)  # every question is a rule's


async def test_channel_next_steps_cover_every_kind_of_item_once(week_home):
    """A mixed inbox: only a rule's question gets a recipe, and each rule comes once, in order."""
    with closing(db.open_existing(week_home)) as conn:
        channels.collect(conn, now=NOW, today=NOW.date())
        qid = channels.ask(
            conn, channel="math", text="how did the proof go?", author="organizer", now=NOW
        )
        granted, _ = (
            channels.request(
                conn, channel="math", text=t, kind="learn", delta=1, by=None, key=t, now=NOW
            )
            for t in ("r1", "r2")
        )
        p = proposals.propose(
            conn,
            ops=[ops.Add(dt.date(2026, 11, 14), "main", "math", "learn", 1, for_request=granted)],
            rationale="",
            dispositions=[proposals.Disposition(granted, "grant")],
            author="organizer",
            now=NOW,
            today=NOW.date(),
        )
        proposals.keep(conn, p.id, now=NOW, today=NOW.date())
    lines = (await _mentor(week_home, "channel", {"subject": "math"})).splitlines()
    assert sum(ln.startswith("    → if ") for ln in lines) > 1  # the rule questions'
    assert not lines[lines.index(f"  #{qid} how did the proof go?") + 1].startswith("    →")
    assert lines[lines.index("next steps:") :] == [
        "next steps:",
        f"  {text._LATE_RULE}",
        "  any other question: answer from the vault's record, or ask the learner once; never"
        " file a report for it.",
        '  answers state facts, never judgement (no "late", "missed" or "behind").',
        "  an outcome: mention it to the learner once; ack(message_id=<its #id>) once it has been"
        " acted on (the learner has re-planned for it, or says nothing is needed).",
        "  a request awaiting the learner: nothing to do; its outcome comes here.",
    ]


async def test_channel_assessment_due_carries_the_assess_call(week_home):
    at = dt.datetime(2026, 11, 16, 9, 0)
    lines = (await _mentor(week_home, "channel", {"subject": "math"}, at)).splitlines()
    assert lines == [
        "assessment due: week of 2026-11-09",
        '  → assess(subject="math", week="2026-11-09", strong=…, shaky=…, next=…)'
        " before the sitting's work",
    ]


@pytest.mark.parametrize(
    ("at", "boundary", "day"),
    [
        (dt.datetime(2026, 11, 13, 2, 0), None, dt.date(2026, 11, 12)),
        (dt.datetime(2026, 11, 13, 5, 0), None, dt.date(2026, 11, 13)),
        (dt.datetime(2026, 11, 13, 4, 0), 3, dt.date(2026, 11, 13)),
        (dt.datetime(2026, 11, 13, 5, 0), 6, dt.date(2026, 11, 12)),
    ],
)
async def test_today_prints_the_sittings_day_by_the_boundary(week_home, at, boundary, day):
    if boundary is not None:
        cfg = settings.CONFIG_TEMPLATE.replace(
            "day_boundary_hour = 5", f"day_boundary_hour = {boundary}"
        )
        (week_home / "config.toml").write_text(cfg)
    lines = (await _mentor(week_home, "today", {"subject": "math"}, at)).splitlines()
    assert lines[:2] == [
        f"sitting's day: {day.isoformat()} (the report date of a sitting that starts now;"
        " a sitting keeps its first day)",
        f"{text.fmt_day(day)} · today",
    ]


async def test_old_gnomon_patterns_still_match(week_home):
    """Vault sessions on the old text match these two strings; every tool and parameter
    they call is still there."""
    at = dt.datetime(2026, 11, 16, 9, 0)
    with closing(db.open_existing(week_home)) as conn:
        channels.collect(conn, now=at, today=at.date())
    for subject in ("math", "geography"):
        said = await _mentor(week_home, "channel", {"subject": subject}, at)
        if subject == "math":
            assert re.search(r"^assessment due: week of 2026-11-09", said, re.M)
        asked = re.findall(r"^  #\d+ .*: nothing reported — what happened\?$", said, re.M)
        assert asked and all(f" {subject} " in q for q in asked)
    async with Client(build_server(lambda: week_home, lambda: NOW), mode="legacy") as c:
        params = {t.name: set(t.input_schema["properties"]) for t in (await c.list_tools()).tools}
    assert params == {
        "today": {"subject"},
        "week": {"subject"},
        "channel": {"subject"},
        "report": {"subject", "date", "kind", "sessions", "status", "key", "note", "replaces",
                   "started_at", "ended_at"},
        "agenda": {"subject", "entries"},
        "request": {"subject", "text", "kind", "delta", "key", "by"},
        "answer": {"subject", "question_id", "text"},
        "ack": {"subject", "message_id"},
        "assess": {"subject", "week", "strong", "shaky", "next"},
    }  # fmt: skip


@pytest.mark.parametrize("only", ["outcomes", "awaiting"])
def test_next_steps_gate_each_rule_on_its_own_item(only):
    """An inbox of one outcome, or of one request awaiting the learner, gets exactly that
    item's rule under next steps, and the organizer's rendering (no mentor) gets none."""
    m = channels.Message(7, "math", "outcome", "orrery", {}, "kept", None, None, "open", "")
    inbox = channels.Inbox((), (m,), ()) if only == "outcomes" else channels.Inbox((), (), (m,))
    rule = text._OUTCOME_RULE if only == "outcomes" else text._AWAITING_RULE
    rules = text._LATE_RULE, text._OTHER_RULE, text._FACTS_RULE, text._OUTCOME_RULE
    rules += (text._AWAITING_RULE,)
    shown = text.inbox_text(inbox, mentor="math").split("next steps:\n")[1].splitlines()
    assert shown == [f"  {rule}"], shown
    assert "next steps:" not in text.inbox_text(inbox)
    assert sum(r in text.inbox_text(inbox, mentor="math") for r in rules) == 1
    assert "learner" in text._AWAITING_RULE and "user" not in text._AWAITING_RULE
