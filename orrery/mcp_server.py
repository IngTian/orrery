"""MCP server for orrery: plain-text channel tools for mentors and organizers."""

import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, StrictInt

from orrery import (
    __version__,
    board,
    channels,
    db,
    ops,
    paths,
    proposals,
    schedule,
    settings,
    store,
    text,
)
from orrery.ai import fence
from orrery.organizer import sessions

UNAVAILABLE = "orrery: data unavailable ({reason})"

# The mentor role's server instructions: a summary of what its tools' descriptions and output
# say, since a client may not pass instructions on. The organizer's rules are its prompt.
MENTOR_INSTRUCTIONS = """\
orrery allocates the learner's days across subjects; you mentor one subject. Pass it as \
subject exactly (case-sensitive); if orrery replies "unknown subject", ask the learner once and \
never guess.

At a sitting's start, take the clock's time as the report's started_at and call \
today(subject): its first line is the sitting's day, the date for this sitting's report. Then \
call channel(subject) and act on every item it lists before the sitting's work; each item \
names its next step.

At the sitting's end, file one report; its description gives the date, key and correction \
rules.

Answers state facts, never judgement (no "late", "missed" or "behind").

No tool here changes the schedule: a request waits for the learner's approval.
"""

# Mentor descriptions of the read tools both roles share; the organizer's are the docstrings.
_MENTOR_TODAY = """\
Today's schedule for a subject, headed by the sitting's day.

Call it at a sitting's start, and take the clock's time then as the report's started_at. Pass
the subject exactly (case-sensitive); on "unknown subject", ask the learner once, never guess.
Its first line is the sitting's day (YYYY-MM-DD) by orrery's day boundary: use that date as the
sitting's report date, even if the sitting runs past the boundary. Then today's blocks, marks,
topics and deadlines; before orrery starts, that nothing is planned. If today has no block of
the kind about to run, say so once and follow the learner's call; never refuse. Read-only.
"""
_MENTOR_CHANNEL = """\
A subject's mentor inbox: questions, outcomes, pending requests, and an assessment due.

Call it at a sitting's start, after today, and act on every item it lists before the sitting's
work: an item that needs a call shows it filled in, and the rules for the items follow them.
Read-only.
"""


class AgendaIn(BaseModel):
    date: str
    item: str
    kind: str | None = None
    deadline: bool = False


def _parse_date(s: str) -> date:
    """Parse an ISO date string, raising ValueError on format or validity errors."""
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise ValueError(f"bad date {s}") from None


def _known(conn: sqlite3.Connection, subject: str) -> bool:
    return subject in {s.name for s in schedule.subjects(conn)}


def _on_channel(conn: sqlite3.Connection, message_id: int, subject: str) -> bool:
    """False only for a message that exists on another channel; a missing one is the core's to
    refuse."""
    row = conn.execute("select channel from messages where id = ?", (message_id,)).fetchone()
    return row is None or row[0] == subject


def _struck(ln: proposals.DiffLine) -> str:
    """The line's text with each struck span as ~~phrase~~, since plain text has no strike."""
    t = ln.text
    for a, b in reversed(ln.struck):  # from the end, so earlier offsets still hold
        t = f"{t[:a]}~~{t[a:b]}~~{t[b:]}"
    return t


def _diff_text(conn: sqlite3.Connection, pid: int, today: date) -> str:
    """Proposal #pid's diff as the organizer reads it, or the line saying why it can't be."""
    try:
        lines = proposals.diff(conn, proposals.get(conn, pid), today)
    except Exception as e:  # e.g. ops that no longer parse: the refusal still says what to do
        return f"can't show it: {e}"
    return "\n".join(f"{ln.sign} {_struck(ln)}" for ln in lines)


