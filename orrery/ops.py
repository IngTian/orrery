"""Pure schedule edit operations for orrery."""

import json
import re
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import date

from orrery import text
from orrery.model import PARTS, Block, Part, Plan, Template


class OpError(ValueError):
    """Raised when an operation is invalid."""


@dataclass(frozen=True)
class Move:
    date: date
    subject: str
    kind: str
    to: date
    sessions: int | None = None
    for_request: int | None = None


@dataclass(frozen=True)
class Add:
    date: date
    part: Part
    subject: str
    kind: str
    sessions: int
    for_request: int | None = None


@dataclass(frozen=True)
class Remove:
    date: date
    subject: str
    kind: str
    sessions: int | None = None
    for_request: int | None = None


@dataclass(frozen=True)
class TemplateSet:
    weekday: int
    part: Part
    subject: str
    kind: str
    sessions: int
    for_request: int | None = None


@dataclass(frozen=True)
class Report:
    date: date
    subject: str
    kind: str
    sessions: int
    note: str = ""


@dataclass(frozen=True)
class Withdraw:
    question_id: int


@dataclass(frozen=True)
class SubjectSet:
    name: str
    label: str
    active: bool = True


@dataclass(frozen=True)
class KindSet:
    name: str
    active: bool = True


@dataclass(frozen=True)
class CapSet:
    cap: int


Op = Move | Add | Remove | TemplateSet | Report | Withdraw | SubjectSet | KindSet | CapSet


def apply_plan(
    plan: Plan,
    ops: Sequence[Op],
    *,
    today: date,
    done: Mapping[tuple[date, str, str], int],
    subjects: AbstractSet[str],
    kinds: AbstractSet[str],
    cap: int,
) -> Plan:
    """Apply Move, Add, and Remove ops to a plan. Returns a new plan."""
    result = dict(plan)

    for op in ops:
        # Here, not in parse, so a stored proposal still loads and its diff says why.
        if isinstance(op, Move | Add | Remove) and op.sessions is not None and op.sessions < 1:
            raise OpError("sessions must be at least 1")
        if isinstance(op, Move):
            _apply_move(result, op, today, done, subjects, kinds, cap)
        elif isinstance(op, Add):
            _apply_add(result, op, today, subjects, kinds, cap)
        elif isinstance(op, Remove):
            _apply_remove(result, op, today, done, subjects, kinds)

    return result


def _take_sessions(
    plan: Plan,
    d: date,
    subject: str,
    kind: str,
    sessions: int | None,
    done: Mapping[tuple[date, str, str], int],
    today: date,
    subjects: AbstractSet[str],
    kinds: AbstractSet[str],
) -> tuple[tuple[Block, ...], Part, int]:
    """Take sessions from a day. Returns (new_blocks, source_part, sessions_taken)."""
    # Check date in plan
    if d not in plan:
        raise OpError(f"{text.fmt_day(d)} is outside the weeks you can edit")

    # Check past days
    if d < today:
        raise OpError("past days keep their plan")

    # Check subject/kind
    if subject not in subjects:
        raise OpError(f"unknown subject {subject}")
    if kind not in kinds:
        raise OpError(f"unknown kind {kind}")

    # Find matching blocks
    blocks = list(plan[d])
    matches = [b for b in blocks if b.subject == subject and b.kind == kind]
    if not matches:
        raise OpError(f"no {subject} {kind} on {text.fmt_day(d)}")

    # Calculate movable sessions
    total = sum(b.sessions for b in matches)
    done_count = done.get((d, subject, kind), 0)
    movable = max(0, total - done_count)

    if movable == 0:
        raise OpError(f"nothing left to move on {text.fmt_day(d)}: {done_count} already done")

    # Determine sessions to take
    to_take = movable if sessions is None else sessions
    if to_take > movable:
        raise OpError(f"only {movable} of {subject} {kind} on {text.fmt_day(d)} not done yet")

    # Remove sessions from last matching block first
    remaining = to_take
    source_part = None
    new_blocks = []
    for b in reversed(blocks):
        if b.subject == subject and b.kind == kind and remaining > 0:
            if source_part is None:
                source_part = b.part
            take = min(remaining, b.sessions)
            remaining -= take
            if b.sessions > take:
                new_blocks.insert(0, Block(b.part, b.subject, b.kind, b.sessions - take))
        else:
            new_blocks.insert(0, b)

    return tuple(new_blocks), source_part, to_take


