"""Proposals: ops plus request dispositions that only the user's y applies; undo as compensation."""

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from typing import Literal

from orrery import board, channels, ledger, ops, schedule
from orrery.model import Block, Plan, Template
from orrery.ops import Op, OpError, to_json
from orrery.text import fmt_day, has_verdict, phrase

STALE = "the week changed since; nothing written — ask again"
UNDO_CHANGED = "that change was changed since; undo skipped"

_WKD = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_RESOLVED = {"grant": "granted", "partial": "partial", "decline": "declined"}
_META = (ops.TemplateSet, ops.SubjectSet, ops.KindSet, ops.CapSet)
# Diff order after the day and template lines: meta ops, reports, withdraws.
_GROUP = {ops.SubjectSet: 0, ops.KindSet: 0, ops.CapSet: 0, ops.Report: 1, ops.Withdraw: 2}
# The tables undo restores, by primary key. progress is append-only and gets retractions;
# history rows of any other table (e.g. the dropped overrides) are skipped.
_KEYS = {
    "plan_blocks": ("date", "part", "ord"),
    "template_blocks": ("weekday", "part", "ord"),
    "subjects": ("name",),
    "kinds": ("name",),
    "settings": ("key",),
    "messages": ("id",),
}


class Stale(Exception):
    """The proposal no longer matches the database, or the change to undo was changed since."""


@dataclass(frozen=True)
class Disposition:
    request_id: int
    verdict: Literal["grant", "partial", "decline", "defer"]
    note: str = ""


@dataclass(frozen=True)
class Proposal:
    id: int
    ops: tuple[Op, ...]
    rationale: str
    dispositions: tuple[Disposition, ...]
    author: str
    created_at: str
    state: str


@dataclass(frozen=True)
class DiffLine:
    sign: Literal["-", "+", " "]
    text: str
    struck: tuple[tuple[int, int], ...] = ()


def _editable(conn: sqlite3.Connection, today: date) -> Plan:
    """The current plan of the weeks a proposal can change: this week and next."""
    m = schedule.week_start(today)
    return {**schedule.read_plan(conn, m), **schedule.read_plan(conn, m + timedelta(days=7))}


def _validate(conn: sqlite3.Connection, p_ops: Sequence[Op], today: date) -> tuple[Plan, Template]:
    """The editable weeks and the template with the ops applied; OpError if they can't be."""
    plan = _editable(conn, today)
    subjects = {s.name for s in schedule.subjects(conn)}
    kinds = schedule.kinds(conn)
    cap = schedule.cap(conn)
    done = channels.done_counts(conn, min(plan), max(plan))
    new_plan = ops.apply_plan(
        plan, p_ops, today=today, done=done, subjects=subjects, kinds=kinds, cap=cap
    )
    new_tmpl = ops.apply_template(
        schedule.read_template(conn), p_ops, subjects=subjects, kinds=kinds, cap=cap
    )
    # Past days keep their plan, so only today on counts as still planned.
    ahead = [b for d, bs in new_plan.items() if d >= today for b in bs]
    ahead += [b for bs in new_tmpl.values() for b in bs]
    ops.check_meta(
        p_ops,
        subjects=subjects,
        kinds=kinds,
        plan_and_template_max=max((b.sessions for b in ahead), default=0),
        used_subjects={b.subject for b in ahead},
        used_kinds={b.kind for b in ahead},
    )
    for op in p_ops:
        if isinstance(op, ops.Report):
            if op.subject not in subjects:
                raise OpError(f"unknown subject {op.subject}")
            if op.kind not in kinds:
                raise OpError(f"unknown kind {op.kind}")
        if isinstance(op, ops.Report) and op.date > today:
            raise OpError("no reports for future days")
        if isinstance(op, ops.Report) and op.sessions < 0:
            raise OpError("sessions can't be negative")
        if isinstance(op, ops.Withdraw) and _open_question(conn, op.question_id) is None:
            raise OpError(f"question #{op.question_id} isn't open")
    return new_plan, new_tmpl


def _open_question(conn: sqlite3.Connection, qid: int) -> str | None:
    """The channel of an open question, or None."""
    row = conn.execute(
        "select channel from messages where id = ? and kind = 'question' and state = 'open'",
        (qid,),
    ).fetchone()
    return row and row[0]


def _request(conn: sqlite3.Connection, rid: int) -> tuple[str, str]:
    """(channel, text) of a request."""
    return conn.execute("select channel, text from messages where id = ?", (rid,)).fetchone()


