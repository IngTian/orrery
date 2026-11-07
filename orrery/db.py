"""The SQLite store: connections, migrations, and the triggers that keep history honest."""

import sqlite3
from pathlib import Path

from orrery import store

SCHEMA_VERSION = 3
NEWER = "made by a newer orrery"

_NAME = "CHECK (name GLOB '[a-z]*' AND name NOT GLOB '*[^a-z0-9-]*' AND length(name) <= 32)"
_PART = "CHECK (part IN ('warmup','main','residual'))"
# Date checks use `IS`, not `=`: date() of a non-date is NULL, and a NULL CHECK passes.
_BLOCKS = f"""(date TEXT NOT NULL CHECK (date(date) IS date),
  monday TEXT NOT NULL REFERENCES weeks(monday), part TEXT NOT NULL {_PART}, ord INTEGER NOT NULL,
  subject TEXT NOT NULL REFERENCES subjects(name), kind TEXT NOT NULL REFERENCES kinds(name),
  sessions INTEGER NOT NULL CHECK (sessions >= 1), PRIMARY KEY (date, part, ord)) STRICT"""

_TABLES = f"""
CREATE TABLE subjects (name TEXT PRIMARY KEY {_NAME}, label TEXT NOT NULL, ord INTEGER NOT NULL,
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1))) STRICT;
CREATE TABLE kinds (name TEXT PRIMARY KEY {_NAME},
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1))) STRICT;
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
CREATE TABLE changes (id INTEGER PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL,
  kind TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', proposal_id INTEGER REFERENCES proposals(id),
  undoes INTEGER UNIQUE REFERENCES changes(id), effective TEXT) STRICT;
CREATE TABLE history (id INTEGER PRIMARY KEY, change_id INTEGER NOT NULL REFERENCES changes(id),
  tbl TEXT NOT NULL, op TEXT NOT NULL CHECK (op IN ('insert','update','delete')), old TEXT,
  new TEXT) STRICT;
CREATE TABLE open_change (one INTEGER PRIMARY KEY CHECK (one = 1),
  change_id INTEGER NOT NULL REFERENCES changes(id)) STRICT;
CREATE TABLE template_blocks (weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
  part TEXT NOT NULL {_PART}, ord INTEGER NOT NULL CHECK (ord >= 0),
  subject TEXT NOT NULL REFERENCES subjects(name), kind TEXT NOT NULL REFERENCES kinds(name),
  sessions INTEGER NOT NULL CHECK (sessions >= 1), PRIMARY KEY (weekday, part, ord)) STRICT;
CREATE TABLE weeks (monday TEXT PRIMARY KEY
  CHECK (date(monday) IS monday AND strftime('%w', monday) = '1'), pinned_at TEXT NOT NULL) STRICT;
CREATE TABLE baseline_blocks {_BLOCKS};
CREATE TABLE plan_blocks {_BLOCKS};
CREATE TABLE overrides (date TEXT NOT NULL CHECK (date(date) IS date),
  subject TEXT NOT NULL REFERENCES subjects(name), kind TEXT NOT NULL REFERENCES kinds(name),
  mark TEXT NOT NULL CHECK (mark IN ('full','half','none')),
  PRIMARY KEY (date, subject, kind)) STRICT;
CREATE TABLE messages (id INTEGER PRIMARY KEY, channel TEXT NOT NULL REFERENCES subjects(name),
  kind TEXT NOT NULL CHECK (kind IN ('question','answer','report','agenda','request','outcome')),
  author TEXT NOT NULL, body TEXT NOT NULL CHECK (json_valid(body)), text TEXT NOT NULL DEFAULT '',
  reply_to INTEGER REFERENCES messages(id), key TEXT CHECK (key IS NULL OR length(key) > 0),
  state TEXT NOT NULL, created_at TEXT NOT NULL,
  applied_change INTEGER REFERENCES changes(id)) STRICT;
CREATE UNIQUE INDEX messages_key ON messages(channel, kind, key) WHERE key IS NOT NULL;
CREATE TABLE progress (id INTEGER PRIMARY KEY, date TEXT NOT NULL CHECK (date(date) IS date),
  subject TEXT NOT NULL REFERENCES subjects(name), kind TEXT NOT NULL REFERENCES kinds(name),
  sessions INTEGER NOT NULL CHECK (sessions >= 0),
  status TEXT NOT NULL CHECK (status IN ('done','partial')), note TEXT NOT NULL DEFAULT '',
  started_at TEXT, ended_at TEXT, actor TEXT NOT NULL,
  replaces INTEGER UNIQUE REFERENCES progress(id), message_id INTEGER REFERENCES messages(id),
  change_id INTEGER NOT NULL REFERENCES changes(id), created_at TEXT NOT NULL) STRICT;
CREATE UNIQUE INDEX progress_message ON progress(message_id) WHERE message_id IS NOT NULL;
CREATE TABLE agenda (id INTEGER PRIMARY KEY, subject TEXT NOT NULL REFERENCES subjects(name),
  date TEXT NOT NULL CHECK (date(date) IS date), kind TEXT REFERENCES kinds(name),
  item TEXT NOT NULL CHECK (length(item) > 0),
  deadline INTEGER NOT NULL DEFAULT 0 CHECK (deadline IN (0,1)),
  message_id INTEGER NOT NULL REFERENCES messages(id),
  change_id INTEGER NOT NULL REFERENCES changes(id), created_at TEXT NOT NULL) STRICT;
CREATE TABLE proposals (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, author TEXT NOT NULL,
  ops TEXT NOT NULL CHECK (json_valid(ops)), rationale TEXT NOT NULL DEFAULT '',
  dispositions TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(dispositions)),
  expected TEXT NOT NULL CHECK (json_valid(expected)),
  state TEXT NOT NULL CHECK (state IN ('waiting','kept','discarded','replaced')),
  decided_at TEXT, change_id INTEGER REFERENCES changes(id)) STRICT;
CREATE UNIQUE INDEX one_waiting ON proposals(state) WHERE state = 'waiting';
"""

