"""One organizer turn: a `claude -p` subprocess resuming the session, streamed as stream-json."""

import contextlib
import dataclasses
import json
import logging
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from orrery import db, schedule, store
from orrery.ai import fence
from orrery.organizer import prompt, sessions
from orrery.settings import Settings

log = logging.getLogger(__name__)

BUSY = "the organizer is busy in another window"
NO_TOOLS = "organizer couldn't reach orrery's tools; nothing changed"
UNSAFE = "organizer started with tools it must not have; nothing changed"
RESTORED = "organizer context restored from transcript"
CONTEXT_FULL = "the organizer's context is full; x starts a new chat"
FAILED = "the organizer didn't answer; nothing changed"
CANCELLED = "turn cancelled"
TIMED_OUT = "turn timed out"

_MISSING = "missing session"  # internal: a --resume whose session is gone, or an id in use


def _kill(proc: subprocess.Popen) -> None:
    """SIGKILL the whole process group, so no grandchild keeps our stdout open."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


@dataclass(frozen=True)
class TurnResult:
    """The reply, whether the turn answered, orrery's line about it if any, and whether the
    transcript has that line (else only a note can say it)."""

    text: str
    ok: bool
    line: str | None
    recorded: bool = False


def argv(settings: Settings, claude: str, s: sessions.Session, mcp_file: Path) -> list[str]:
    """The organizer's argv: only orrery's MCP tools, none of the user's own setup."""
    return [
        claude, "-p", "--restricted", "--tools", "", "--mcp-config", str(mcp_file),
        "--strict-mcp-config", "--allowedTools", "mcp__orrery", "--output-format", "stream-json",
        "--verbose", "--include-partial-messages", "--model", settings.ai_model,
        "--resume" if s.claude_live else "--session-id", s.claude_id,
        "--system-prompt", prompt.system(),
    ]  # fmt: skip


# the CLAUDE* names the organizer's CLI still needs: provider, auth, mTLS, config dir
_KEEP = ("CLAUDE_CODE_USE_", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_CLIENT_CERT",
         "CLAUDE_CODE_CLIENT_KEY", "CLAUDE_CONFIG_DIR")  # fmt: skip


def _kept(k: str) -> bool:
    return (
        not k.startswith("CLAUDE")
        or k.startswith(_KEEP)
        or (k.startswith("CLAUDE_CODE_SKIP_") and k.endswith("_AUTH"))
    )


def env() -> dict[str, str]:
    """os.environ without the runner guard or a parent Claude Code session's CLAUDE* vars;
    provider, auth and config-dir ones stay."""
    guard = "ORRERY_DISABLE_EXTERNAL_RUNNERS"
    return {k: v for k, v in os.environ.items() if k != guard and _kept(k)}


def run_turn(
    root: Path,
    settings: Settings,
    line: str,
    speaker: Literal["you", "orrery"],
    *,
    now: Callable[[], datetime],
    on_text: Callable[[str], None],
    cancel: threading.Event,
    timeout_sec: float = 300,
) -> TurnResult:
    """One turn, one at a time across processes; it records the transcript and never raises."""
    deadline = time.monotonic() + timeout_sec
    try:
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(store.file_lock("organizer", blocking=False))
            except BlockingIOError:
                return TurnResult("", False, BUSY)
            conn = db.open_existing(root)
            if isinstance(conn, store.Unavailable):
                log.warning("organizer turn: data unavailable (%s)", conn.reason)
                return TurnResult("", False, FAILED)
            stack.callback(conn.close)
            s = sessions.ensure(conn, now())
            talked = sessions.has_talk(conn, s.id)
            news = sessions.unheard(conn, s.id)  # read before the line, which goes as itself
            sessions.say(conn, s.id, speaker, line, now())
            try:
                r = _turn(
                    conn, s, root, settings, line, talked, news, now, on_text, cancel, deadline
                )
            except Exception:
                log.exception("organizer turn failed")
                r = TurnResult("", False, FAILED)
            if not r.ok:
                sessions.say(conn, s.id, "orrery", r.line, now())
                r = dataclasses.replace(r, recorded=True)
            return r
    except Exception:
        log.exception("organizer turn failed")
        return TurnResult("", False, FAILED)