def _apply_move(
    plan: Plan,
    op: Move,
    today: date,
    done: Mapping[tuple[date, str, str], int],
    subjects: AbstractSet[str],
    kinds: AbstractSet[str],
    cap: int,
) -> None:
    """Apply a Move operation (mutates plan in-place)."""
    # Check destination in plan
    if op.to not in plan:
        raise OpError(f"{text.fmt_day(op.to)} is outside the weeks you can edit")

    # Check destination not past
    if op.to < today:
        raise OpError("past days keep their plan")

    # Check same day
    if op.date == op.to:
        raise OpError("that's the same day")

    # Take sessions from source
    new_blocks, source_part, to_move = _take_sessions(
        plan, op.date, op.subject, op.kind, op.sessions, done, today, subjects, kinds
    )
    plan[op.date] = new_blocks

    # Add sessions to target
    _add_to_day(plan, op.to, source_part, op.subject, op.kind, to_move, cap)


def _apply_add(
    plan: Plan,
    op: Add,
    today: date,
    subjects: AbstractSet[str],
    kinds: AbstractSet[str],
    cap: int,
) -> None:
    """Apply an Add operation (mutates plan in-place)."""
    # Check date in plan
    if op.date not in plan:
        raise OpError(f"{text.fmt_day(op.date)} is outside the weeks you can edit")

    # Check past days
    if op.date < today:
        raise OpError("past days keep their plan")

    # Check subject/kind
    if op.subject not in subjects:
        raise OpError(f"unknown subject {op.subject}")
    if op.kind not in kinds:
        raise OpError(f"unknown kind {op.kind}")

    # Add to target
    _add_to_day(plan, op.date, op.part, op.subject, op.kind, op.sessions, cap)


def _apply_remove(
    plan: Plan,
    op: Remove,
    today: date,
    done: Mapping[tuple[date, str, str], int],
    subjects: AbstractSet[str],
    kinds: AbstractSet[str],
) -> None:
    """Apply a Remove operation (mutates plan in-place)."""
    # Take sessions (discard source_part and count, we just remove)
    new_blocks, _, _ = _take_sessions(
        plan, op.date, op.subject, op.kind, op.sessions, done, today, subjects, kinds
    )
    plan[op.date] = new_blocks


def _add_to_day(
    plan: Plan, d: date, part: Part, subject: str, kind: str, sessions: int, cap: int
) -> None:
    """Add sessions to a day, merging or appending as needed (mutates plan)."""
    blocks = list(plan[d])

    # Try to merge with existing block
    for i, b in enumerate(blocks):
        if b.subject == subject and b.kind == kind:
            new_sessions = b.sessions + sessions
            if new_sessions > cap:
                raise OpError(
                    f"would make {new_sessions} {subject} {kind} on {text.fmt_day(d)}, "
                    f"over the cap of {cap}"
                )
            blocks[i] = Block(b.part, b.subject, b.kind, new_sessions)
            plan[d] = tuple(blocks)
            return

    # Append new block
    if sessions > cap:
        raise OpError(
            f"would make {sessions} {subject} {kind} on {text.fmt_day(d)}, over the cap of {cap}"
        )
    blocks.append(Block(part, subject, kind, sessions))

    # Re-order by PARTS (stable sort preserves original order within each part)
    blocks_with_idx = [(PARTS.index(b.part), i, b) for i, b in enumerate(blocks)]
    blocks_with_idx.sort()
    plan[d] = tuple(b for _, _, b in blocks_with_idx)


def apply_template(
    tmpl: Template,
    ops: Sequence[Op],
    *,
    subjects: AbstractSet[str],
    kinds: AbstractSet[str],
    cap: int,
) -> Template:
    """Apply TemplateSet ops to a template. Returns a new template."""
    result = dict(tmpl)

    for op in ops:
        if isinstance(op, TemplateSet):
            _apply_template_set(result, op, subjects, kinds, cap)

    return result


def _apply_template_set(
    tmpl: Template,
    op: TemplateSet,
    subjects: AbstractSet[str],
    kinds: AbstractSet[str],
    cap: int,
) -> None:
    """Apply a TemplateSet operation (mutates template in-place)."""
    # Validate weekday
    if not (0 <= op.weekday <= 6):
        raise OpError(f"weekday must be 0-6, got {op.weekday}")

    # Validate sessions
    if not (0 <= op.sessions <= cap):
        raise OpError(f"sessions must be 0-{cap}, got {op.sessions}")

    # Check subject/kind
    if op.subject not in subjects:
        raise OpError(f"unknown subject {op.subject}")
    if op.kind not in kinds:
        raise OpError(f"unknown kind {op.kind}")

    # Get blocks for this weekday
    blocks = list(tmpl.get(op.weekday, ()))

    # Find matching block
    match_idx = None
    for i, b in enumerate(blocks):
        if b.part == op.part and b.subject == op.subject and b.kind == op.kind:
            match_idx = i
            break

    # Remove if sessions=0
    if op.sessions == 0:
        if match_idx is None:
            day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            wkd = day_names[op.weekday]
            raise OpError(f"nothing to remove on every {wkd}")
        blocks.pop(match_idx)
    else:
        # Set or add
        if match_idx is not None:
            blocks[match_idx] = Block(op.part, op.subject, op.kind, op.sessions)
        else:
            blocks.append(Block(op.part, op.subject, op.kind, op.sessions))

    # Re-order by PARTS (stable sort preserves original order within each part)
    blocks_with_idx = [(PARTS.index(b.part), i, b) for i, b in enumerate(blocks)]
    blocks_with_idx.sort()
    tmpl[op.weekday] = tuple(b for _, _, b in blocks_with_idx)


