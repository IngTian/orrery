"""Text formatting utilities for orrery."""

import json
import re
import sqlite3
from datetime import date, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orrery.board import WeekBoard
    from orrery.channels import Inbox

VERDICT_WORDS = (
    "behind",
    "late",
    "failed",
    "fail",
    "overdue",
    "missed",
    "miss",
    "should have",
    "shortfall",
)
_VERDICT_RE = re.compile(r"\b(?:" + "|".join(map(re.escape, VERDICT_WORDS)) + r")\b", re.I)


def has_verdict(s: str) -> bool:
    """True if the text contains any VERDICT_WORDS entry as a whole word, any case."""
    return _VERDICT_RE.search(s) is not None


def fmt_day(d: date) -> str:
    """Format a date as 'Thu 11/12'."""
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    day_name = day_names[d.weekday()]
    return f"{day_name} {d.month}/{d.day}"


def phrase(subject: str, kind: str, sessions: int) -> str:
    """Format a block as 'math · learn ×2', or 'geography ×1' when kind equals subject."""
    if subject == kind:
        return f"{subject} ×{sessions}"
    return f"{subject} · {kind} ×{sessions}"


GLYPHS = {"full": "✓", "half": "½", "none": ""}


def _glyph(mark: str) -> str:
    """Format a mark glyph: ' ✓' when non-empty, '' otherwise."""
    g = GLYPHS[mark]
    return f" {g}" if g else ""


def bar(done: int, planned: int, width: int = 10) -> str:
    """A done/planned bar: a non-zero share shows at least one cell, and it clamps at full."""
    if planned <= 0:
        cells = width if done > 0 else 0
    else:
        cells = min(width, round(done * width / planned))
        if done > 0:
            cells = max(cells, 1)
    return "█" * cells + "░" * (width - cells)


def today_text(b: "WeekBoard", subject: str | None, starts: date = date.min) -> str:
    """Render today's schedule from the board; before starts, nothing is planned."""
    today_board = next(d for d in b.days if d.date == b.today)
    lines = [f"{fmt_day(b.today)} · today"]
    if b.today < starts:
        return "\n".join([*lines, f"nothing planned: orrery starts {fmt_day(starts)}"])

    def mine(s: str) -> bool:
        return subject is None or s == subject

    cells = [c for c in today_board.cells if mine(c.subject)]
    extras = [x for x in today_board.extras if mine(x.subject)]
    deadlines = [d for d in today_board.deadlines if mine(d.split(": ", 1)[0])]  # "{subject}: …"

    if not cells and not extras and not deadlines:
        lines.append("rest")
        return "\n".join(lines)

    for c in cells:
        if c.sessions > 0:
            line = f"{c.part}  {phrase(c.subject, c.kind, c.sessions)}{_glyph(c.mark)}"
            if c.added > 0:
                line += f" (+{c.added})"
            lines.append(line)
        if c.struck > 0:
            moved_part = f" → {fmt_day(c.moved_to)}" if c.moved_to else ""
            lines.append(f"{c.part}  {phrase(c.subject, c.kind, c.struck)} moved{moved_part}")
        if c.topic:
            lines.append(f"  topic: {c.topic}")

    for d in deadlines:
        lines.append(f"due: {d}")

    return "\n".join(lines)


def week_text(b: "WeekBoard", subject: str | None, starts: date = date.min) -> str:
    """Render the week view with 7 days and a > marker on today; a day before starts has
    nothing planned."""
    lines = []
    for day_board in b.days:
        cells = [c for c in day_board.cells if subject is None or c.subject == subject]

        parts = []
        for c in cells:
            if c.sessions > 0:
                parts.append(f"{phrase(c.subject, c.kind, c.sessions)}{_glyph(c.mark)}")
            if c.struck > 0:
                parts.append(f"{phrase(c.subject, c.kind, c.struck)} moved")

        if day_board.date < starts:
            text = f" nothing planned: orrery starts {fmt_day(starts)}"
        elif parts:
            text = " " + ", ".join(parts)
        else:
            text = " rest"

        marker = ">" if day_board.date == b.today else " "
        lines.append(f"{marker} {fmt_day(day_board.date):<10}{text}")

    return "\n".join(lines)


