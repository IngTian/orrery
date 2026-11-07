"""Schedule operations for orrery: reading and writing plans, templates, and weeks."""

import sqlite3
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta

from orrery import ledger
from orrery.model import Block, Plan, Subject, Template


def today(now: datetime, boundary_hour: int) -> date:
    """Calculate today based on the day boundary hour."""
    return (now - timedelta(hours=boundary_hour)).date()


def week_start(d: date) -> date:
    """Return the Monday on or before date d."""
    return d - timedelta(days=d.weekday())


def week_dates(monday: date) -> list[date]:
    """Return all 7 dates in the week starting from monday."""
    return [monday + timedelta(days=i) for i in range(7)]


def seed(
    conn: sqlite3.Connection,
    *,
    subjects: Sequence[tuple[str, str]],
    kinds: Sequence[str],
    cap: int,
    starts: date,
    now: datetime,
) -> int:
    """Initialize subjects, kinds, and settings. Returns the change id."""
    with ledger.change(conn, actor="orrery", kind="setup", now=now) as cid:
        # Insert subjects with ord = index
        for i, (name, label) in enumerate(subjects):
            conn.execute(
                "insert into subjects (name, label, ord, active) values (?, ?, ?, 1)",
                (name, label, i),
            )

        # Insert kinds
        for kind_name in kinds:
            conn.execute("insert into kinds (name, active) values (?, 1)", (kind_name,))

        # Insert settings
        conn.execute("insert into settings (key, value) values ('session_cap', ?)", (str(cap),))
        conn.execute(
            "insert into settings (key, value) values ('starts', ?)", (starts.isoformat(),)
        )

    return cid


def subjects(conn: sqlite3.Connection) -> list[Subject]:
    """Return active subjects ordered by ord."""
    return [s for s in all_subjects(conn) if s.active]


def all_subjects(conn: sqlite3.Connection) -> list[Subject]:
    """Every subject, active or not, ordered by ord."""
    rows = conn.execute("select name, label, ord, active from subjects order by ord, name")
    return [Subject(name, label, o, bool(a)) for name, label, o, a in rows]


def kinds(conn: sqlite3.Connection) -> frozenset[str]:
    """Return active kinds."""
    rows = conn.execute("select name from kinds where active = 1").fetchall()
    return frozenset(name for (name,) in rows)


def cap(conn: sqlite3.Connection) -> int:
    """Return the session cap."""
    row = conn.execute("select value from settings where key = 'session_cap'").fetchone()
    return int(row[0])


def starts(conn: sqlite3.Connection) -> date:
    """Return the start date."""
    row = conn.execute("select value from settings where key = 'starts'").fetchone()
    return date.fromisoformat(row[0])


def _read_blocks(conn: sqlite3.Connection, table: str, where: str, params: tuple) -> dict:
    """Read blocks from a table (template_blocks, baseline_blocks, or plan_blocks)."""
    if table == "template_blocks":
        key_col = "weekday"
    else:
        key_col = "date"

    rows = conn.execute(
        f"""
        select {key_col}, part, subject, kind, sessions
        from {table}
        where {where}
        order by {key_col},
                 case part when 'warmup' then 0 when 'main' then 1 else 2 end,
                 ord
        """,
        params,
    ).fetchall()

    result = {}
    for row in rows:
        key = row[0]
        if table != "template_blocks":
            key = date.fromisoformat(key)
        block = Block(row[1], row[2], row[3], row[4])
        if key not in result:
            result[key] = []
        result[key].append(block)

    # Convert lists to tuples
    return {k: tuple(v) for k, v in result.items()}


def read_template(conn: sqlite3.Connection) -> Template:
    """Read the template with all 7 weekdays present."""
    blocks = _read_blocks(conn, "template_blocks", "1 = 1", ())
    # Ensure all 7 weekdays are present
    return {i: blocks.get(i, ()) for i in range(7)}


def is_pinned(conn: sqlite3.Connection, monday: date) -> bool:
    """Check if a week is pinned."""
    row = conn.execute("select 1 from weeks where monday = ?", (monday.isoformat(),)).fetchone()
    return row is not None


def read_plan(conn: sqlite3.Connection, monday: date) -> Plan:
    """Read the plan for a week. Returns template if unpinned. Never writes."""
    if not is_pinned(conn, monday):
        # Return template for this week
        tmpl = read_template(conn)
        dates = week_dates(monday)
        return {d: tmpl[d.weekday()] for d in dates}

    # Read from plan_blocks
    dates = week_dates(monday)
    blocks = _read_blocks(conn, "plan_blocks", "monday = ?", (monday.isoformat(),))
    # Ensure all 7 dates are present
    return {d: blocks.get(d, ()) for d in dates}


