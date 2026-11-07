"""The board: derived week view of strikes, additions, marks, topics, and deadlines."""

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date

from orrery import channels, schedule
from orrery.model import PARTS, Mark, Part, Plan


@dataclass(frozen=True)
class Cell:
    part: Part
    subject: str
    kind: str
    sessions: int
    struck: int
    added: int
    done: int  # every reported session, capped at planned
    partial: int  # of done, those from partial reports: done-status sessions fill a cell first
    mark: Mark
    topic: str | None
    moved_to: date | None


@dataclass(frozen=True)
class Extra:
    subject: str
    kind: str
    done: int


@dataclass(frozen=True)
class DayBoard:
    date: date
    cells: tuple[Cell, ...]
    extras: tuple[Extra, ...]
    deadlines: tuple[str, ...]


@dataclass(frozen=True)
class WeekBoard:
    monday: date
    today: date
    days: tuple[DayBoard, ...]


def mark_for(done: int, planned: int) -> Mark:
    """Compute a mark from done and planned sessions."""
    if done >= planned > 0:
        return "full"
    elif 0 < done < planned:
        return "half"
    else:
        return "none"


def _effective_agenda(
    conn: sqlite3.Connection, subject: str, d: date
) -> list[tuple[str, str | None, bool]]:
    """Return effective agenda items for (subject, date) as (item, kind, deadline)."""
    # The effective agenda for (subject, date) is the rows of the newest message
    # that has rows for that (subject, date)
    row = conn.execute(
        "select message_id from agenda where subject = ? and date = ?"
        " order by message_id desc limit 1",
        (subject, d.isoformat()),
    ).fetchone()

    if row is None:
        return []

    msg_id = row[0]

    rows = conn.execute(
        "select item, kind, deadline from agenda where message_id = ? and subject = ? and date = ?",
        (msg_id, subject, d.isoformat()),
    ).fetchall()

    # one line each, as every view draws them
    return [(" ".join(item.split()), kind, bool(deadline)) for item, kind, deadline in rows]


def _moved_to(conn: sqlite3.Connection, d: date, subject: str, kind: str) -> date | None:
    """Find the 'to' date from the newest kept proposal with this move, excluding undone changes."""
    # Find the newest kept proposal with a Move(date, subject, kind, to)
    # Skip proposals whose change_id appears in changes.undoes
    rows = conn.execute(
        """
        select ops, change_id from proposals
        where state = 'kept'
        and change_id not in (select undoes from changes where undoes is not null)
        order by id desc
        """,
    ).fetchall()

    for ops_json, _change_id in rows:
        ops_list = json.loads(ops_json)
        for op in ops_list:
            if (
                op.get("op") == "move"
                and op.get("date") == d.isoformat()
                and op.get("subject") == subject
                and op.get("kind") == kind
            ):
                return date.fromisoformat(op["to"])

    return None


