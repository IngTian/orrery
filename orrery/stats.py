"""Stats built on the board, adding what it lacks: multi-week rates, the Report's days, range
and best week, rhythm, and a day's sessions and changes. Nothing here is stored."""

import json
import sqlite3
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache

from orrery import board, channels, schedule

MIN_TIMED = 5  # timed reports needed before rhythm states sitting times


@dataclass(frozen=True)
class Rhythm:
    n: int  # timed reports in range
    start_from: time | None  # the middle half of sitting start times
    start_to: time | None
    median_minutes: int | None


@dataclass(frozen=True)
class Summary:
    done: int  # capped per block, as the board's
    planned: int
    study_days: int  # days with planned blocks
    longest_run: int  # of study days with every planned session done, rest days skipped
    extras: int  # sessions past the plan or off it


@dataclass(frozen=True)
class SessionRow:
    subject: str
    kind: str
    sessions: int
    status: str
    started_at: datetime | None
    ended_at: datetime | None
    note: str


def _version(conn: sqlite3.Connection) -> tuple[int, int]:
    """Moves on with another connection's commit (data_version) or this one's write."""
    return conn.execute("pragma data_version").fetchone()[0], conn.total_changes


# ponytail: holds up to 128 boards and their (closed) connections, a year's Report and the
# rest of one reload; a per-reload dict if that grows
@lru_cache(maxsize=128)
def _week(conn: sqlite3.Connection, monday: date, today: date, _v: tuple[int, int]):
    return board.build(conn, monday, today)


def week(conn: sqlite3.Connection, monday: date, today: date) -> board.WeekBoard:
    """board.build, cached per (connection, week, data version), so one reload's views share it."""
    return _week(conn, monday, today, _version(conn))


def _days(conn: sqlite3.Connection, start: date, end: date) -> list[date]:
    """start to end inclusive, none before starts."""
    first = max(start, schedule.starts(conn))
    return [first + timedelta(days=i) for i in range((end - first).days + 1)]


def week_rates(
    conn: sqlite3.Connection, monday: date, weeks: int, today: date
) -> dict[str, list[tuple[date, int, int]]]:
    """Per subject planned in the window, (monday, done, planned to date) for each of the weeks
    ending with monday's, oldest first. Done is the board's, capped per cell, so extras never
    count. Weeks before starts' are left out, and days before starts count nothing."""
    starts = schedule.starts(conn)
    mondays = [monday - timedelta(weeks=i) for i in reversed(range(weeks))]
    mondays = [m for m in mondays if m >= schedule.week_start(starts)]
    totals: dict[tuple[str, date], tuple[int, int]] = {}
    planned: set[str] = set()
    for m in mondays:
        for day in week(conn, m, today).days:
            for c in day.cells:
                if c.sessions == 0 or day.date < starts:
                    continue
                planned.add(c.subject)
                if day.date <= today:
                    done, total = totals.get((c.subject, m), (0, 0))
                    totals[c.subject, m] = (done + c.done, total + c.sessions)
    return {
        s.name: [(m, *totals.get((s.name, m), (0, 0))) for m in mondays]
        for s in schedule.all_subjects(conn)
        if s.name in planned
    }


def _boards(conn: sqlite3.Connection, start: date, end: date, today: date) -> list[board.DayBoard]:
    """The days from start to end counted to date: none before starts, none after today."""
    days = _days(conn, start, min(end, today))
    got = {
        x.date: x for m in {schedule.week_start(d) for d in days} for x in week(conn, m, today).days
    }
    return [got[d] for d in days]


def _planned(day: board.DayBoard) -> dict[str, tuple[int, int]]:
    """Per subject with sessions planned on day, (done capped per block, planned)."""
    out: dict[str, tuple[int, int]] = {}
    for c in day.cells:
        if c.sessions:
            done, planned = out.get(c.subject, (0, 0))
            out[c.subject] = (done + c.done, planned + c.sessions)
    return out


def daily(
    conn: sqlite3.Connection, start: date, end: date, today: date
) -> dict[date, dict[str, tuple[int, int]]]:
    """Per day from start to end with blocks planned, counted to date (none before starts,
    none after today): per subject planned that day, (done capped per block, planned).
    Extras never count."""
    return {day.date: got for day in _boards(conn, start, end, today) if (got := _planned(day))}


def range_summary(
    conn: sqlite3.Connection, start: date, end: date, today: date
) -> dict[str, Summary]:
    """all, then each subject with blocks planned from start to end by ord: its sessions done
    (capped per block) of planned, its study days, the longest run of them with every planned
    session done, and the sessions past or off the plan. Counted to date, from starts."""
    boards = _boards(conn, start, end, today)
    days = [got for day in boards if (got := _planned(day))]
    extras = Counter()
    for day in boards:
        for e in day.extras:
            extras[e.subject] += e.done
    shown = {s for got in days for s in got}
    out = {}
    for key in ["all", *(s.name for s in schedule.all_subjects(conn) if s.name in shown)]:
        cells = [
            tuple(map(sum, zip(*got.values(), strict=True))) if key == "all" else got[key]
            for got in days
            if key == "all" or key in got
        ]
        run = longest = 0
        for done, planned in cells:  # done is capped per block: all done is every block done
            run = run + 1 if done >= planned else 0
            longest = max(longest, run)
        n = sum(extras.values()) if key == "all" else extras[key]
        out[key] = Summary(
            sum(d for d, _ in cells), sum(p for _, p in cells), len(cells), longest, n
        )
    return out


