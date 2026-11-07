"""Change sets: every audited write happens inside one, and the triggers log it to history."""

import contextlib
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime


class ChangeError(RuntimeError):
    """Raised when a change set is opened on a connection already in a transaction."""


@dataclass(frozen=True)
class HistoryRow:
    change_id: int
    at: str
    actor: str
    kind: str
    reason: str
    proposal_id: int | None
    tbl: str
    op: str
    old: dict | None
    new: dict | None


@contextlib.contextmanager
def change(
    conn: sqlite3.Connection,
    *,
    actor: str,
    kind: str,
    now: datetime,
    reason: str = "",
    proposal_id: int | None = None,
    undoes: int | None = None,
    effective: date | None = None,
) -> Iterator[int]:
    """One transaction that records who, when and why; yields the change id."""
    if conn.in_transaction:
        raise ChangeError("a transaction is already open on this connection")
    conn.execute("BEGIN IMMEDIATE")
    try:
        cid = conn.execute(
            "insert into changes (at, actor, kind, reason, proposal_id, undoes, effective)"
            " values (?, ?, ?, ?, ?, ?, ?)",
            (
                now.isoformat(timespec="seconds"),
                actor,
                kind,
                reason,
                proposal_id,
                undoes,
                effective and effective.isoformat(),
            ),
        ).lastrowid
        assert cid is not None
        conn.execute("insert into open_change values (1, ?)", (cid,))
        yield cid
        conn.execute("delete from open_change")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def history(
    conn: sqlite3.Connection,
    *,
    change_id: int | None = None,
    tbl: str | None = None,
    since: str | None = None,
) -> list[HistoryRow]:
    """History rows, oldest first; `since` keeps changes with `at >= since`."""
    rows = conn.execute(
        "select c.id, c.at, c.actor, c.kind, c.reason, c.proposal_id, h.tbl, h.op, h.old, h.new"
        " from history h join changes c on c.id = h.change_id"
        " where (:cid is null or c.id = :cid) and (:tbl is null or h.tbl = :tbl)"
        " and (:since is null or c.at >= :since) order by h.id",
        {"cid": change_id, "tbl": tbl, "since": since},
    )
    return [HistoryRow(*r[:8], *(json.loads(j) if j else None for j in r[8:])) for r in rows]


def last_change(conn: sqlite3.Connection, kind: str) -> int | None:
    return conn.execute("select max(id) from changes where kind = ?", (kind,)).fetchone()[0]