def build(
    conn: sqlite3.Connection, monday: date, today: date, *, plan: Plan | None = None
) -> WeekBoard:
    """Build the derived week view."""
    dates = schedule.week_dates(monday)

    # Read current plan and baseline
    if plan is None:
        current = schedule.read_plan(conn, monday)
    else:
        # Use the provided plan for those dates
        current = plan

    baseline = schedule.read_baseline(conn, monday)

    # Read done counts
    done_counts = channels.done_counts(conn, dates[0], dates[-1])
    partial_counts = channels.done_counts(conn, dates[0], dates[-1], "partial")

    # Build day boards
    days = []
    for d in dates:
        cells: list[Cell] = []
        extras: list[Extra] = []
        deadlines: list[str] = []

        cur_blocks = current.get(d, ())
        base_blocks = baseline.get(d, ())

        # Calculate struck and added per (subject, kind)
        cur_totals: Counter[tuple[str, str]] = Counter()
        base_totals: Counter[tuple[str, str]] = Counter()

        for b in cur_blocks:
            cur_totals[(b.subject, b.kind)] += b.sessions
        for b in base_blocks:
            base_totals[(b.subject, b.kind)] += b.sessions

        struck_map: dict[tuple[str, str], int] = {}
        added_map: dict[tuple[str, str], int] = {}

        all_keys = dict.fromkeys([*cur_totals, *base_totals])  # plan order, then baseline order
        for key in all_keys:
            base = base_totals.get(key, 0)
            cur = cur_totals.get(key, 0)
            struck_map[key] = max(0, base - cur)
            added_map[key] = max(0, cur - base)

        # Build cells by part
        struck_only_cells: list[Cell] = []

        for part in PARTS:
            # Current cells in this part
            part_cur_blocks = [b for b in cur_blocks if b.part == part]

            for i, b in enumerate(part_cur_blocks):
                key = (b.subject, b.kind)

                # Struck and added go on the first current cell with this (subject, kind)
                first_idx = next(
                    (j for j, bb in enumerate(part_cur_blocks) if (bb.subject, bb.kind) == key),
                    None,
                )
                is_first = i == first_idx

                struck = struck_map[key] if is_first else 0
                added = added_map[key] if is_first else 0

                # Clear struck/added for subsequent cells
                if is_first:
                    struck_map[key] = 0
                    added_map[key] = 0

                # Done: will be filled later
                done = 0

                # Mark: will be filled later
                mark: Mark = "none"

                # Topic: first effective agenda item with deadline=0 and kind matching or NULL
                agenda_items = _effective_agenda(conn, b.subject, d)
                topic = next(
                    (item for item, k, dl in agenda_items if not dl and (k == b.kind or k is None)),
                    None,
                )

                # moved_to: only when struck > 0
                moved_to = _moved_to(conn, d, b.subject, b.kind) if struck > 0 else None

                cells.append(
                    Cell(
                        part=b.part,
                        subject=b.subject,
                        kind=b.kind,
                        sessions=b.sessions,
                        struck=struck,
                        added=added,
                        done=done,
                        partial=0,
                        mark=mark,
                        topic=topic,
                        moved_to=moved_to,
                    )
                )

            # Struck-only cells: if struck > 0 and no current cell
            for key, struck in list(struck_map.items()):
                if struck > 0:
                    subject, kind = key
                    # Find the first baseline block with this (subject, kind) in this part
                    first_base = next(
                        (
                            b
                            for b in base_blocks
                            if b.subject == subject and b.kind == kind and b.part == part
                        ),
                        None,
                    )
                    if first_base:
                        moved_to = _moved_to(conn, d, subject, kind)
                        struck_only_cells.append(
                            Cell(
                                part=first_base.part,
                                subject=subject,
                                kind=kind,
                                sessions=0,
                                struck=struck,
                                added=0,
                                done=0,
                                partial=0,
                                mark="none",
                                topic=None,
                                moved_to=moved_to,
                            )
                        )
                        struck_map[key] = 0

        # Add struck-only cells to the appropriate positions
        # They go after current cells in each part, in baseline order
        final_cells: list[Cell] = []
        for part in PARTS:
            part_cur = [c for c in cells if c.part == part]
            part_struck = [c for c in struck_only_cells if c.part == part]
            final_cells.extend(part_cur)
            final_cells.extend(part_struck)

        cells = final_cells

        # Distribute done
        done_surplus: dict[tuple[str, str], int] = {}

        for key, total_done in done_counts.items():
            if key[0] != d:
                continue
            subject, kind = key[1], key[2]

            # Find cells with this (subject, kind) and sessions > 0
            matching_cells = [
                (i, c)
                for i, c in enumerate(cells)
                if c.subject == subject and c.kind == kind and c.sessions > 0
            ]

            remaining = total_done
            done_status = total_done - partial_counts[key]  # these fill the cells first

            for i, c in matching_cells:
                allocated = min(remaining, c.sessions)
                full = min(done_status, allocated)
                cells[i] = replace(c, done=allocated, partial=allocated - full)
                remaining -= allocated
                done_status -= full

            if remaining > 0:
                done_surplus[(subject, kind)] = remaining

        # Update marks
        for i, c in enumerate(cells):
            cells[i] = replace(c, mark=mark_for(c.done, c.sessions))

        # Extras: surplus first, then unplanned, each in day's plan order
        # Surplus extras, struck-only cells' done included
        for c in cells:
            key = (c.subject, c.kind)
            if key in done_surplus:
                extras.append(Extra(c.subject, c.kind, done_surplus.pop(key)))

        # Unplanned extras
        for key, total_done in done_counts.items():
            if key[0] != d:
                continue
            subject, kind = key[1], key[2]

            # Check if there's no current and no baseline block
            has_current = any(b.subject == subject and b.kind == kind for b in cur_blocks)
            has_baseline = any(b.subject == subject and b.kind == kind for b in base_blocks)

            if not has_current and not has_baseline and total_done:  # 0 after an undo or report
                extras.append(Extra(subject, kind, total_done))

        # Deadlines
        subjects_list = schedule.subjects(conn)
        for s in subjects_list:
            agenda_items = _effective_agenda(conn, s.name, d)
            for item, _kind, deadline in agenda_items:
                if deadline:
                    deadlines.append(f"{s.name}: {item}")

        days.append(
            DayBoard(date=d, cells=tuple(cells), extras=tuple(extras), deadlines=tuple(deadlines))
        )

    return WeekBoard(monday=monday, today=today, days=tuple(days))