def _check_requests(
    conn: sqlite3.Connection, p_ops: Sequence[Op], dispositions: Sequence[Disposition]
) -> None:
    pending = {m.id for m in channels.pending_requests(conn)}
    named = [getattr(op, "for_request", None) for op in p_ops]
    for rid in [*named, *(d.request_id for d in dispositions)]:
        if rid is not None and rid not in pending:
            raise OpError(f"request #{rid} isn't pending")


def _expected(p_ops: Sequence[Op], plan: Plan, tmpl: Template) -> str:
    """The touched days' and weekdays' blocks, as stored in proposals.expected."""

    def rows(blocks: tuple[Block, ...]) -> list:
        return [[b.part, b.subject, b.kind, b.sessions] for b in blocks]

    days = {d.isoformat(): rows(plan[d]) for d in sorted(ops.touched_dates(p_ops))}
    return json.dumps(
        {"days": days, "template": {w: rows(tmpl[w]) for w in sorted(ops.touched_weekdays(p_ops))}}
    )


def _verdict_text(d: Disposition) -> str:
    return f"request #{d.request_id}: {d.verdict}{' — ' + d.note if d.note else ''}"


def _outcome(conn: sqlite3.Connection, rid: int, body: dict, text: str, at: str) -> None:
    """Post an outcome on the request's channel, in reply to it."""
    conn.execute(
        "insert into messages (channel, kind, author, body, text, reply_to, state, created_at)"
        " select channel, 'outcome', 'orrery', ?, ?, id, 'unacked', ? from messages where id = ?",
        (json.dumps(body), text, at, rid),
    )


def propose(
    conn: sqlite3.Connection,
    *,
    ops: Sequence[Op],
    rationale: str,
    dispositions: Sequence[Disposition],
    author: str,
    now: datetime,
    today: date,
    session_id: int | None = None,
) -> Proposal:
    """Store the one waiting proposal, replacing any other. Nothing else is written."""
    plan, tmpl = _validate(conn, ops, today)
    for t in (rationale, *(d.note for d in dispositions)):
        if has_verdict(t):
            raise OpError("reword: " + t)
    for d in dispositions:
        if d.verdict not in (*_RESOLVED, "defer"):
            raise OpError(f"unknown verdict {d.verdict}")
        if d.verdict == "partial" and not d.note.strip():
            raise OpError("say what partial gives")
    _check_requests(conn, ops, dispositions)
    labelled = [d.request_id for d in dispositions]
    for rid in labelled:
        if labelled.count(rid) > 1:
            raise OpError(f"request #{rid} has two labels")
    for op in ops:
        if (rid := getattr(op, "for_request", None)) is not None and rid not in labelled:
            raise OpError(f"request #{rid} needs a label")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("update proposals set state = 'replaced' where state = 'waiting'")
        pid = conn.execute(
            "insert into proposals (created_at, author, ops, rationale, dispositions, expected,"
            " state, session_id) values (?, ?, ?, ?, ?, ?, 'waiting', ?)",
            (
                now.isoformat(timespec="seconds"),
                author,
                to_json(ops),
                rationale,
                json.dumps([asdict(d) for d in dispositions]),
                _expected(ops, plan, tmpl),
                session_id,
            ),
        ).lastrowid
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    assert pid is not None
    return get(conn, pid)


def get(conn: sqlite3.Connection, proposal_id: int) -> Proposal:
    row = conn.execute(
        "select id, ops, rationale, dispositions, author, created_at, state from proposals"
        " where id = ?",
        (proposal_id,),
    ).fetchone()
    if row is None:
        raise OpError(f"no proposal #{proposal_id}")
    pid, ops_json, rationale, disp, author, created_at, state = row
    return Proposal(
        pid,
        tuple(ops.from_json(ops_json)),
        rationale,
        tuple(Disposition(**d) for d in json.loads(disp)),
        author,
        created_at,
        state,
    )


def waiting(conn: sqlite3.Connection) -> Proposal | None:
    row = conn.execute("select id from proposals where state = 'waiting'").fetchone()
    return get(conn, row[0]) if row else None


def _current(
    conn: sqlite3.Connection, proposal_id: int, today: date
) -> tuple[Proposal, Plan, Template]:
    """The waiting proposal applied to the database now; Stale unless that is what was shown."""
    p = get(conn, proposal_id)
    if p.state != "waiting":
        raise Stale(STALE)
    plan, tmpl = _validate(conn, p.ops, today)
    _check_requests(conn, p.ops, p.dispositions)
    (expected,) = conn.execute("select expected from proposals where id = ?", (p.id,)).fetchone()
    if _expected(p.ops, plan, tmpl) != expected:
        raise Stale(STALE)
    return p, plan, tmpl


