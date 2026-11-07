"""Mentor channels: messages between orrery and the mentors, and the collect step."""

import json
import sqlite3
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from orrery import ledger, schedule
from orrery.text import fmt_day, phrase


class ChannelError(ValueError):
    """A message refused at post time, or an answer or ack aimed at the wrong message."""


@dataclass(frozen=True)
class Message:
    id: int
    channel: str
    kind: str
    author: str
    body: dict
    text: str
    reply_to: int | None
    key: str | None
    state: str
    created_at: str


@dataclass(frozen=True)
class AgendaEntry:
    date: date
    item: str
    kind: str | None = None
    deadline: bool = False


@dataclass(frozen=True)
class Inbox:
    questions: tuple[Message, ...]
    outcomes: tuple[Message, ...]
    awaiting: tuple[Message, ...]
    due: date | None = None  # the Monday of a week whose assessment is due


@dataclass(frozen=True)
class Assessment:
    id: int
    week: date
    strong: str
    shaky: str
    next: str
    at: datetime


@dataclass(frozen=True)
class ChannelStatus:
    subject: str
    open_questions: int
    pending_requests: int
    unacked_outcomes: int


@dataclass(frozen=True)
class Collected:
    reports: int
    agenda: int
    rejected: int
    questions: int
    change_id: int | None


_COLS = "id, channel, kind, author, body, text, reply_to, key, state, created_at"
_EFFECTIVE = "id not in (select replaces from progress where replaces is not null)"


def _messages(conn: sqlite3.Connection, where: str, params: tuple = ()) -> tuple[Message, ...]:
    rows = conn.execute(f"select {_COLS} from messages where {where} order by id", params)
    return tuple(Message(*r[:4], json.loads(r[4]), *r[5:]) for r in rows)


def _get(conn: sqlite3.Connection, message_id: int) -> Message | None:
    return next(iter(_messages(conn, "id = ?", (message_id,))), None)


def _find(conn: sqlite3.Connection, channel: str, kind: str, key: str | None) -> int | None:
    row = conn.execute(
        "select id from messages where channel = ? and kind = ? and key = ?", (channel, kind, key)
    ).fetchone()
    return row and row[0]


def _post(
    conn: sqlite3.Connection,
    channel: str,
    kind: str,
    author: str,
    body: dict,
    *,
    state: str,
    now: datetime,
    text: str = "",
    key: str | None = None,
    reply_to: int | None = None,
) -> int:
    """Insert a message; a key already used on (channel, kind) returns that message's id."""
    cur = conn.execute(
        "insert into messages (channel, kind, author, body, text, reply_to, key, state, created_at)"
        " values (?, ?, ?, ?, ?, ?, ?, ?, ?) on conflict do nothing",
        (
            channel,
            kind,
            author,
            json.dumps(body),
            text,
            reply_to,
            key,
            state,
            now.isoformat(timespec="seconds"),
        ),
    )
    found = cur.lastrowid if cur.rowcount else _find(conn, channel, kind, key)
    assert found is not None
    return found


def _check_channel(conn: sqlite3.Connection, channel: str) -> None:
    if channel not in {s.name for s in schedule.subjects(conn)}:
        raise ChannelError(f"unknown channel {channel}")


def _check_kind(conn: sqlite3.Connection, kind: str) -> None:
    if kind not in schedule.kinds(conn):
        raise ChannelError(f"unknown kind {kind}")