def read_baseline(conn: sqlite3.Connection, monday: date) -> Plan:
    """Read the baseline for a week. Equal to read_plan when unpinned."""
    if not is_pinned(conn, monday):
        return read_plan(conn, monday)

    # Read from baseline_blocks
    dates = week_dates(monday)
    blocks = _read_blocks(conn, "baseline_blocks", "monday = ?", (monday.isoformat(),))
    # Ensure all 7 dates are present
    return {d: blocks.get(d, ()) for d in dates}


def pin(conn: sqlite3.Connection, monday: date, now: datetime) -> bool:
    """Pin a week by copying template to baseline and plan. Returns False if already pinned."""
    if is_pinned(conn, monday):
        return False

    # Insert weeks row
    conn.execute(
        "insert into weeks (monday, pinned_at) values (?, ?)",
        (monday.isoformat(), now.isoformat(timespec="seconds")),
    )

    # Copy template to baseline_blocks and plan_blocks
    tmpl = read_template(conn)
    dates = week_dates(monday)

    for d in dates:
        blocks = tmpl[d.weekday()]
        for ord_idx, block in enumerate(blocks):
            # Insert into baseline_blocks
            conn.execute(
                """
                insert into baseline_blocks (date, monday, part, ord, subject, kind, sessions)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    d.isoformat(),
                    monday.isoformat(),
                    block.part,
                    ord_idx,
                    block.subject,
                    block.kind,
                    block.sessions,
                ),
            )
            # Insert into plan_blocks
            conn.execute(
                """
                insert into plan_blocks (date, monday, part, ord, subject, kind, sessions)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    d.isoformat(),
                    monday.isoformat(),
                    block.part,
                    ord_idx,
                    block.subject,
                    block.kind,
                    block.sessions,
                ),
            )

    return True


def ensure_pinned(conn: sqlite3.Connection, mondays: Iterable[date], now: datetime) -> int | None:
    """Ensure weeks are pinned. Opens one change if needed. Returns change id or None."""
    mondays_list = list(mondays)
    unpinned = [m for m in mondays_list if not is_pinned(conn, m)]

    if not unpinned:
        return None

    # Open one change for all unpinned weeks
    first_monday = unpinned[0]
    reason = f"week of {first_monday} pinned"
    with ledger.change(conn, actor="orrery", kind="pin", now=now, reason=reason) as cid:
        for monday in unpinned:
            pin(conn, monday, now)

    return cid


def due_mondays(conn: sqlite3.Connection, today: date) -> list[date]:
    """Return every Monday from week_start(starts) to week_start(today) inclusive."""
    start_date = starts(conn)
    start_monday = week_start(start_date)
    end_monday = week_start(today)

    mondays = []
    current = start_monday
    while current <= end_monday:
        mondays.append(current)
        current += timedelta(days=7)

    return mondays


def write_days(conn: sqlite3.Connection, plan: Plan) -> None:
    """Replace plan_blocks rows for the given dates. Raises ValueError if any week is unpinned."""
    if not plan:
        return

    # Check that all dates are pinned
    mondays_to_check = {week_start(d) for d in plan.keys()}
    for monday in mondays_to_check:
        if not is_pinned(conn, monday):
            raise ValueError(f"week {monday} is not pinned")

    # Delete existing rows for these dates
    dates_to_delete = list(plan.keys())
    placeholders = ", ".join("?" * len(dates_to_delete))
    conn.execute(
        f"delete from plan_blocks where date in ({placeholders})",
        tuple(d.isoformat() for d in dates_to_delete),
    )

    # Insert new rows
    for d, blocks in plan.items():
        monday = week_start(d)
        for ord_idx, block in enumerate(blocks):
            conn.execute(
                """
                insert into plan_blocks (date, monday, part, ord, subject, kind, sessions)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    d.isoformat(),
                    monday.isoformat(),
                    block.part,
                    ord_idx,
                    block.subject,
                    block.kind,
                    block.sessions,
                ),
            )


def write_template(conn: sqlite3.Connection, tmpl: Template) -> None:
    """Replace template_blocks rows for the given weekdays."""
    if not tmpl:
        return

    # Delete existing rows for these weekdays
    weekdays_to_delete = list(tmpl.keys())
    placeholders = ", ".join("?" * len(weekdays_to_delete))
    conn.execute(
        f"delete from template_blocks where weekday in ({placeholders})",
        tuple(weekdays_to_delete),
    )

    # Insert new rows
    for weekday, blocks in tmpl.items():
        for ord_idx, block in enumerate(blocks):
            conn.execute(
                """
                insert into template_blocks (weekday, part, ord, subject, kind, sessions)
                values (?, ?, ?, ?, ?, ?)
                """,
                (weekday, block.part, ord_idx, block.subject, block.kind, block.sessions),
            )