# A missing session_cap row makes the comparison NULL, so no cap applies.
_CAP = "".join(
    f"CREATE TRIGGER {t}_cap_{op} BEFORE {event} ON {t} WHEN NEW.sessions > "
    "(SELECT CAST(value AS INTEGER) FROM settings WHERE key = 'session_cap')\n"
    "BEGIN SELECT RAISE(ABORT, 'over the cap'); END;\n"
    for t in ("template_blocks", "plan_blocks")
    for op, event in (("insert", "INSERT"), ("update", "UPDATE OF sessions"))
)

_BLOCK_COLS = "date monday part ord subject kind sessions"
_PROGRESS_COLS = (
    "id date subject kind sessions status note started_at ended_at actor replaces message_id"
    " change_id created_at"
)
_AGENDA_COLS = "id subject date kind item deadline message_id change_id created_at"
_MESSAGE_COLS = "id channel kind author body text reply_to key state created_at applied_change"
_MESSAGE_FROZEN = "channel kind author body text reply_to key created_at"
_ORGANIZER_SESSION_COLS = "id started_at ended_at claude_id claude_live active"
_TRANSCRIPT_COLS = "id session_id at speaker text"
_MEMORY_COLS = "id text created_at"
# table, every column (read only by "log"), insert, update, delete, forbid_update_of
_AUDIT_V1 = [
    ("subjects", "name label ord active", "log", "log", "log", ""),
    ("kinds", "name active", "log", "log", "log", ""),
    ("settings", "key value", "log", "log", "log", ""),
    ("template_blocks", "weekday part ord subject kind sessions", "log", "log", "log", ""),
    ("plan_blocks", _BLOCK_COLS, "log", "log", "log", ""),
    ("overrides", "date subject kind mark", "log", "log", "log", ""),
    ("weeks", "monday pinned_at", "log", "forbid", "forbid", ""),
    ("baseline_blocks", _BLOCK_COLS, "log", "forbid", "forbid", ""),
    ("progress", _PROGRESS_COLS, "log", "forbid", "forbid", ""),
    ("agenda", _AGENDA_COLS, "log", "forbid", "forbid", ""),
    ("messages", _MESSAGE_COLS, "free", "log", "forbid", _MESSAGE_FROZEN),
    ("changes", "", "free", "forbid", "forbid", ""),
    ("history", "", "free", "forbid", "forbid", ""),
    ("proposals", "", "free", "free", "forbid", ""),
]
_AUDIT_V2 = [
    ("memory", _MEMORY_COLS, "log", "log", "log", ""),
    ("transcript", _TRANSCRIPT_COLS, "free", "forbid", "forbid", ""),
    ("organizer_sessions", _ORGANIZER_SESSION_COLS, "free", "free", "forbid", ""),
]
_AUDIT = [a for a in _AUDIT_V1 if a[0] != "overrides"] + _AUDIT_V2