_LATE_RULE = (
    'a "nothing reported" question: look in the vault\'s record of that day, or ask the learner'
    " once. If that kind ran, file the report shown, with sessions and note from the record or"
    " the learner (not the ×N; if neither says how many sessions ran, ask the learner once),"
    " status as report describes, and started_at/ended_at only if the record has times; then"
    " answer as shown, with the id report's reply gave. If another kind ran, its own report"
    ' covers it: file nothing and answer with what ran (e.g. "ran rep ×2"). If nothing ran,'
    " answer with what the record or the learner says."
)
_OTHER_RULE = (
    "any other question: answer from the vault's record, or ask the learner once; never file a"
    " report for it."
)
_FACTS_RULE = 'answers state facts, never judgement (no "late", "missed" or "behind").'
_OUTCOME_RULE = (
    "an outcome: mention it to the learner once; ack(message_id=<its #id>) once it has been acted"
    " on (the learner has re-planned for it, or says nothing is needed)."
)
_AWAITING_RULE = "a request awaiting the learner: nothing to do; its outcome comes here."


def inbox_text(i: "Inbox", mentor: str = "") -> str:
    """Render a channel's inbox. For its mentor (`mentor` is the subject), each item that needs
    a filled-in call says it below, and the rules for acting on the items follow once."""
    if not i.questions and not i.outcomes and not i.awaiting and not i.due:
        return "nothing waiting"

    lines = []
    if i.due:
        week = i.due.isoformat()
        lines.append(f"assessment due: week of {week}")  # vaults match this line; keep it
        if mentor:
            lines.append(
                f'  → assess(subject="{mentor}", week="{week}", strong=…, shaky=…, next=…)'
                " before the sitting's work"
            )
    late = [m for m in i.questions if m.key and m.key.startswith("rule:")]  # nothing reported
    if i.questions:
        lines.append("open questions:")
        for m in i.questions:
            lines.append(f"  #{m.id} {m.text}")
            if mentor and m in late:
                d, k = m.body["date"], m.body["kind"]
                lines.append(
                    f'    → if {k} ran that day: report(date="{d}", kind="{k}",'
                    f' key="{mentor}:late:q{m.id}", …), then answer(question_id={m.id},'
                    f' text="filed report #<report id> for {d}")'
                )

    if i.outcomes:
        lines.append("outcomes to act on:")
        for m in i.outcomes:
            lines.append(f"  #{m.id} {m.text}")

    if i.awaiting:
        lines.append("awaiting the user:")
        for m in i.awaiting:
            lines.append(f"  request #{m.id} {m.text}")

    if mentor:
        rules = [
            (late, _LATE_RULE),
            (len(late) < len(i.questions), _OTHER_RULE),
            (i.questions, _FACTS_RULE),
            (i.outcomes, _OUTCOME_RULE),
            (i.awaiting, _AWAITING_RULE),
        ]
        if said := [f"  {rule}" for shown, rule in rules if shown]:
            lines += ["next steps:", *said]

    return "\n".join(lines)


def history_text(
    conn: sqlite3.Connection,
    *,
    since: date,
    date: date | None = None,
    subject: str | None = None,
) -> str:
    """Render change history, one line per change_id.

    Args:
        conn: Database connection.
        since: Include changes with at >= since.
        date: If set, keep only changes with a history row whose new or old has this date.
        subject: If set, keep only changes with a history row whose new or old has this subject.
    """
    from orrery import ledger

    rows = ledger.history(conn, since=since.isoformat())

    # Group rows by change_id
    by_change = {}
    for row in rows:
        if row.change_id not in by_change:
            by_change[row.change_id] = []
        by_change[row.change_id].append(row)

    lines = []
    for change_id in sorted(by_change.keys()):
        change_rows = by_change[change_id]
        first = change_rows[0]

        # Filter by date or subject if requested
        if date is not None or subject is not None:
            match = False
            for row in change_rows:
                for payload in (row.old, row.new):
                    if payload:
                        if date is not None and payload.get("date") == date.isoformat():
                            match = True
                            break
                        if subject is not None and payload.get("subject") == subject:
                            match = True
                            break
                if match:
                    break
            if not match:
                continue

        # Format: at  actor  kind  proposal #N  requests  reason or tables
        at = first.at
        actor = first.actor
        kind = first.kind
        reason = first.reason
        pid = first.proposal_id

        proposal_part = f"  proposal #{pid}" if pid else ""

        # Extract requests from proposal ops if this is a proposal change
        requests_part = ""
        if pid:
            # Read proposal ops
            prop_row = conn.execute("select ops from proposals where id = ?", (pid,)).fetchone()
            if prop_row:
                ops_json = json.loads(prop_row[0])
                # Collect distinct for_request values
                req_ids = dict.fromkeys(
                    op.get("for_request") for op in ops_json if op.get("for_request")
                )
                for rid in req_ids:
                    msg_row = conn.execute(
                        "select channel, text from messages where id = ?", (rid,)
                    ).fetchone()
                    if msg_row:
                        channel, text = msg_row
                        requests_part += f'  for request #{rid} ({channel}: "{text}")'

        # Tables: comma-joined sorted set
        tables = ", ".join(sorted({row.tbl for row in change_rows}))
        reason_or_tables = reason if reason else tables

        line = f"{at}  {actor}  {kind}{proposal_part}{requests_part}  {reason_or_tables}"
        lines.append(line)

    return "\n".join(lines) or "no changes"