def report(
    conn: sqlite3.Connection,
    *,
    channel: str,
    date: date,
    kind: str,
    sessions: int,
    status: str,
    note: str,
    key: str,
    now: datetime,
    boundary_hour: int,
    replaces: int | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
) -> int:
    """Stage one sitting's progress; collect turns it into a progress row."""
    _check_channel(conn, channel)
    if not key:
        raise ChannelError("key is required")
    # A retry returns the first post before validation, which it would now fail (replaces).
    if (existing := _find(conn, channel, "report", key)) is not None:
        return existing
    if date > schedule.today(now, boundary_hour):
        raise ChannelError("no reports for future days")
    _check_kind(conn, kind)
    if sessions < 0:
        raise ChannelError("sessions can't be negative")
    if status not in ("done", "partial"):
        raise ChannelError("status is done or partial")
    for name, value in (("started_at", started_at), ("ended_at", ended_at)):
        try:
            if value:  # a report is never edited, so its times can't be filled in later: parse now
                datetime.fromisoformat(value)
        except ValueError:
            raise ChannelError(f"{name} must be an ISO datetime") from None
    if replaces is not None:
        latest = conn.execute(
            "select 1 from messages m where id = ? and kind = 'report' and channel = ?"
            " and state != 'rejected' and not exists (select 1 from messages r"
            " where r.kind = 'report' and r.state != 'rejected'"
            " and json_extract(r.body, '$.replaces') = m.id)",
            (replaces, channel),
        ).fetchone()
        if latest is None:
            raise ChannelError("replaces must name the latest report for that entry")
    body = {
        "date": date.isoformat(),
        "kind": kind,
        "sessions": sessions,
        "status": status,
        "note": note,
        "replaces": replaces,
        "started_at": started_at,
        "ended_at": ended_at,
    }
    return _post(
        conn, channel, "report", f"mentor:{channel}", body, state="staged", now=now, key=key
    )


def agenda(
    conn: sqlite3.Connection, *, channel: str, entries: Sequence[AgendaEntry], now: datetime
) -> int:
    """Stage the mentor's upcoming topics and deadlines."""
    _check_channel(conn, channel)
    active = schedule.kinds(conn)
    if any(not e.item.strip() or (e.kind is not None and e.kind not in active) for e in entries):
        raise ChannelError("bad agenda entry")
    body = {
        "entries": [
            {"date": e.date.isoformat(), "item": e.item, "kind": e.kind, "deadline": e.deadline}
            for e in entries
        ]
    }
    return _post(conn, channel, "agenda", f"mentor:{channel}", body, state="staged", now=now)


def request(
    conn: sqlite3.Connection,
    *,
    channel: str,
    text: str,
    kind: str,
    delta: int,
    by: date | None,
    key: str,
    now: datetime,
) -> int:
    """File a schedule change the mentor wants; it waits for the user."""
    _check_channel(conn, channel)
    if not key:
        raise ChannelError("key is required")
    if (existing := _find(conn, channel, "request", key)) is not None:
        return existing
    _check_kind(conn, kind)
    if delta == 0:
        raise ChannelError("delta can't be 0")
    body = {"kind": kind, "delta": delta, "by": by.isoformat() if by else None}
    return _post(
        conn,
        channel,
        "request",
        f"mentor:{channel}",
        body,
        state="pending",
        now=now,
        text=text,
        key=key,
    )


def ask(
    conn: sqlite3.Connection,
    *,
    channel: str,
    text: str,
    author: str,
    now: datetime,
    key: str | None = None,
    about: dict | None = None,
) -> int:
    """Post an open question on a channel."""
    _check_channel(conn, channel)
    return _post(
        conn, channel, "question", author, about or {}, state="open", now=now, text=text, key=key
    )


def answer(
    conn: sqlite3.Connection, *, question_id: int, text: str, author: str, now: datetime
) -> int:
    """File the answer and close the question, in one change."""
    with ledger.change(conn, actor=author, kind="message", now=now):
        q = _get(conn, question_id)
        if q is None or q.kind != "question" or q.state != "open":
            raise ChannelError(f"question #{question_id} isn't open")
        aid = _post(
            conn, q.channel, "answer", author, {}, state="filed", now=now, text=text, reply_to=q.id
        )
        conn.execute("update messages set state = 'answered' where id = ?", (q.id,))
    return aid


def ack(conn: sqlite3.Connection, *, message_id: int, author: str, now: datetime) -> None:
    """Mark an outcome as acted on; acking it again changes nothing."""
    with ledger.change(conn, actor=author, kind="message", now=now):
        m = _get(conn, message_id)
        if m is None or m.kind != "outcome":
            raise ChannelError(f"#{message_id} isn't an outcome")
        conn.execute(
            "update messages set state = 'acked' where id = ? and state = 'unacked'", (message_id,)
        )