def audit_sql(
    table: str,
    cols: tuple[str, ...],
    *,
    insert: str,
    update: str,
    delete: str,
    forbid_update_of: tuple[str, ...] = (),
) -> str:
    """Triggers for one table. Each mode is "log", "forbid" or "free"; cols is every column."""
    refuse = f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;\n"
    out = []
    for op, mode in (("insert", insert), ("update", update), ("delete", delete)):
        on = f"{op.upper()} ON {table}"
        if mode == "forbid":
            out.append(f"CREATE TRIGGER {table}_{op}_forbid BEFORE {on}\n{refuse}")
        elif mode == "log":
            old = "NULL" if op == "insert" else _json("OLD", cols)
            new = "NULL" if op == "delete" else _json("NEW", cols)
            out.append(
                f"CREATE TRIGGER {table}_{op}_guard BEFORE {on} FOR EACH ROW\n"
                "WHEN NOT EXISTS (SELECT 1 FROM open_change)\n"
                f"BEGIN SELECT RAISE(ABORT, '{table}: write outside a change set'); END;\n"
                f"CREATE TRIGGER {table}_{op}_log AFTER {on} FOR EACH ROW\n"
                "BEGIN INSERT INTO history (change_id, tbl, op, old, new)\n"
                f"  VALUES ((SELECT change_id FROM open_change), '{table}', '{op}',\n"
                f"  {old}, {new});\n"
                "END;\n"
            )
        elif mode != "free":
            raise ValueError(f"unknown audit mode {mode!r}")
    if forbid_update_of:
        frozen = ", ".join(forbid_update_of)
        out.append(f"CREATE TRIGGER {table}_frozen BEFORE UPDATE OF {frozen} ON {table}\n{refuse}")
    return "".join(out)


def _json(row: str, cols: tuple[str, ...]) -> str:
    return "json_object(" + ", ".join(f"'{c}', {row}.{c}" for c in cols) + ")"