def check_meta(
    ops: Sequence[Op],
    *,
    subjects: AbstractSet[str],
    kinds: AbstractSet[str],
    plan_and_template_max: int,
    used_subjects: AbstractSet[str],
    used_kinds: AbstractSet[str],
) -> None:
    """Validate SubjectSet, KindSet, and CapSet operations."""
    for op in ops:
        if isinstance(op, SubjectSet):
            # Validate name; "all" is the Report's every-subject pick (stats.range_summary)
            if not re.match(r"^[a-z][a-z0-9-]{0,31}$", op.name) or op.name == "all":
                raise OpError(f"bad name {op.name}")
            # Check if deactivating a used subject
            if not op.active and op.name in used_subjects:
                raise OpError(f"{op.name} is still planned; move or remove it first")
        elif isinstance(op, KindSet):
            # Validate name
            if not re.match(r"^[a-z][a-z0-9-]{0,31}$", op.name):
                raise OpError(f"bad name {op.name}")
            # Check if deactivating a used kind
            if not op.active and op.name in used_kinds:
                raise OpError(f"{op.name} is still planned; move or remove it first")
        elif isinstance(op, CapSet):
            # Validate range
            if not (1 <= op.cap <= 9):
                raise OpError("cap must be 1-9")
            # Check against existing blocks
            if op.cap < plan_and_template_max:
                raise OpError(f"a block already has {plan_and_template_max} sessions")


def touched_dates(ops: Sequence[Op]) -> set[date]:
    """Return all dates touched by Move, Add, and Remove ops."""
    dates = set()
    for op in ops:
        if isinstance(op, Move):
            dates.add(op.date)
            dates.add(op.to)
        elif isinstance(op, (Add, Remove)):
            dates.add(op.date)
    return dates


def touched_weekdays(ops: Sequence[Op]) -> set[int]:
    """Return all weekdays touched by TemplateSet ops."""
    weekdays = set()
    for op in ops:
        if isinstance(op, TemplateSet):
            weekdays.add(op.weekday)
    return weekdays


def to_json(ops: Sequence[Op]) -> str:
    """Serialize ops to JSON."""
    items = []
    for op in ops:
        item = _op_to_dict(op)
        items.append(item)
    return json.dumps(items)


def from_json(s: str) -> list[Op]:
    """Deserialize ops from JSON."""
    items = json.loads(s)
    return [parse(item) for item in items]


def _op_to_dict(op: Op) -> dict:
    """Convert an op to a dict for JSON serialization."""
    if isinstance(op, Move):
        d = {
            "op": "move",
            "date": op.date.isoformat(),
            "subject": op.subject,
            "kind": op.kind,
            "to": op.to.isoformat(),
        }
        if op.sessions is not None:
            d["sessions"] = op.sessions
        if op.for_request is not None:
            d["for_request"] = op.for_request
        return d
    elif isinstance(op, Add):
        d = {
            "op": "add",
            "date": op.date.isoformat(),
            "part": op.part,
            "subject": op.subject,
            "kind": op.kind,
            "sessions": op.sessions,
        }
        if op.for_request is not None:
            d["for_request"] = op.for_request
        return d
    elif isinstance(op, Remove):
        d = {
            "op": "remove",
            "date": op.date.isoformat(),
            "subject": op.subject,
            "kind": op.kind,
        }
        if op.sessions is not None:
            d["sessions"] = op.sessions
        if op.for_request is not None:
            d["for_request"] = op.for_request
        return d
    elif isinstance(op, TemplateSet):
        d = {
            "op": "template",
            "weekday": op.weekday,
            "part": op.part,
            "subject": op.subject,
            "kind": op.kind,
            "sessions": op.sessions,
        }
        if op.for_request is not None:
            d["for_request"] = op.for_request
        return d
    elif isinstance(op, Report):
        d = {
            "op": "report",
            "date": op.date.isoformat(),
            "subject": op.subject,
            "kind": op.kind,
            "sessions": op.sessions,
        }
        if op.note:
            d["note"] = op.note
        return d
    elif isinstance(op, Withdraw):
        return {"op": "withdraw", "question_id": op.question_id}
    elif isinstance(op, SubjectSet):
        d = {"op": "subject", "name": op.name, "label": op.label}
        if not op.active:
            d["active"] = op.active
        return d
    elif isinstance(op, KindSet):
        d = {"op": "kind", "name": op.name}
        if not op.active:
            d["active"] = op.active
        return d
    elif isinstance(op, CapSet):
        return {"op": "cap", "cap": op.cap}
    else:
        raise ValueError(f"unknown op type: {type(op)}")