def _cells(cells: Sequence[board.Cell]) -> list[tuple[str, bool]]:
    """(phrase, struck-only) for each cell that shows."""
    return [
        (phrase(c.subject, c.kind, c.sessions or c.struck), not c.sessions)
        for c in cells
        if c.sessions or c.struck
    ]


def _blocks(blocks: Sequence[Block]) -> list[tuple[str, bool]]:
    return [(phrase(b.subject, b.kind, b.sessions), False) for b in blocks]


def _line(sign: Literal["-", "+"], head: str, items: list[tuple[str, bool]], suffix="") -> DiffLine:
    text, struck = f"{head}  ", []
    for i, (ph, is_struck) in enumerate(items):
        text += ", " if i else ""
        if is_struck:
            struck.append((len(text), len(text) + len(ph)))
        text += ph
    return DiffLine(sign, (text if items else text + "rest") + suffix, tuple(struck))


def _rids(related: Sequence[Op]) -> list[int]:
    """The requests behind these ops, each once, in order."""
    named = (getattr(op, "for_request", None) for op in related)
    return list(dict.fromkeys(r for r in named if r is not None))


def _asks(related: Sequence[Op]) -> str:
    """The `  (request #n, request #m)` suffix naming the requests behind these ops; their
    text is quoted once, under the rationale."""
    rids = _rids(related)
    return f"  ({', '.join(f'request #{r}' for r in rids)})" if rids else ""


def _status(day: Sequence[Block], op: ops.Report) -> str:
    """done or partial: how keep stores the report against its day's plan."""
    planned = sum(b.sessions for b in day if (b.subject, b.kind) == (op.subject, op.kind))
    return "done" if op.sessions >= planned else "partial"


def _op_lines(conn: sqlite3.Connection, op: Op, plan: Plan) -> list[DiffLine]:
    match op:
        case ops.SubjectSet(name, label, active):
            return [DiffLine("+", f"subject {name} ({label}){'' if active else ' off'}")]
        case ops.KindSet(name, active):
            return [DiffLine("+", f"kind {name}{'' if active else ' off'}")]
        case ops.CapSet(cap):
            return [DiffLine("+", f"cap {cap}")]
        case ops.Report(d, subject, kind, sessions):
            day = plan[d] if d in plan else schedule.read_plan(conn, schedule.week_start(d))[d]
            what = f"report {phrase(subject, kind, sessions)} {_status(day, op)}"
            return [DiffLine("+", f"{fmt_day(d)}  {what}")]
        case ops.Withdraw(qid):
            return [DiffLine("+", f"withdraw question #{qid} ({_open_question(conn, qid)})")]
    return []


def diff(conn: sqlite3.Connection, p: Proposal, today: date) -> list[DiffLine]:
    """The proposal as -/+ lines against the database now. It never raises, and a line break
    in a mentor's text can't start a line of its own."""
    return [replace(ln, text=ln.text.translate(_FLAT)) for ln in _diff(conn, p, today)]


_FLAT = str.maketrans("\r\n", "  ")  # 1:1, so struck spans stay where they were


def _diff(conn: sqlite3.Connection, p: Proposal, today: date) -> list[DiffLine]:
    lines = [DiffLine(" ", f"proposal #{p.id} · {p.author}")]
    if p.rationale:
        lines.append(DiffLine(" ", p.rationale))
    try:
        plan, tmpl = _validate(conn, p.ops, today)
        _check_requests(conn, p.ops, p.dispositions)
    except Exception as e:  # e.g. a proposal stored before a check existed
        return [*lines, DiffLine(" ", f"can't apply now: {e}")]
    for r in _rids(p.ops):  # pending, as just checked
        channel, said = _request(conn, r)
        lines.append(DiffLine(" ", f'{channel} request #{r}: "{said}"'))
    dates = sorted(ops.touched_dates(p.ops))
    boards = {
        m: (board.build(conn, m, today), board.build(conn, m, today, plan=plan))
        for m in {schedule.week_start(d) for d in dates}
    }
    for d in dates:
        old, new = (b.days[d.weekday()].cells for b in boards[schedule.week_start(d)])
        related = [op for op in p.ops if d in ops.touched_dates([op])]
        lines.append(_line("-", fmt_day(d), _cells(old)))
        lines.append(_line("+", fmt_day(d), _cells(new), _asks(related)))
    old_tmpl = schedule.read_template(conn)
    for w in sorted(ops.touched_weekdays(p.ops)):
        related = [op for op in p.ops if w in ops.touched_weekdays([op])]
        lines.append(_line("-", f"every {_WKD[w]}", _blocks(old_tmpl[w])))
        lines.append(_line("+", f"every {_WKD[w]}", _blocks(tmpl[w]), _asks(related)))
    for op in sorted((op for op in p.ops if type(op) in _GROUP), key=lambda op: _GROUP[type(op)]):
        lines += _op_lines(conn, op, plan)
    for d in p.dispositions:
        lines.append(DiffLine(" ", f"{_request(conn, d.request_id)[0]} {_verdict_text(d)}"))
    # keep leaves an untouched, unpinned next week to the template, so it takes the new one.
    before, after = _editable(conn, today), dict(plan)
    nxt = schedule.week_start(today) + timedelta(days=7)
    if not schedule.is_pinned(conn, nxt) and all(schedule.week_start(d) != nxt for d in dates):
        after.update({d: tmpl[d.weekday()] for d in schedule.week_dates(nxt)})
    for s in schedule.subjects(conn):
        a, b = (
            sum(x.sessions for bs in pl.values() for x in bs if x.subject == s.name)
            for pl in (before, after)
        )
        if a != b:
            lines.append(DiffLine(" ", f"plan fit  {s.label} planned {a} → {b}"))
    return lines