MIGRATIONS: list[str] = [
    _TABLES
    + _CAP
    + "".join(
        audit_sql(
            t, tuple(c.split()), insert=i, update=u, delete=d, forbid_update_of=tuple(f.split())
        )
        for t, c, i, u, d, f in _AUDIT_V1
    )
    + "PRAGMA user_version = 1;\n",
    """CREATE TABLE organizer_sessions (
  id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
  claude_id TEXT NOT NULL,
  claude_live INTEGER NOT NULL DEFAULT 0 CHECK (claude_live IN (0,1)),
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1))) STRICT;
CREATE UNIQUE INDEX one_active_session ON organizer_sessions(active)
  WHERE active = 1;
CREATE TABLE transcript (
  id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES organizer_sessions(id),
  at TEXT NOT NULL,
  speaker TEXT NOT NULL CHECK (speaker IN ('you','organizer','orrery')),
  text TEXT NOT NULL) STRICT;
CREATE TABLE memory (
  id INTEGER PRIMARY KEY,
  text TEXT NOT NULL CHECK (length(text) > 0),
  created_at TEXT NOT NULL) STRICT;
ALTER TABLE proposals ADD COLUMN session_id INTEGER
  REFERENCES organizer_sessions(id);
"""
    + audit_sql("memory", tuple(_MEMORY_COLS.split()), insert="log", update="log", delete="log")
    + audit_sql(
        "transcript",
        tuple(_TRANSCRIPT_COLS.split()),
        insert="free",
        update="forbid",
        delete="forbid",
    )
    + audit_sql(
        "organizer_sessions",
        tuple(_ORGANIZER_SESSION_COLS.split()),
        insert="free",
        update="free",
        delete="forbid",
    )
    + "PRAGMA user_version = 2;\n",
    # Widens messages.kind in place: a copy-drop-rename rebuild fails on the foreign keys that
    # point at messages. _m3 fails the migration if the edit missed. DROP TABLE takes the
    # overrides triggers with it, and a waiting proposal with a mark op could no longer load.
    """PRAGMA writable_schema = ON;
UPDATE sqlite_schema SET sql = replace(sql,
  'CHECK (kind IN (''question'',''answer'',''report'',''agenda'',''request'',''outcome''))',
  'CHECK (kind IN (''question'',''answer'',''report'',''agenda'',''request'',''outcome'','
    || '''assessment''))')
  WHERE type = 'table' AND name = 'messages';
PRAGMA writable_schema = RESET;
CREATE TEMP TABLE _m3 (ok INTEGER CHECK (ok = 1));
INSERT INTO _m3 SELECT count(*) FROM sqlite_schema
  WHERE name = 'messages' AND instr(sql, '''assessment''') > 0;
DROP TABLE _m3;
DROP TABLE overrides;
UPDATE proposals SET state = 'discarded',
  decided_at = strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')
  WHERE state = 'waiting' AND EXISTS (SELECT 1 FROM json_each(ops)
    WHERE CASE WHEN type = 'object' THEN json_extract(value, '$.op') END = 'mark');
PRAGMA user_version = 3;
""",
]


def db_path(root: Path) -> Path:
    return root / "orrery.db"


def connect(path: Path) -> sqlite3.Connection:
    """The only way to open a connection: FKs on, WAL, 5 s busy wait, explicit transactions."""
    conn = sqlite3.connect(path, autocommit=True, timeout=5.0)
    conn.execute("pragma foreign_keys = ON")
    # Without it, REPLACE deletes the conflicting row without firing delete triggers.
    conn.execute("pragma recursive_triggers = ON")
    conn.execute("pragma journal_mode = WAL")
    return conn


def _version(conn: sqlite3.Connection) -> int:
    return conn.execute("pragma user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> None:
    """Apply pending migrations in one transaction. Takes the write lock only if one is pending."""
    if _version(conn) >= SCHEMA_VERSION:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        # autocommit=True: executescript adds no COMMIT of its own, so all scripts share this
        # transaction, and a failure midway rolls every one of them back.
        for script in MIGRATIONS[_version(conn) :]:
            conn.executescript(script)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _is_newer(path: Path) -> bool:
    ro = sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True)
    try:
        return _version(ro) > SCHEMA_VERSION
    finally:
        ro.close()


def _open(path: Path) -> sqlite3.Connection:
    conn = connect(path)
    try:
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def open_db(root: Path) -> sqlite3.Connection:
    """Create the file if needed and migrate it. Only `init` uses this."""
    path = db_path(root)
    if path.exists() and _is_newer(path):
        raise RuntimeError(NEWER)
    return _open(path)


def open_existing(root: Path) -> sqlite3.Connection | store.Unavailable:
    """Open and migrate the database, or say why not; a newer file is left untouched."""
    path = db_path(root)
    if not path.exists():
        return store.Unavailable("missing")
    try:
        if _is_newer(path):
            return store.Unavailable(NEWER)
        return _open(path)
    except sqlite3.DatabaseError as e:  # e.g. "file is not a database"
        return store.Unavailable(str(e))