def assess(
    conn: sqlite3.Connection,
    *,
    channel: str,
    week: date,
    strong: str,
    shaky: str,
    next: str,
    today: date,
    now: datetime,
) -> int:
    """File the mentor's assessment of a finished week; a second one for that week is a no-op."""
    _check_channel(conn, channel)
    if week.weekday() != 0:
        raise ChannelError("week must be a Monday")
    if week >= schedule.week_start(today):
        raise ChannelError(f"the week of {week.isoformat()} isn't over yet")
    if week < schedule.week_start(schedule.starts(conn)):
        raise ChannelError(f"the week of {week.isoformat()} is before orrery started")
    if not (strong.strip() and shaky.strip() and next.strip()):
        raise ChannelError("strong, shaky and next can't be empty")
    body = {"week": week.isoformat(), "strong": strong, "shaky": shaky, "next": next}
    return _post(
        conn,
        channel,
        "assessment",
        f"mentor:{channel}",
        body,
        state="applied",
        now=now,
        key=week.isoformat(),
    )


def assessment_due(conn: sqlite3.Connection, channel: str, today: date) -> date | None:
    """Last week's Monday, while that week planned the subject and has no assessment."""
    prev = schedule.week_start(today) - timedelta(days=7)
    if prev < schedule.week_start(schedule.starts(conn)):
        return None
    days = schedule.read_plan(conn, prev).values()
    if not any(b.subject == channel for day in days for b in day):
        return None
    return None if _find(conn, channel, "assessment", prev.isoformat()) is not None else prev


def assessments(conn: sqlite3.Connection, channel: str) -> list[Assessment]:
    """A channel's assessments, newest week first."""
    out = [
        Assessment(
            m.id,
            date.fromisoformat(m.body["week"]),
            m.body["strong"],
            m.body["shaky"],
            m.body["next"],
            datetime.fromisoformat(m.created_at),
        )
        for m in _messages(conn, "channel = ? and kind = 'assessment'", (channel,))
    ]
    return sorted(out, key=lambda a: a.week, reverse=True)


def inbox(conn: sqlite3.Connection, channel: str, today: date | None = None) -> Inbox:
    """A channel's waiting messages; with today, also the week whose assessment is due."""

    def pick(kind: str, state: str) -> tuple[Message, ...]:
        return _messages(conn, "channel = ? and kind = ? and state = ?", (channel, kind, state))

    due = assessment_due(conn, channel, today) if today else None
    return Inbox(
        pick("question", "open"), pick("outcome", "unacked"), pick("request", "pending"), due
    )


def pending_requests(conn: sqlite3.Connection) -> tuple[Message, ...]:
    return _messages(conn, "kind = 'request' and state = 'pending'")


def answers(conn: sqlite3.Connection, since: str) -> tuple[tuple[Message, Message], ...]:
    """(question, answer) pairs whose answer was created at or after `since`."""
    filed = _messages(conn, "kind = 'answer' and created_at >= ?", (since,))
    return tuple((_get(conn, a.reply_to), a) for a in filed)


def status(conn: sqlite3.Connection) -> tuple[ChannelStatus, ...]:
    out = []
    for s in schedule.subjects(conn):
        box = inbox(conn, s.name)
        out.append(ChannelStatus(s.name, len(box.questions), len(box.awaiting), len(box.outcomes)))
    return tuple(out)


def done_counts(
    conn: sqlite3.Connection, start: date, end: date, status: str | None = None
) -> Counter[tuple[date, str, str]]:
    """Sessions done per (date, subject, kind), from effective progress; with status, only
    the sessions of reports with that status."""
    rows = conn.execute(
        f"select date, subject, kind, sum(sessions) from progress where {_EFFECTIVE}"
        " and date between ? and ? and (? is null or status = ?) group by date, subject, kind",
        (start.isoformat(), end.isoformat(), status, status),
    )
    return Counter({(date.fromisoformat(d), s, k): n for d, s, k, n in rows})