def build_server(
    root: Callable[[], Path] = paths.data_root,
    now: Callable[[], datetime] = datetime.now,
    role: Literal["mentor", "organizer"] = "mentor",
) -> MCPServer:
    """Build the MCP server with role-specific tools.

    Args:
        root: Function to get the data root path.
        now: Function to get current time.
        role: Server role ("mentor" or "organizer").

    Returns:
        An MCPServer instance with role-specific tools.
    """
    mentor = role == "mentor"
    srv = MCPServer(
        "orrery", version=__version__, instructions=MENTOR_INSTRUCTIONS if mentor else None
    )
    # Mentor-authored text reaches the organizer fenced; a mentor reads its own plain.
    wrap = fence.wrap_corpus if role == "organizer" else (lambda s: s)

    @srv.tool(structured_output=False, description=_MENTOR_TODAY if mentor else None)
    def today(subject: str) -> str:
        """Today's schedule for a subject.

        Returns blocks, marks, topics, and deadlines for today; before orrery starts, that
        nothing is planned. Read-only.
        """
        conn = None
        try:
            conn = db.open_existing(root())
            if isinstance(conn, store.Unavailable):
                return UNAVAILABLE.format(reason=conn.reason)

            cfg = settings.load_settings(root())
            if isinstance(cfg, store.Unavailable):
                return UNAVAILABLE.format(reason=cfg.reason)
            if not _known(conn, subject):
                return f"unknown subject {subject}"

            now_dt = now()
            today_date = schedule.today(now_dt, cfg.day_boundary_hour)
            monday = schedule.week_start(today_date)
            b = board.build(conn, monday, today_date)
            said = text.today_text(b, subject, schedule.starts(conn))
            if mentor:
                day = today_date.isoformat()
                said = (
                    f"sitting's day: {day} (the report date of a sitting that starts now;"
                    f" a sitting keeps its first day)\n{said}"
                )
            return wrap(said)
        finally:
            if conn is not None and not isinstance(conn, store.Unavailable):
                conn.close()

    @srv.tool(structured_output=False)
    def week(subject: str = "") -> str:
        """This week's schedule for a subject.

        Returns all 7 days with blocks, marks, and a today marker; a day before orrery starts
        has nothing planned. Read-only.
        """
        conn = None
        try:
            conn = db.open_existing(root())
            if isinstance(conn, store.Unavailable):
                return UNAVAILABLE.format(reason=conn.reason)

            cfg = settings.load_settings(root())
            if isinstance(cfg, store.Unavailable):
                return UNAVAILABLE.format(reason=cfg.reason)
            if subject and not _known(conn, subject):
                return f"unknown subject {subject}"

            now_dt = now()
            today_date = schedule.today(now_dt, cfg.day_boundary_hour)
            monday = schedule.week_start(today_date)
            b = board.build(conn, monday, today_date)
            return wrap(text.week_text(b, subject or None, schedule.starts(conn)))
        finally:
            if conn is not None and not isinstance(conn, store.Unavailable):
                conn.close()

    @srv.tool(structured_output=False, description=_MENTOR_CHANNEL if mentor else None)
    def channel(subject: str) -> str:
        """A subject's mentor inbox: questions, outcomes, pending requests, and an assessment due.

        Read-only.
        """
        conn = None
        try:
            conn = db.open_existing(root())
            if isinstance(conn, store.Unavailable):
                return UNAVAILABLE.format(reason=conn.reason)

            cfg = settings.load_settings(root())
            if isinstance(cfg, store.Unavailable):
                return UNAVAILABLE.format(reason=cfg.reason)
            if not _known(conn, subject):
                return f"unknown subject {subject}"

            today_date = schedule.today(now(), cfg.day_boundary_hour)
            inbox = channels.inbox(conn, subject, today_date)
            return wrap(text.inbox_text(inbox, mentor=subject if mentor else ""))
        finally:
            if conn is not None and not isinstance(conn, store.Unavailable):
                conn.close()

    if mentor:

        @srv.tool(structured_output=False)
        def report(
            subject: str,
            date: str,
            kind: str,
            sessions: StrictInt,
            status: str,
            key: str,
            note: str = "",
            replaces: StrictInt | None = None,
            started_at: str = "",
            ended_at: str = "",
        ) -> str:
            """File a sitting's report: one per sitting, at its end, whatever it got done.

            - date: the sitting's day (YYYY-MM-DD), as today's first line gave it at the
              sitting's start.
            - kind: what ran. sessions: the sessions done this sitting, as an increment
              (usually 1; never "N of M"). note: one line on what was covered.
            - status describes the sitting: done when it ran its sessions, even if the topic
              carries over; partial only if it ended early.
            - started_at / ended_at: the clock's local time (YYYY-MM-DDTHH:MM:SS) at the
              sitting's start and end, taken then; orrery can't add them later.
            - key: <subject>:<kind>:<started_at>, or <subject>:<kind>:<ended_at> if started_at
              was lost. Build it once; resend it unchanged only to retry a failed call. A
              repeated key is a no-op.
            - To correct a report filed this sitting, correct its latest version: that
              version's key + ":fix", with replaces=<the id its reply gave>. An older report is
              the learner's to fix in orrery; never guess an id.
            - A late report, for a "nothing reported" question, takes the date, kind and key
              channel shows for it.
            """
            conn = None
            try:
                conn = db.open_existing(root())
                if isinstance(conn, store.Unavailable):
                    raise ToolError(UNAVAILABLE.format(reason=conn.reason))

                cfg = settings.load_settings(root())
                if isinstance(cfg, store.Unavailable):
                    raise ToolError(UNAVAILABLE.format(reason=cfg.reason))

                report_date = _parse_date(date)
                now_dt = now()

                msg_id = channels.report(
                    conn,
                    channel=subject,
                    date=report_date,
                    kind=kind,
                    sessions=sessions,
                    status=status,
                    note=note,
                    key=key,
                    now=now_dt,
                    boundary_hour=cfg.day_boundary_hour,
                    replaces=replaces,
                    started_at=started_at if started_at else None,
                    ended_at=ended_at if ended_at else None,
                )
                return f"report #{msg_id} filed; it counts after orrery's next collect"
            except (channels.ChannelError, ValueError) as e:
                raise ToolError(str(e)) from None
            finally:
                if conn is not None and not isinstance(conn, store.Unavailable):
                    conn.close()

        @srv.tool(structured_output=False)
        def agenda(subject: str, entries: list[AgendaIn]) -> str:
            """File an agenda with upcoming topics and deadlines.

            One entry per topic or deadline: date (YYYY-MM-DD, today or later), item, kind
            (the block's kind it's for, or none for any) and deadline (true for a due date).
            For each day it names, it replaces the subject's earlier agenda. It counts after
            orrery's next collect.
            """
            conn = None
            try:
                conn = db.open_existing(root())
                if isinstance(conn, store.Unavailable):
                    raise ToolError(UNAVAILABLE.format(reason=conn.reason))

                cfg = settings.load_settings(root())
                if isinstance(cfg, store.Unavailable):
                    raise ToolError(UNAVAILABLE.format(reason=cfg.reason))

                agenda_entries = []
                for e in entries:
                    entry_date = _parse_date(e.date)
                    agenda_entries.append(
                        channels.AgendaEntry(entry_date, e.item, e.kind, e.deadline)
                    )

                now_dt = now()
                msg_id = channels.agenda(conn, channel=subject, entries=agenda_entries, now=now_dt)
                return f"agenda #{msg_id} filed ({len(entries)} entries)"
            except (channels.ChannelError, ValueError) as e:
                raise ToolError(str(e)) from None
            finally:
                if conn is not None and not isinstance(conn, store.Unavailable):
                    conn.close()

        @srv.tool(structured_output=False)
        def request(
            subject: str, text: str, kind: str, delta: StrictInt, key: str, by: str = ""
        ) -> str:
            """File a schedule change request: more or fewer sessions of a kind.

            delta is the sessions to add or remove (not 0), by an optional YYYY-MM-DD, text the
            why. key is unique to the request (e.g. <subject>:request:<started_at>:<n>); a
            repeated key is a no-op. It waits for the learner's approval, and its outcome comes
            back on channel.
            """
            conn = None
            try:
                conn = db.open_existing(root())
                if isinstance(conn, store.Unavailable):
                    raise ToolError(UNAVAILABLE.format(reason=conn.reason))

                cfg = settings.load_settings(root())
                if isinstance(cfg, store.Unavailable):
                    raise ToolError(UNAVAILABLE.format(reason=cfg.reason))

                by_date = _parse_date(by) if by else None
                now_dt = now()

                msg_id = channels.request(
                    conn,
                    channel=subject,
                    text=text,
                    kind=kind,
                    delta=delta,
                    by=by_date,
                    key=key,
                    now=now_dt,
                )
                return f"request #{msg_id} filed; it waits for the user's approval"
            except (channels.ChannelError, ValueError) as e:
                raise ToolError(str(e)) from None
            finally:
                if conn is not None and not isinstance(conn, store.Unavailable):
                    conn.close()

        @srv.tool(structured_output=False)
        def answer(subject: str, question_id: StrictInt, text: str) -> str:
            """Answer an open question.

            Answer from the vault's own record, or ask the learner once. State facts, never
            judgement (no "late", "missed" or "behind"). File a report only for a "nothing
            reported" question, as its step in channel says.
            """
            conn = None
            try:
                conn = db.open_existing(root())
                if isinstance(conn, store.Unavailable):
                    raise ToolError(UNAVAILABLE.format(reason=conn.reason))

                cfg = settings.load_settings(root())
                if isinstance(cfg, store.Unavailable):
                    raise ToolError(UNAVAILABLE.format(reason=cfg.reason))

                if not _on_channel(conn, question_id, subject):
                    raise ToolError(f"question #{question_id} isn't on {subject}'s channel")
                now_dt = now()
                channels.answer(
                    conn, question_id=question_id, text=text, author=f"mentor:{subject}", now=now_dt
                )
                return f"answered question #{question_id}"
            except (channels.ChannelError, ValueError) as e:
                raise ToolError(str(e)) from None
            finally:
                if conn is not None and not isinstance(conn, store.Unavailable):
                    conn.close()

        @srv.tool(structured_output=False)
        def ack(subject: str, message_id: StrictInt) -> str:
            """Acknowledge an outcome message once it has been acted on.

            Mention the outcome to the learner once, and ack it once the learner has re-planned
            for it, or says nothing is needed; until then it stays on channel. message_id is the
            outcome line's leading #id, not the request #n inside it; acking it again changes
            nothing.
            """
            conn = None
            try:
                conn = db.open_existing(root())
                if isinstance(conn, store.Unavailable):
                    raise ToolError(UNAVAILABLE.format(reason=conn.reason))

                cfg = settings.load_settings(root())
                if isinstance(cfg, store.Unavailable):
                    raise ToolError(UNAVAILABLE.format(reason=cfg.reason))

                if not _on_channel(conn, message_id, subject):
                    raise ToolError(f"outcome #{message_id} isn't on {subject}'s channel")
                now_dt = now()
                channels.ack(conn, message_id=message_id, author=f"mentor:{subject}", now=now_dt)
                return f"acknowledged #{message_id}"
            except (channels.ChannelError, ValueError) as e:
                raise ToolError(str(e)) from None
            finally:
                if conn is not None and not isinstance(conn, store.Unavailable):
                    conn.close()

        @srv.tool(structured_output=False)
        def assess(subject: str, week: str, strong: str, shaky: str, next: str) -> str:
            """File your assessment of a finished week: what was strong, shaky, and next.

            week is that week's Monday (YYYY-MM-DD), as channel's "assessment due" line names it.
            Write one or two sentences each, from the vault's own record of the week and in your
            own voice: your view of the learning, not of keeping to the schedule. One per week:
            filing the same week again changes nothing.
            """
            conn = None
            try:
                conn = db.open_existing(root())
                if isinstance(conn, store.Unavailable):
                    raise ToolError(UNAVAILABLE.format(reason=conn.reason))

                cfg = settings.load_settings(root())
                if isinstance(cfg, store.Unavailable):
                    raise ToolError(UNAVAILABLE.format(reason=cfg.reason))

                monday = _parse_date(week)
                now_dt = now()
                msg_id = channels.assess(
                    conn,
                    channel=subject,
                    week=monday,
                    strong=strong,
                    shaky=shaky,
                    next=next,
                    today=schedule.today(now_dt, cfg.day_boundary_hour),
                    now=now_dt,
                )
                return f"assessment #{msg_id} filed for the week of {monday.isoformat()}"
            except (channels.ChannelError, ValueError) as e:
                raise ToolError(str(e)) from None
            finally:
                if conn is not None and not isinstance(conn, store.Unavailable):
                    conn.close()

    if role == "organizer":

        class DispositionIn(BaseModel):
            request_id: StrictInt
            verdict: Literal["grant", "partial", "decline", "defer"]
            note: str = ""

        @srv.tool(structured_output=False)
        def overview() -> str:
            """Full organizer overview: the day orrery starts (or started), then this week, next
            week, channels, pending requests and answers. Days before the start have nothing
            planned.

            Read-only.
            """
            conn = None
            try:
                conn = db.open_existing(root())
                if isinstance(conn, store.Unavailable):
                    return UNAVAILABLE.format(reason=conn.reason)

                cfg = settings.load_settings(root())
                if isinstance(cfg, store.Unavailable):
                    return UNAVAILABLE.format(reason=cfg.reason)

                now_dt = now()
                today = schedule.today(now_dt, cfg.day_boundary_hour)
                return fence.wrap_corpus(text.overview_text(conn, today))
            finally:
                if conn is not None and not isinstance(conn, store.Unavailable):
                    conn.close()

        @srv.tool(structured_output=False)
        def history(days: StrictInt = 7, date: str = "", subject: str = "") -> str:
            """Change history, optionally filtered by date or subject.

            Read-only.
            """
            conn = None
            try:
                conn = db.open_existing(root())
                if isinstance(conn, store.Unavailable):
                    return UNAVAILABLE.format(reason=conn.reason)

                cfg = settings.load_settings(root())
                if isinstance(cfg, store.Unavailable):
                    return UNAVAILABLE.format(reason=cfg.reason)

                now_dt = now()
                today = schedule.today(now_dt, cfg.day_boundary_hour)
                since = today - timedelta(days=days)

                date_filter = _parse_date(date) if date else None
                return wrap(
                    text.history_text(conn, since=since, date=date_filter, subject=subject or None)
                )
            except ValueError as e:
                raise ToolError(str(e)) from None
            finally:
                if conn is not None and not isinstance(conn, store.Unavailable):
                    conn.close()

        @srv.tool(structured_output=False)
        def requests(state: str = "all", subject: str = "") -> str:
            """List requests, optionally filtered by state or subject.

            States: pending, granted, partial, declined, or all. A deferred request stays
            pending. Read-only.
            """
            conn = None
            try:
                conn = db.open_existing(root())
                if isinstance(conn, store.Unavailable):
                    return UNAVAILABLE.format(reason=conn.reason)

                got = text.requests_text(conn, state=state, subject=subject)
                return wrap(got) if got else "no requests"
            finally:
                if conn is not None and not isinstance(conn, store.Unavailable):
                    conn.close()

        @srv.tool(structured_output=False)
        def memory() -> str:
            """List all memory notes.

            Read-only.
            """
            conn = None
            try:
                conn = db.open_existing(root())
                if isinstance(conn, store.Unavailable):
                    return UNAVAILABLE.format(reason=conn.reason)

                notes = sessions.memory(conn)
                if not notes:
                    return "no notes"
                return "\n".join(f"#{n.id} {n.text}" for n in notes)
            finally:
                if conn is not None and not isinstance(conn, store.Unavailable):
                    conn.close()

        @srv.tool(structured_output=False)
        def propose(
            operations: list[dict],
            rationale: str,
            dispositions: list[DispositionIn] | None = None,
            replaces: StrictInt | None = None,
        ) -> str:
            """Propose schedule changes with optional request dispositions.

            One proposal waits at a time. While one waits, propose is refused unless replaces
            is its id: fold its changes into the new proposal, since the old one is then never
            kept, and orrery tells the user. Otherwise wait for the user's y/n.

            Op shapes (all dates as YYYY-MM-DD; ? marks an optional field):
            - {"op":"move", "date":str, "subject":str, "kind":str, "to":str,
               "sessions":int?, "for_request":int?}
            - {"op":"add", "date":str, "part":"warmup"|"main"|"residual", "subject":str,
               "kind":str, "sessions":int, "for_request":int?}
            - {"op":"remove", "date":str, "subject":str, "kind":str,
               "sessions":int?, "for_request":int?}
            - {"op":"template", "weekday":int, "part":"warmup"|"main"|"residual",
               "subject":str, "kind":str, "sessions":int, "for_request":int?}
            - {"op":"report", "date":str, "subject":str, "kind":str, "sessions":int, "note":str?}
            - {"op":"withdraw", "question_id":int}
            - {"op":"subject", "name":str, "label":str, "active":bool?}
            - {"op":"kind", "name":str, "active":bool?}
            - {"op":"cap", "cap":int}

            Request rules: ops with for_request must have a disposition for that request;
            dispositions only for pending requests.

            The diff marks a struck phrase, a block no longer on that day, as ~~phrase~~.
            """
            if dispositions is None:
                dispositions = []
            conn = None
            try:
                conn = db.open_existing(root())
                if isinstance(conn, store.Unavailable):
                    raise ToolError(UNAVAILABLE.format(reason=conn.reason))

                cfg = settings.load_settings(root())
                if isinstance(cfg, store.Unavailable):
                    raise ToolError(UNAVAILABLE.format(reason=cfg.reason))

                now_dt = now()
                today = schedule.today(now_dt, cfg.day_boundary_hour)

                # Parse ops
                parsed_ops = []
                for raw_op in operations:
                    try:
                        parsed_ops.append(ops.parse(raw_op))
                    except ops.OpError as e:
                        raise ToolError(str(e)) from None

                # Convert dispositions
                disps = [
                    proposals.Disposition(d.request_id, d.verdict, d.note) for d in dispositions
                ]

                # Ensure session
                session = sessions.ensure(conn, now_dt)

                # Propose
                p = proposals.propose(
                    conn,
                    ops=parsed_ops,
                    rationale=rationale,
                    dispositions=disps,
                    author="organizer",
                    now=now_dt,
                    today=today,
                    session_id=session.id,
                    replaces=replaces,
                )

                return f"proposal #{p.id} is waiting for the user:\n{_diff_text(conn, p.id, today)}"
            except proposals.Waiting as e:  # what waits, for the new one to fold in
                raise ToolError(f"{e}. It reads:\n{wrap(_diff_text(conn, e.pid, today))}") from None
            except (ops.OpError, ValueError) as e:
                raise ToolError(str(e)) from None
            finally:
                if conn is not None and not isinstance(conn, store.Unavailable):
                    conn.close()

        @srv.tool(structured_output=False)
        def ask(subject: str, question: str) -> str:
            """Ask a mentor a question.

            Verdict-gated: text must not contain verdict words.
            """
            conn = None
            try:
                conn = db.open_existing(root())
                if isinstance(conn, store.Unavailable):
                    raise ToolError(UNAVAILABLE.format(reason=conn.reason))

                if text.has_verdict(question):
                    raise ToolError("reword: " + question)

                now_dt = now()
                qid = channels.ask(
                    conn, channel=subject, text=question, author="organizer", now=now_dt
                )
                return f"asked {subject} (question #{qid})"
            except channels.ChannelError as e:
                raise ToolError(str(e)) from None
            finally:
                if conn is not None and not isinstance(conn, store.Unavailable):
                    conn.close()

        @srv.tool(structured_output=False)
        def remember(note: str) -> str:
            """Remember a note. Only what the user said or confirmed; never what was done when."""
            conn = None
            try:
                conn = db.open_existing(root())
                if isinstance(conn, store.Unavailable):
                    raise ToolError(UNAVAILABLE.format(reason=conn.reason))

                now_dt = now()
                nid = sessions.remember(conn, note, now_dt)
                return f"remembered #{nid}"
            except ValueError as e:
                raise ToolError(str(e)) from None
            finally:
                if conn is not None and not isinstance(conn, store.Unavailable):
                    conn.close()

        @srv.tool(structured_output=False)
        def forget(note_id: StrictInt) -> str:
            """Forget a memory note by id."""
            conn = None
            try:
                conn = db.open_existing(root())
                if isinstance(conn, store.Unavailable):
                    raise ToolError(UNAVAILABLE.format(reason=conn.reason))

                now_dt = now()
                sessions.forget(conn, note_id, now_dt)
                return f"forgot #{note_id}"
            except ValueError as e:
                raise ToolError(str(e)) from None
            finally:
                if conn is not None and not isinstance(conn, store.Unavailable):
                    conn.close()

    return srv


def serve(role: str = "mentor") -> None:
    """Start the MCP server on stdio.

    Args:
        role: Server role ("mentor" or "organizer").

    This function never returns under normal operation.
    """
    srv = build_server(role=role)
    srv.run(transport="stdio")