def _req_str(raw: dict, name: str) -> str:
    """Extract a required string field."""
    try:
        val = raw[name]
    except KeyError:
        raise OpError(f"missing field '{name}'") from None
    if not isinstance(val, str):
        raise OpError(f"invalid field: {name} must be a string") from None
    return val


def _req_int(raw: dict, name: str) -> int:
    """Extract a required int field (rejects bool)."""
    try:
        val = raw[name]
    except KeyError:
        raise OpError(f"missing field '{name}'") from None
    if type(val) is not int:
        raise OpError(f"invalid field: {name} must be an int") from None
    return val


def _opt_int(raw: dict, name: str) -> int | None:
    """Extract an optional int field (rejects bool)."""
    val = raw.get(name)
    if val is None:
        return None
    if type(val) is not int:
        raise OpError(f"invalid field: {name} must be an int") from None
    return val


def _req_date(raw: dict, name: str) -> date:
    """Extract a required date field (ISO string)."""
    try:
        val = raw[name]
    except KeyError:
        raise OpError(f"missing field '{name}'") from None
    if not isinstance(val, str):
        raise OpError(f"invalid field: {name} must be a date string") from None
    try:
        return date.fromisoformat(val)
    except ValueError:
        raise OpError(f"invalid field: {name} must be an ISO date") from None


def _req_part(raw: dict, name: str) -> Part:
    """Extract a required Part field."""
    try:
        val = raw[name]
    except KeyError:
        raise OpError(f"missing field '{name}'") from None
    if val not in PARTS:
        raise OpError(f"invalid field: {name} must be one of {PARTS}") from None
    return val


def _opt_bool(raw: dict, name: str, default: bool) -> bool:
    """Extract an optional bool field."""
    val = raw.get(name, default)
    if not isinstance(val, bool):
        raise OpError(f"invalid field: {name} must be a bool") from None
    return val


def _opt_str(raw: dict, name: str, default: str) -> str:
    """Extract an optional string field."""
    val = raw.get(name, default)
    if not isinstance(val, str):
        raise OpError(f"invalid field: {name} must be a string") from None
    return val


def parse(raw: dict) -> Op:
    """Parse a raw dict into an Op."""
    op_name = _req_str(raw, "op")

    if op_name == "move":
        return Move(
            date=_req_date(raw, "date"),
            subject=_req_str(raw, "subject"),
            kind=_req_str(raw, "kind"),
            to=_req_date(raw, "to"),
            sessions=_opt_int(raw, "sessions"),
            for_request=_opt_int(raw, "for_request"),
        )
    elif op_name == "add":
        return Add(
            date=_req_date(raw, "date"),
            part=_req_part(raw, "part"),
            subject=_req_str(raw, "subject"),
            kind=_req_str(raw, "kind"),
            sessions=_req_int(raw, "sessions"),
            for_request=_opt_int(raw, "for_request"),
        )
    elif op_name == "remove":
        return Remove(
            date=_req_date(raw, "date"),
            subject=_req_str(raw, "subject"),
            kind=_req_str(raw, "kind"),
            sessions=_opt_int(raw, "sessions"),
            for_request=_opt_int(raw, "for_request"),
        )
    elif op_name == "template":
        return TemplateSet(
            weekday=_req_int(raw, "weekday"),
            part=_req_part(raw, "part"),
            subject=_req_str(raw, "subject"),
            kind=_req_str(raw, "kind"),
            sessions=_req_int(raw, "sessions"),
            for_request=_opt_int(raw, "for_request"),
        )
    elif op_name == "report":
        return Report(
            date=_req_date(raw, "date"),
            subject=_req_str(raw, "subject"),
            kind=_req_str(raw, "kind"),
            sessions=_req_int(raw, "sessions"),
            note=_opt_str(raw, "note", ""),
        )
    elif op_name == "withdraw":
        return Withdraw(question_id=_req_int(raw, "question_id"))
    elif op_name == "subject":
        return SubjectSet(
            name=_req_str(raw, "name"),
            label=_req_str(raw, "label"),
            active=_opt_bool(raw, "active", True),
        )
    elif op_name == "kind":
        return KindSet(name=_req_str(raw, "name"), active=_opt_bool(raw, "active", True))
    elif op_name == "cap":
        return CapSet(cap=_req_int(raw, "cap"))
    else:
        raise OpError(f"unknown op '{op_name}'")