def _write(conn: sqlite3.Connection, op: Op, cid: int, at: str) -> None:
    """The non-block writes of one kept op."""
    match op:
        case ops.SubjectSet(name, label, active):
            conn.execute(
                "insert into subjects (name, label, ord, active)"
                " values (?, ?, (select coalesce(max(ord), -1) + 1 from subjects), ?)"
                " on conflict (name) do update set label = excluded.label,"
                " active = excluded.active",
                (name, label, active),
            )
        case ops.KindSet(name, active):
            conn.execute(
                "insert into kinds (name, active) values (?, ?)"
                " on conflict (name) do update set active = excluded.active",
                (name, active),
            )
        case ops.CapSet(cap):
            conn.execute("update settings set value = ? where key = 'session_cap'", (str(cap),))
        case ops.Report(d, subject, kind, sessions, note):
            day = schedule.read_plan(conn, schedule.week_start(d))[d]
            conn.execute(
                "insert into progress (date, subject, kind, sessions, status, note, actor,"
                " change_id, created_at) values (?, ?, ?, ?, ?, ?, 'you', ?, ?)",
                (
                    d.isoformat(),
                    subject,
                    kind,
                    sessions,
                    _status(day, op),
                    note,
                    cid,
                    at,
                ),
            )
        case ops.Withdraw(qid):
            conn.execute("update messages set state = 'withdrawn' where id = ?", (qid,))


def keep(conn: sqlite3.Connection, proposal_id: int, *, now: datetime, today: date) -> int:
    """Apply the waiting proposal exactly as shown, in one change. Returns the change id."""
    p, _, _ = _current(conn, proposal_id, today)  # refuses before anything is written
    dates = ops.touched_dates(p.ops)
    weekdays = ops.touched_weekdays(p.ops)
    mondays = {schedule.week_start(d) for d in dates}
    if any(isinstance(op, _META) for op in p.ops):
        mondays.update(schedule.due_mondays(conn, today))  # past and current weeks keep their plan
    schedule.ensure_pinned(conn, sorted(mondays), now)
    next_monday = schedule.week_start(today) + timedelta(days=7)
    effective = min(dates) if dates else (next_monday if weekdays else None)
    at = now.isoformat(timespec="seconds")
    with ledger.change(
        conn,
        actor="you",
        kind="proposal",
        now=now,
        reason=p.rationale,
        proposal_id=p.id,
        effective=effective,
    ) as cid:
        p, plan, tmpl = _current(conn, proposal_id, today)
        schedule.write_days(conn, {d: plan[d] for d in dates})
        schedule.write_template(conn, {w: tmpl[w] for w in weekdays})
        for op in p.ops:
            _write(conn, op, cid, at)
        for d in p.dispositions:
            if d.verdict == "defer":  # stays pending, which the mentor already sees
                continue
            conn.execute(
                "update messages set state = ? where id = ?", (_RESOLVED[d.verdict], d.request_id)
            )
            body = {**asdict(d), "change_id": cid}  # request_id, verdict, note
            _outcome(conn, d.request_id, body, _verdict_text(d), at)
        conn.execute(
            "update proposals set state = 'kept', decided_at = ?, change_id = ? where id = ?",
            (at, cid, p.id),
        )
    return cid