def _turn(
    conn, s, root, settings, line, talked, news, now, on_text, cancel, deadline
) -> TurnResult:
    claude = settings.claude_path or (
        None if os.environ.get("ORRERY_DISABLE_EXTERNAL_RUNNERS") == "1" else shutil.which("claude")
    )
    if claude is None:
        log.warning("organizer: no claude CLI on PATH; set claude_path under [ai] in config.toml")
        return TurnResult("", False, FAILED)
    cwd = root / "organizer"
    cwd.mkdir(parents=True, exist_ok=True)
    mcp_file = cwd / "mcp.json"
    server = {
        "command": sys.executable,
        "args": ["-m", "orrery", "mcp", "--role", "organizer"],
        "env": {"ORRERY_HOME": str(root)},
    }
    mcp_file.write_text(json.dumps({"mcpServers": {"orrery": server}}))
    today = schedule.today(now(), settings.day_boundary_hour)

    def message(fresh: bool) -> str:
        # a fresh Claude session gets the earlier lines, if any were said, and history instead
        m = prompt.state(conn, root, today, fresh=fresh, news=() if fresh else news)
        m += "\n\n" + line
        if fresh and talked:
            earlier = sessions.transcript(conn, s.id, limit=21)[:-1]  # all but the current line
            said = "\n".join(f"{ln.speaker}: {ln.text}" for ln in earlier)
            m = fence.wrap_corpus("earlier in this chat:\n" + said) + "\n\n" + m
        return m

    def attempt(s: sessions.Session) -> tuple[str, str | None]:
        cmd = argv(settings, claude, s, mcp_file)
        return _attempt(cmd, cwd, message(not s.claude_live), conn, s, on_text, cancel, deadline)

    reply, problem = attempt(s)
    restored = problem == _MISSING
    if restored:
        s = sessions.reseed(conn, s.id)
        reply, problem = attempt(s)
        if problem == _MISSING:  # the fresh id is in use too: give up, never loop
            log.warning("organizer turn failed: the CLI refused the reseeded session id too")
            problem = FAILED
    if problem is not None:
        return TurnResult("", False, problem)
    if restored:
        sessions.say(conn, s.id, "orrery", RESTORED, now())
    sessions.say(conn, s.id, "organizer", reply, now())
    return TurnResult(reply, True, RESTORED if restored else None, recorded=restored)


def _attempt(
    cmd: list[str],
    cwd: Path,
    msg: str,
    conn: sqlite3.Connection,
    s: sessions.Session,
    on_text: Callable[[str], None],
    cancel: threading.Event,
    deadline: float,
) -> tuple[str, str | None]:
    """Run one subprocess; returns (reply, None) if ok, else ("", the problem). The reply is
    the turn's last text block that says anything, so the commentary between tool calls goes;
    on_text hears each text block as it streams, afresh."""
    stopped: list[str] = []  # set by the watchdog
    refused = None  # set at init
    init_seen = False
    r: dict = {}
    block = last = ""  # the text block streaming, and the last one with any text
    with (
        tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err,
        subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
            encoding="utf-8", errors="replace", cwd=cwd, env=env(), start_new_session=True,
        ) as proc,
    ):  # fmt: skip

        def watch() -> None:  # until the reader is done: a grandchild may outlive claude
            while not done.is_set():
                if cancel.wait(0.1) or time.monotonic() > deadline:
                    stopped.append(CANCELLED if cancel.is_set() else TIMED_OUT)
                    _kill(proc)
                    break

        done = threading.Event()
        dog = threading.Thread(target=watch, daemon=True)
        dog.start()
        try:
            with contextlib.suppress(BrokenPipeError):
                try:
                    proc.stdin.write(msg)
                finally:  # closed here, so Popen's exit has no flush left to break
                    proc.stdin.close()
            for raw in proc.stdout:
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                if ev.get("type") == "system" and ev.get("subtype") == "init":
                    init_seen = True
                    if not s.claude_live:  # the CLI has the session now, refused or not
                        sessions.mark_live(conn, s.id)
                    servers = ev.get("mcp_servers") or []
                    tools = [str(t) for t in ev.get("tools") or []]
                    if not any(
                        m.get("name") == "orrery" and m.get("status") == "connected"
                        for m in servers
                    ):
                        refused = NO_TOOLS
                    elif any(not t.startswith("mcp__orrery__") for t in tools):
                        refused = UNSAFE
                    elif "mcp__orrery__propose" not in tools:  # connected, but no tools listed
                        refused = NO_TOOLS
                    if refused:
                        _kill(proc)
                        break
                elif ev.get("type") == "stream_event":
                    e = ev.get("event") or {}
                    started = (e.get("content_block") or {}).get("type")
                    if e.get("type") == "content_block_start" and started == "text":
                        block = ""
                    d = e.get("delta") or {}
                    if d.get("type") == "text_delta":
                        block += d.get("text", "")
                        if block.strip():
                            last = block
                            on_text(block)
                elif ev.get("type") == "result":
                    r = ev
        except BaseException:
            _kill(proc)
            raise
        finally:
            proc.wait()
            done.set()
            dog.join()
        err.seek(0)
        raw_err = err.read()

    if refused or stopped:
        return "", refused or stopped[0]
    if init_seen and r.get("is_error") is False:
        if r.get("permission_denials"):
            return "", NO_TOOLS
        return last or r.get("result") or "", None  # result: the last text block too
    errors = map(str, r.get("errors") or [])
    said = " ".join([str(r.get("result") or ""), *errors]).lower()
    stderr_text = raw_err.lower()
    gone = "no conversation found" if s.claude_live else "is already in use"
    if not init_seen and gone in f"{said} {stderr_text}":
        return "", _MISSING  # the CLI lost the session, or already has this id: reseed
    # stderr may say "context" and "limit" about anything (a rate limit): only "too long" there
    if (
        "too long" in said
        or ("context" in said and "limit" in said)
        or any(m in stderr_text for m in ("prompt is too long", "input is too long"))
    ):
        return "", CONTEXT_FULL
    log.warning("organizer turn failed; stderr tail: %r", raw_err[-500:])  # the log only
    return "", FAILED