def requests_text(conn: sqlite3.Connection, *, state: str = "all", subject: str = "") -> str:
    """Render requests, one line per request with outcomes indented.

    Args:
        conn: Database connection.
        state: 'all', 'pending', 'granted', 'partial' or 'declined'; a deferred request
            stays pending.
        subject: If non-empty, show only this subject's requests.
    """

    # Fetch requests
    where_parts = ["kind = 'request'"]
    params = []
    if state != "all":
        where_parts.append("state = ?")
        params.append(state)
    if subject:
        where_parts.append("channel = ?")
        params.append(subject)

    where = " and ".join(where_parts)
    rows = conn.execute(
        f"select id, channel, text, body, state, created_at from messages"
        f" where {where} order by id",
        params,
    ).fetchall()

    lines = []
    for rid, channel, text, body_json, msg_state, _created_at in rows:
        body = json.loads(body_json)
        kind = body["kind"]
        delta = body["delta"]
        by = body.get("by")
        by_part = f" by {by}" if by else ""
        line = f"request #{rid} ({channel}, {msg_state}): {text} — ask {kind} {delta:+d}{by_part}"
        lines.append(line)

        # Find outcomes for this request
        outcome_rows = conn.execute(
            "select text from messages where kind = 'outcome' and reply_to = ? order by id",
            (rid,),
        ).fetchall()
        for (outcome_text,) in outcome_rows:
            lines.append(f"  → {outcome_text}")

    return "\n".join(lines)


def overview_text(conn: sqlite3.Connection, today: date) -> str:
    """Render the full organizer overview: the day orrery starts, then five sections."""
    from orrery import board, channels, schedule

    monday = schedule.week_start(today)
    next_monday = monday + timedelta(days=7)
    starts = schedule.starts(conn)

    if starts > today:
        lines = [f"orrery starts {fmt_day(starts)} {starts}; nothing is planned before it", ""]
    else:
        lines = [f"orrery started {fmt_day(starts)} {starts}", ""]

    # Section 1: this week
    b_this = board.build(conn, monday, today)
    lines.append("this week:")
    lines.append(week_text(b_this, None, starts))

    # Section 2: next week
    b_next = board.build(conn, next_monday, today)
    lines.append("")
    lines.append("next week:")
    lines.append(week_text(b_next, None, starts))

    # Section 3: per-channel inboxes
    lines.append("")
    for s in schedule.subjects(conn):
        inbox = channels.inbox(conn, s.name, today)
        lines.append(f"{s.name}:")
        for inbox_line in inbox_text(inbox).splitlines():
            lines.append(f"  {inbox_line}")

    # Section 4: pending requests
    lines.append("")
    lines.append("pending requests:")
    req_text = requests_text(conn, state="pending")
    if req_text:
        for req_line in req_text.splitlines():
            lines.append(f"  {req_line}")
    else:
        lines.append("  none")

    # Section 5: answers (last 7 days)
    lines.append("")
    lines.append("answers (last 7 days):")
    seven_days_ago = (today - timedelta(days=7)).isoformat()
    answer_pairs = channels.answers(conn, seven_days_ago)
    if answer_pairs:
        for q, a in answer_pairs:
            lines.append(f"  #{q.id} {q.text} → {a.text}")
    else:
        lines.append("  none")

    return "\n".join(lines)