def discard(conn: sqlite3.Connection, proposal_id: int, *, now: datetime) -> None:
    """The waiting proposal is dropped; its requests stay pending."""
    conn.execute(
        "update proposals set state = 'discarded', decided_at = ?"
        " where id = ? and state = 'waiting'",
        (now.isoformat(timespec="seconds"), proposal_id),
    )


def _where(tbl: str) -> str:
    return " and ".join(f"{c} = ?" for c in _KEYS[tbl])


def _row(conn: sqlite3.Connection, tbl: str, key: tuple) -> dict | None:
    cur = conn.execute(f"select * from {tbl} where {_where(tbl)}", key)
    row = cur.fetchone()
    return dict(zip([c[0] for c in cur.description], row, strict=True)) if row else None


def _restore(
    conn: sqlite3.Connection, tbl: str, key: tuple, old: dict | None, new: dict | None
) -> None:
    """Put back one row as it was; never REPLACE, which would delete and re-insert."""
    if old is None:
        conn.execute(f"delete from {tbl} where {_where(tbl)}", key)
    elif tbl == "messages":  # the other columns are frozen
        conn.execute("update messages set state = ? where id = ?", (old["state"], old["id"]))
    elif new is not None:  # the row is there, as the change left it
        cols = [c for c in old if c not in _KEYS[tbl]]
        sets = ", ".join(f"{c} = ?" for c in cols)
        conn.execute(
            f"update {tbl} set {sets} where {_where(tbl)}", (*(old[c] for c in cols), *key)
        )
    else:
        marks = ", ".join("?" * len(old))
        conn.execute(f"insert into {tbl} ({', '.join(old)}) values ({marks})", tuple(old.values()))


def _undo_target(conn: sqlite3.Connection, undo: int | None = None) -> int | None:
    """The newest kept proposal's change not yet undone; the undo change `undo` doesn't count."""
    return conn.execute(
        "select max(id) from changes where kind = 'proposal' and id not in"
        " (select undoes from changes where undoes is not null and id is not ?)",
        (undo,),
    ).fetchone()[0]


def undo_last(conn: sqlite3.Connection, *, now: datetime, today: date) -> int | None:
    """Undo the newest kept proposal not yet undone, as a compensating change."""
    target = _undo_target(conn)
    if target is None:
        return None
    # Per row: the old of its first history row and the new of its last.
    net: dict[tuple[str, tuple], tuple[dict | None, dict | None]] = {}
    for h in ledger.history(conn, change_id=target):
        if h.tbl in _KEYS:
            row = h.old or h.new
            k = (h.tbl, tuple(row[c] for c in _KEYS[h.tbl]))
            net[k] = (net.get(k, (h.old,))[0], h.new)
    if any(tbl in ("template_blocks", "subjects", "kinds", "settings") for tbl, _ in net):
        schedule.ensure_pinned(conn, schedule.due_mondays(conn, today), now)  # as keep does
    at = now.isoformat(timespec="seconds")
    try:
        with ledger.change(
            conn, actor="you", kind="undo", now=now, undoes=target, reason=f"undo #{target}"
        ) as cid:
            if _undo_target(conn, cid) != target:  # a keep landed since
                raise Stale(UNDO_CHANGED)
            if any(tbl == "plan_blocks" and key[0] < today.isoformat() for tbl, key in net):
                raise OpError("past days keep their plan")
            if any(_row(conn, tbl, key) != new for (tbl, key), (_, new) in net.items()):
                raise Stale(UNDO_CHANGED)
            for (tbl, key), (old, new) in reversed(net.items()):
                _restore(conn, tbl, key, old, new)
            conn.execute(
                "insert into progress (date, subject, kind, sessions, status, note, actor,"
                " replaces, change_id, created_at) select date, subject, kind, 0, 'done',"
                " 'undone', 'you', id, ?, ? from progress where change_id = ? order by id",
                (cid, at, target),
            )
            outcomes = conn.execute(
                "select json_extract(body, '$.request_id') from messages"
                " where kind = 'outcome' and json_extract(body, '$.change_id') = ? order by id",
                (target,),
            ).fetchall()
            for (rid,) in outcomes:
                body = {"request_id": rid, "verdict": "reversed", "change_id": cid}
                _outcome(conn, rid, body, f"request #{rid}: reversed", at)
    # Another undo of it landed first (changes.undoes is unique), or something since depends on
    # a row it would restore, e.g. a message on a subject.
    except sqlite3.IntegrityError:
        raise Stale(UNDO_CHANGED) from None
    return cid
