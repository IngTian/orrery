"""A fake `claude` executable for organizer turns, plus event builders and helpers.

Tests never run the real CLI: they hand `run_turn` the path `make_fake` returns.
"""

import dataclasses
import json
import sys
import tempfile
import threading
from contextlib import closing
from pathlib import Path

from conftest import FROZEN_NOW

from orrery import db, settings
from orrery.organizer import sessions, turn

_TESTS = Path(__file__).parent

_SCRIPT = """\
#!{python}
import json, os, subprocess, sys, time
from pathlib import Path

RESPONSES = json.loads({responses!r})
SIDE_EFFECT = {side_effect!r}
calls = Path(__file__).parent / "calls.jsonl"
i = len(calls.read_text().splitlines()) if calls.exists() else 0
resp = RESPONSES[min(i, len(RESPONSES) - 1)]
stdin = "" if resp.get("deaf") else sys.stdin.read()
with calls.open("a") as f:
    f.write(json.dumps({{"argv": sys.argv, "stdin": stdin, "env": dict(os.environ)}}) + "\\n")
if resp.get("spawn"):
    subprocess.Popen(resp["spawn"])  # a grandchild that inherits stdout
time.sleep(resp.get("sleep", 0.0))
events = resp.get("events", [])
cut = next(
    (k for k, e in enumerate(events) if isinstance(e, dict) and e.get("type") == "result"),
    len(events),
)
for e in events[:cut]:
    print(e if isinstance(e, str) else json.dumps(e), flush=True)
time.sleep(resp.get("linger", 0.0))
if SIDE_EFFECT:
    sys.path.insert(0, {tests!r})
    import importlib
    mod, fn = SIDE_EFFECT.split(":")
    cfg = json.loads(Path(sys.argv[sys.argv.index("--mcp-config") + 1]).read_text())
    home = Path(cfg["mcpServers"]["orrery"]["env"]["ORRERY_HOME"])
    getattr(importlib.import_module(mod), fn)(home)
for e in events[cut:]:
    print(e if isinstance(e, str) else json.dumps(e), flush=True)
sys.stderr.write(resp.get("stderr", ""))
sys.exit(resp.get("exit", 0))
"""


def make_fake(tmp_path: Path, *responses: dict, side_effect: str | None = None) -> Path:
    """An executable fake claude; call i answers with responses[min(i, len - 1)].

    A response is {"events": [...], "exit": 0, "stderr": "", "sleep": 0.0}, plus optional
    "spawn" (argv of a grandchild started with inherited stdout), "linger" (seconds to wait
    after the events before the result) and "deaf" (never read stdin). A str event is printed
    as a raw line.
    """
    d = Path(tempfile.mkdtemp(dir=tmp_path, prefix="fake-claude-"))
    script = d / "claude"
    script.write_text(
        _SCRIPT.format(
            python=sys.executable,
            responses=json.dumps(list(responses)),
            side_effect=side_effect,
            tests=str(_TESTS),
        )
    )
    script.chmod(0o755)
    return script


INIT_OK = {
    "type": "system",
    "subtype": "init",
    "session_id": "fake",
    "tools": ["mcp__orrery__propose", "mcp__orrery__overview"],
    "mcp_servers": [{"name": "orrery", "status": "connected", "source": "dynamic"}],
}
INIT_FAILED = {
    **INIT_OK,
    "tools": [],
    "mcp_servers": [{"name": "orrery", "status": "failed", "source": "dynamic"}],
}
INIT_EXTRA_TOOLS = {**INIT_OK, "tools": [*INIT_OK["tools"], "Bash"]}


def delta(t: str) -> dict:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": t},
        },
    }


def block_start(kind: str = "text") -> dict:
    return {
        "type": "stream_event",
        "event": {"type": "content_block_start", "index": 1, "content_block": {"type": kind}},
    }


def result_ok(t: str, denials=()) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 1,
        "result": t,
        "permission_denials": list(denials),
    }


def result_error(t: str, errors=()) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "num_turns": 1,
        "result": t,
        "errors": list(errors),
        "permission_denials": [],
    }


def resume_missing(uuid: str) -> dict:
    """The probed shape of `--resume <unknown id>`: no init, exit 1, the error on both streams."""
    msg = f"No conversation found with session ID: {uuid}"
    result = {
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "num_turns": 0,
        "errors": [msg],
        "permission_denials": [],
    }
    return {"events": [result], "exit": 1, "stderr": msg + "\n"}


def run(home: Path, fake: Path | None, line: str, **kw) -> turn.TurnResult:
    """run_turn with settings loaded from home and claude_path pointing at fake."""
    s = dataclasses.replace(
        settings.load_settings(home), claude_path=str(fake) if fake is not None else None
    )
    args = dict(speaker="you", now=lambda: FROZEN_NOW, on_text=lambda t: None)
    args["cancel"] = threading.Event()
    return turn.run_turn(home, s, line, **{**args, **kw})


def read_calls(fake: Path) -> list[dict]:
    path = fake.parent / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines()]


def transcript_tail(home: Path, n: int) -> list[sessions.Line]:
    with closing(db.open_existing(home)) as conn:
        return sessions.transcript(conn, sessions.active(conn).id, limit=n)