def _staged(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    return conn.execute(
        "select id, kind from messages where state = 'staged' and kind in ('report', 'agenda')"
        " order by id"
    ).fetchall()


def _due_questions(conn: sqlite3.Connection, today: date) -> list[tuple[str, str, dict, str]]:
    """(channel, key, body, text) for each recent planned block nobody has reported."""
    first = max(schedule.starts(conn), today - timedelta(days=7))
    done = done_counts(conn, first, today)
    out = []
    for i in range((today - first).days):
        d = first + timedelta(days=i)
        planned: dict[tuple[str, str], int] = {}
        for b in schedule.read_plan(conn, schedule.week_start(d))[d]:
            planned[b.subject, b.kind] = planned.get((b.subject, b.kind), 0) + b.sessions
        for (s, k), n in planned.items():
            key = f"rule:{d}:{s}:{k}"
            if (d, s, k) not in done and _find(conn, s, "question", key) is None:
                body = {"date": d.isoformat(), "subject": s, "kind": k}
                text = f"{fmt_day(d)} {phrase(s, k, n)}: nothing reported — what happened?"
                out.append((s, key, body, text))
    return out


_APPLY_REPORT = """insert into progress (date, subject, kind, sessions, status, note,
  started_at, ended_at, actor, replaces, message_id, change_id, created_at)
select json_extract(body, '$.date'), channel, json_extract(body, '$.kind'),
  json_extract(body, '$.sessions'), json_extract(body, '$.status'),
  coalesce(json_extract(body, '$.note'), ''), json_extract(body, '$.started_at'),
  json_extract(body, '$.ended_at'), 'mentor:' || channel, ?, id, ?, ?
from messages where id = ?"""

# Fields read via the entry's full path, so a malformed entry gives NULLs that the
# constraints reject rather than a JSON error that would abort the whole collect.
_APPLY_AGENDA = """insert into agenda (subject, date, kind, item, deadline, message_id,
  change_id, created_at)
select m.channel, json_extract(m.body, e.fullkey || '.date'),
  json_extract(m.body, e.fullkey || '.kind'), json_extract(m.body, e.fullkey || '.item'),
  coalesce(json_extract(m.body, e.fullkey || '.deadline'), 0), m.id, ?, ?
from messages m, json_each(m.body, '$.entries') e where m.id = ? order by e.key"""


def _apply(conn: sqlite3.Connection, mid: int, kind: str, cid: int, at: str) -> bool:
    """Write one staged message's rows; False when it replaces a report that has no row."""
    if kind == "agenda":
        conn.execute(_APPLY_AGENDA, (cid, at, mid))
        return True
    (old,) = conn.execute(
        "select json_extract(body, '$.replaces') from messages where id = ?", (mid,)
    ).fetchone()
    replaces = None
    if old is not None:
        row = conn.execute("select id from progress where message_id = ?", (old,)).fetchone()
        if row is None:  # the replaced report was rejected
            return False
        replaces = row[0]
    conn.execute(_APPLY_REPORT, (replaces, cid, at, mid))
    return True


def collect(conn: sqlite3.Connection, *, now: datetime, today: date) -> Collected:
    """Apply staged reports and agenda, then post rule questions, all in one change."""
    if not _staged(conn) and not _due_questions(conn, today):
        return Collected(0, 0, 0, 0, None)
    at = now.isoformat(timespec="seconds")
    n: Counter[str] = Counter()
    with ledger.change(conn, actor="orrery", kind="collect", now=now) as cid:
        # Re-selected inside the change: an overlapping collect has applied what it saw.
        for mid, kind in _staged(conn):
            conn.execute("savepoint msg")
            try:
                ok = _apply(conn, mid, kind, cid, at)
            except sqlite3.IntegrityError:
                ok = False
            if not ok:
                conn.execute("rollback to msg")
            conn.execute("release msg")
            conn.execute(
                "update messages set state = ?, applied_change = ?"
                " where id = ? and state = 'staged'",
                ("applied" if ok else "rejected", cid, mid),
            )
            n[kind if ok else "rejected"] += 1
        due = _due_questions(conn, today)
        for channel, key, body, text in due:
            _post(
                conn, channel, "question", "orrery", body, state="open", now=now, text=text, key=key
            )
    return Collected(n["report"], n["agenda"], n["rejected"], len(due), cid)