def best_week(
    conn: sqlite3.Connection, start: date, end: date, today: date
) -> tuple[date, int, int] | None:
    """(monday, done, planned) of the week from start to end with the highest share of its
    sessions planned to date done, among those with any; a tie goes to the later week."""
    weeks: dict[date, tuple[int, int]] = {}
    for d, got in daily(conn, start, end, today).items():
        m = schedule.week_start(d)
        done, planned = weeks.get(m, (0, 0))
        weeks[m] = (
            done + sum(x for x, _ in got.values()),
            planned + sum(p for _, p in got.values()),
        )
    if not weeks:
        return None
    m = max(weeks, key=lambda m: (weeks[m][0] / weeks[m][1], m))
    return m, *weeks[m]


_PROGRESS = (
    "select date, subject, kind, sessions, status, started_at, ended_at, note"
    f" from progress where {channels._EFFECTIVE} and date between ? and ? order by date, id"
)


def _progress(conn: sqlite3.Connection, start: date, end: date) -> list[tuple]:
    """Effective progress rows dated start to end. An undo's retraction rows are among them,
    with 0 sessions and no times, so day_sessions and rhythm drop them."""
    return conn.execute(_PROGRESS, (start.isoformat(), end.isoformat())).fetchall()


def _at(s: str | None) -> datetime | None:
    """A reported time as wall clock: a stray offset is dropped, so any two compare."""
    return datetime.fromisoformat(s).replace(tzinfo=None) if s else None


def rhythm(conn: sqlite3.Connection, start: date, end: date, boundary: int) -> Rhythm:
    """Facts from start to end: when timed sittings start and how long they run, from
    MIN_TIMED timed reports on."""
    days = _days(conn, start, end)
    rows = _progress(conn, days[0], days[-1]) if days else []
    timed = []
    for *_, began, ended, _note in rows:
        if began and ended:
            s, e = _at(began), _at(ended)
            if e > s:
                timed.append((s, e))
    start_from = start_to = median = None
    if len(timed) >= MIN_TIMED:
        # minutes after the day boundary, so a sitting past midnight sorts after the evening
        mins = [(s.hour * 60 + s.minute - boundary * 60) % 1440 for s, _ in timed]
        q1, _, q3 = statistics.quantiles(mins, n=4, method="inclusive")
        start_from, start_to = (_clock(round(q) + boundary * 60) for q in (q1, q3))
        median = round(statistics.median((e - s).total_seconds() / 60 for s, e in timed))
    return Rhythm(len(timed), start_from, start_to, median)


def _clock(minutes: int) -> time:
    minutes %= 1440
    return time(minutes // 60, minutes % 60)


def day_sessions(conn: sqlite3.Connection, d: date) -> list[SessionRow]:
    """The day's reported sittings, undo rows left out; a 0-session partial stays, for its
    note."""
    return [
        SessionRow(s, k, n, st, _at(a), _at(b), " ".join(note.split()))  # a line each, as drawn
        for _, s, k, n, st, a, b, note in _progress(conn, d, d)
        if n or st == "partial"
    ]


def day_changes(conn: sqlite3.Connection, d: date, boundary: int) -> list[tuple[datetime, str]]:
    """Proposals kept, undos and memory notes on day d (by the day boundary), one line each."""
    rows = conn.execute(
        "select c.at, c.kind, c.reason, c.proposal_id, u.proposal_id, h.op, h.old, h.new"
        " from changes c left join changes u on u.id = c.undoes"
        " left join history h on h.change_id = c.id and h.tbl = 'memory'"
        " where c.kind in ('proposal', 'undo', 'memory') and date(c.at, ?) = ?"
        " order by c.id, h.id",
        (f"-{boundary} hours", d.isoformat()),
    )
    out = []
    for at, kind, reason, pid, undone, op, old, new in rows:
        if kind == "proposal":
            line = f"kept proposal #{pid}" + (f" · {reason}" if reason else "")
        elif kind == "undo":
            line = f"undid proposal #{undone}"
        elif op is None:  # a memory change that wrote no memory row
            continue
        else:  # rationale and memory text passed the verdict gate when they were written
            note = json.loads(new or old)["text"]
            line = f"noted: {note}" if op == "insert" else f"note dropped: {note}"
        out.append((datetime.fromisoformat(at), " ".join(line.split())))
    return out
