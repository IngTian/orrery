import json
import shutil
import subprocess
import sys
import threading
import time
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from conftest import FROZEN_NOW
from fake_claude import (
    INIT_EXTRA_TOOLS,
    INIT_FAILED,
    INIT_OK,
    block_start,
    delta,
    make_fake,
    read_calls,
    result_error,
    result_ok,
    resume_missing,
    run,
    transcript_tail,
)
from helpers import TODAY, propose_move, remember_at

from orrery import db, proposals, store
from orrery.ai import fence
from orrery.organizer import prompt, sessions, turn
from orrery.organizer.sessions import Session
from orrery.settings import Settings
from orrery.text import VERDICT_WORDS

SETTINGS = Settings(Path("/r"), ai_model="haiku")
OK = {"events": [INIT_OK, result_ok("ok")]}


def _rows(home: Path, n: int) -> list[tuple[str, str]]:
    return [(ln.speaker, ln.text) for ln in transcript_tail(home, n)]


def _live(home: Path) -> Session:
    """An active session already live in Claude, with one exchange of talk."""
    with closing(db.open_existing(home)) as conn:
        s = sessions.ensure(conn, FROZEN_NOW)
        sessions.mark_live(conn, s.id)
        sessions.say(conn, s.id, "you", "earlier line", FROZEN_NOW)
        sessions.say(conn, s.id, "organizer", "earlier reply", FROZEN_NOW)
        return sessions.active(conn)


def _active(home: Path) -> Session:
    with closing(db.open_existing(home)) as conn:
        return sessions.active(conn)


def test_argv_first_turn_and_resume():
    s = Session(1, "u-1", False, "…")
    a = turn.argv(SETTINGS, "/c", s, Path("/m.json"))
    assert a[:5] == ["/c", "-p", "--restricted", "--tools", ""]
    assert a[-4:-2] == ["--session-id", "u-1"] and a[-2] == "--system-prompt"
    assert turn.argv(SETTINGS, "/c", replace(s, claude_live=True), Path("/m.json"))[-4:-2] == [
        "--resume",
        "u-1",
    ]
    assert a == [
        "/c", "-p", "--restricted", "--tools", "", "--mcp-config", "/m.json",
        "--strict-mcp-config", "--allowedTools", "mcp__orrery", "--output-format", "stream-json",
        "--verbose", "--include-partial-messages", "--model", "haiku", "--session-id", "u-1",
        "--system-prompt", prompt.system(),
    ]  # fmt: skip


def test_system_prompt_shape():
    p = prompt.system()
    assert not p.startswith("-") and p.endswith(fence.CORPUS_NOTICE)
    assert all(w in p for w in VERDICT_WORDS)
    for name in (
        "propose(operations, rationale, dispositions, replaces)",
        "ask(subject, question)",
    ):
        assert name in p


def test_system_prompt_says_nothing_is_planned_before_starts():
    """The chat can't say a session is on a day the screen shows before orrery starts."""
    p = " ".join(prompt.system().split())
    assert "The overview's first line names the day orrery starts: days before it have no plan" in p
    assert "never tell the user a session is planned on one" in p


def test_system_prompt_pins_keeps_plain_text_and_silent_tools():
    """It can't claim a keep it never saw, writes no Markdown, and never narrates its tools."""
    p = " ".join(prompt.system().split())
    for rule in (
        "Only the user keeps or discards a proposal, with y or n in orrery",
        "Never say a proposal was kept, applied or discarded unless the state or an orrery line"
        " says so",
        "A bare y or n the user writes while a proposal waits is orrery's to handle",
        'if they write anything else that means it, such as "keep it", tell them to press y (or'
        " type y)",
        "The chat is a terminal that shows no Markdown: replies, rationale and notes are plain"
        " sentences, with no **, #, backticks or bullet syntax",
        "call tools silently, then write one reply after them, never commentary on your own"
        " tool use",
        "To change a waiting one, fold its changes into a new proposal that passes"
        " replaces=<its id>",
    ):
        assert rule in p, rule


def test_state_says_whether_a_proposal_is_waiting(week_home, tmp_path):
    fake = make_fake(tmp_path, OK)
    run(week_home, fake, "hi")
    with closing(db.open_existing(week_home)) as conn:
        pid = propose_move(conn).id
    run(week_home, fake, "and now?")
    first, second = (c["stdin"] for c in read_calls(fake))
    assert "2026-11-12\n\nno proposal waiting\n\nmemory:" in first
    assert f"2026-11-12\n\nproposal #{pid} is waiting for the user's y/n\n\nmemory:" in second


def test_state_says_how_the_last_proposal_ended(week_home, tmp_path):
    fake = make_fake(tmp_path, OK)
    with closing(db.open_existing(week_home)) as conn:
        dropped = propose_move(conn).id
        proposals.discard(conn, dropped, now=FROZEN_NOW)
    run(week_home, fake, "a")
    with closing(db.open_existing(week_home)) as conn:
        kept = propose_move(conn).id
        proposals.keep(conn, kept, now=FROZEN_NOW, today=TODAY)
    run(week_home, fake, "b")
    with closing(db.open_existing(week_home)) as conn:
        proposals.undo_last(conn, now=FROZEN_NOW, today=TODAY)
    run(week_home, fake, "c")
    a, b, c = (call["stdin"] for call in read_calls(fake))
    assert f"\n\nno proposal waiting; the last, #{dropped}, was discarded\n\nmemory:" in a
    assert f"\n\nno proposal waiting; the last, #{kept}, was kept\n\nmemory:" in b
    assert f"\n\nno proposal waiting; the last, #{kept}, was kept, then undone\n\nmemory:" in c


def test_state_says_the_last_proposal_expired_and_why(week_home):
    with closing(db.open_existing(week_home)) as conn:
        pid = propose_move(conn).id
        fri = TODAY + timedelta(days=1)
        proposals.expire(conn, now=datetime(2026, 11, 13, 6), today=fri)
        got = prompt.state(conn, week_home, fri, fresh=False)
    line = f"no proposal waiting; the last, #{pid}, expired: past days keep their plan"
    assert f"\n\n{line}\n\nmemory:" in got


def test_a_keep_while_the_organizer_answers_reaches_its_next_turn(week_home, tmp_path):
    """orrery's kept line lands before that turn's reply, so it is not news to the next turn;
    the state still says the proposal was kept."""
    with closing(db.open_existing(week_home)) as conn:
        pid = propose_move(conn).id
    fake = make_fake(tmp_path, OK, side_effect="helpers:keep_waiting_at")
    run(week_home, fake, "first")
    run(week_home, fake, "what now")
    first, second = (c["stdin"] for c in read_calls(fake))
    assert f"proposal #{pid} is waiting for the user's y/n" in first
    assert f"\n\nno proposal waiting; the last, #{pid}, was kept\n\nmemory:" in second
    said = [t for _, t in _rows(week_home, 5)]
    assert said == ["first", f"kept proposal #{pid}", "ok", "what now", "ok"]  # kept mid-turn


def test_a_resumed_turn_hears_what_orrery_said_since_its_reply(week_home, tmp_path):
    """A keep or discard, said by orrery between turns, reaches the organizer's next turn once;
    a fresh session has it in the earlier lines instead."""
    fake = make_fake(tmp_path, OK)
    run(week_home, fake, "hi")
    with closing(db.open_existing(week_home)) as conn:
        sid = sessions.active(conn).id
        sessions.say(conn, sid, "orrery", "kept proposal #1", FROZEN_NOW)
    run(week_home, fake, "and now?")
    run(week_home, fake, "and then?")
    first, second, third = (c["stdin"] for c in read_calls(fake))
    assert "orrery since your last reply:" not in first
    assert "\n\norrery since your last reply:\nkept proposal #1\n\nmemory:" in second
    assert "kept proposal #1" not in third


def test_env_drops_runner_guard_and_mcp_uses_this_python(week_home, tmp_path):
    fake = make_fake(tmp_path, {"events": [INIT_OK, result_ok("ok")]})
    run(week_home, fake, "hi")
    env = read_calls(fake)[0]["env"]
    assert "ORRERY_DISABLE_EXTERNAL_RUNNERS" not in env
    cfg = json.loads((week_home / "organizer" / "mcp.json").read_text())["mcpServers"]["orrery"]
    assert cfg["command"] == sys.executable
    assert cfg["args"] == ["-m", "orrery", "mcp", "--role", "organizer"]
    assert cfg["env"] == {"ORRERY_HOME": str(week_home)}


def test_env_drops_the_parent_claude_session(week_home, tmp_path, monkeypatch):
    kept = {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_SKIP_BEDROCK_AUTH", "CLAUDE_CONFIG_DIR",
            "CLAUDE_CODE_USE_BEDROCK"}  # fmt: skip
    dropped = {"CLAUDE_EFFORT", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID",
               "CLAUDE_CODE_SKIP_PROMPT_HISTORY"}  # fmt: skip
    for k in kept | dropped:
        monkeypatch.setenv(k, "1")
    fake = make_fake(tmp_path, OK)
    run(week_home, fake, "hi")
    env = read_calls(fake)[0]["env"]
    assert not dropped & env.keys()
    assert {"PATH", "HOME", *kept} <= env.keys()  # provider, auth and config dir stay


def test_failed_turn_logs_stderr_only(week_home, tmp_path, caplog):
    fake = make_fake(tmp_path, {"events": [], "exit": 1, "stderr": "x" * 600 + "boom-auth\n"})
    with caplog.at_level("WARNING", logger="orrery.organizer.turn"):
        r = run(week_home, fake, "hi")
    assert r == turn.TurnResult("", False, turn.FAILED)
    assert any("boom-auth" in rec.getMessage() for rec in caplog.records)
    assert all(len(rec.getMessage()) < 600 for rec in caplog.records)  # the tail only
    assert "boom-auth" not in " ".join(t for _, t in _rows(week_home, 2))


def test_streams_and_records_transcript(week_home, tmp_path):
    seen = []
    r = run(
        week_home,
        make_fake(tmp_path, {"events": [INIT_OK, delta("Mo"), delta("ved."), result_ok("Moved.")]}),
        "hi",
        on_text=seen.append,
    )
    assert r.ok and seen[-1] == "Moved."
    assert [ln.speaker for ln in transcript_tail(week_home, 2)] == ["you", "organizer"]
    assert r == turn.TurnResult("Moved.", True, None)


def test_new_block_starts_a_new_line(week_home, tmp_path):
    seen = []
    events = [INIT_OK, block_start(), delta("A"), block_start(), delta("B"), result_ok("B")]
    run(week_home, make_fake(tmp_path, {"events": events}), "hi", on_text=seen.append)
    assert seen == ["A", "A\nB"]


def test_only_text_blocks_start_a_new_line(week_home, tmp_path):
    seen = []
    events = [INIT_OK, block_start(), delta("A"), block_start("thinking"),
              block_start("tool_use"), block_start(), delta("B"), result_ok("B")]  # fmt: skip
    run(week_home, make_fake(tmp_path, {"events": events}), "hi", on_text=seen.append)
    assert seen == ["A", "A\nB"]


def test_records_every_text_block_not_only_the_last(week_home, tmp_path):
    events = [INIT_OK, block_start(), delta("A"), block_start("tool_use"), block_start(),
              delta("B"), result_ok("B")]  # fmt: skip
    r = run(week_home, make_fake(tmp_path, {"events": events}), "hi")
    assert r == turn.TurnResult("A\nB", True, None)
    assert _rows(week_home, 1) == [("organizer", "A\nB")]


def test_non_json_lines_are_skipped(week_home, tmp_path):
    events = ["hello", "", "[1, 2]", INIT_OK, "not json {", result_ok("ok")]
    r = run(week_home, make_fake(tmp_path, {"events": events}), "hi")
    assert r == turn.TurnResult("ok", True, None)


def test_state_is_fenced_and_has_fronts(week_home, tmp_path):
    (week_home / "fronts.md").write_text("# Fronts\nmath: lorem lemmas\n")
    fake = make_fake(tmp_path, OK)
    run(week_home, fake, "what's on thursday?")
    run(week_home, fake, "and friday?")
    first, second = (c["stdin"] for c in read_calls(fake))
    assert first.startswith("<orrery:untrusted>today is Thu 11/12 2026-11-12")
    assert "fronts.md:\n# Fronts\nmath: lorem lemmas" in first
    assert "memory:\nno notes" in first and "this week:" in first
    assert "history (last 7 days):" in first  # a fresh Claude session gets recent history
    assert first.endswith("</orrery:untrusted>\n\nwhat's on thursday?")
    assert "history (last 7 days):" not in second and "earlier in this chat:" not in second
    assert second.endswith("\n\nand friday?")


def test_state_without_fronts(week_home, tmp_path):
    fake = make_fake(tmp_path, OK)
    run(week_home, fake, "hi")
    assert "fronts.md:\nno fronts.md" in read_calls(fake)[0]["stdin"]


def test_mcp_not_connected_stops_turn(week_home, tmp_path):
    seen = []
    fake = make_fake(tmp_path, {"events": [INIT_FAILED, delta("x"), result_ok("x")]})
    r = run(week_home, fake, "hi", on_text=seen.append)
    assert r == turn.TurnResult("", False, turn.NO_TOOLS)
    assert seen == []
    assert _rows(week_home, 2) == [("you", "hi"), ("orrery", turn.NO_TOOLS)]
    assert "organizer" not in [sp for sp, _ in _rows(week_home, 100)]


def test_extra_tools_refused(week_home, tmp_path):
    seen = []
    fake = make_fake(tmp_path, {"events": [INIT_EXTRA_TOOLS, delta("x"), result_ok("x")]})
    r = run(week_home, fake, "hi", on_text=seen.append)
    assert r.line == turn.UNSAFE and not r.ok and seen == []
    assert _rows(week_home, 2) == [("you", "hi"), ("orrery", turn.UNSAFE)]


def test_permission_denied_is_no_tools(week_home, tmp_path):
    denied = result_ok("done", denials=[{"tool_name": "mcp__orrery__propose"}])
    r = run(week_home, make_fake(tmp_path, {"events": [INIT_OK, denied]}), "hi")
    assert r.line == turn.NO_TOOLS and not r.ok
    assert _rows(week_home, 2) == [("you", "hi"), ("orrery", turn.NO_TOOLS)]


def test_result_without_init_is_not_ok(week_home, tmp_path):
    r = run(week_home, make_fake(tmp_path, {"events": [result_ok("hello")]}), "hi")
    assert r.line == turn.FAILED and not r.ok


def test_failed_first_turn_resumes_next_time(week_home, tmp_path):
    fake = make_fake(tmp_path, {"events": [INIT_OK, result_error("x")], "exit": 1}, OK)
    r = run(week_home, fake, "hi")
    assert r.line == turn.FAILED and not r.ok
    assert _rows(week_home, 1) == [("orrery", turn.FAILED)]
    assert run(week_home, fake, "again").ok
    first, second = (c["argv"] for c in read_calls(fake))
    uid = _active(week_home).claude_id
    assert first[-4:-2] == ["--session-id", uid] and second[-4:-2] == ["--resume", uid]


@pytest.mark.parametrize(
    ("init", "line"), [(INIT_FAILED, turn.NO_TOOLS), (INIT_EXTRA_TOOLS, turn.UNSAFE)]
)
def test_refused_first_turn_resumes_next_time(week_home, tmp_path, init, line):
    """Once init is out the CLI has the session, so the next turn must resume, not re-create."""
    fake = make_fake(tmp_path, {"events": [init]}, OK)
    assert run(week_home, fake, "hi").line == line
    assert run(week_home, fake, "again").ok
    first, second = (c["argv"] for c in read_calls(fake))
    uid = _active(week_home).claude_id
    assert first[-4:-2] == ["--session-id", uid] and second[-4:-2] == ["--resume", uid]


def test_resume_failure_reseeds_once(week_home, tmp_path):
    s = _live(week_home)
    fake = make_fake(
        tmp_path, resume_missing(s.claude_id), {"events": [INIT_OK, result_ok("Back.")]}
    )
    r = run(week_home, fake, "hi")
    assert r == turn.TurnResult("Back.", True, turn.RESTORED)
    now = _active(week_home)
    assert now.id == s.id and now.claude_id != s.claude_id and now.claude_live
    first, second = read_calls(fake)
    assert first["argv"][-4:-2] == ["--resume", s.claude_id]
    assert second["argv"][-4:-2] == ["--session-id", now.claude_id]
    assert "earlier in this chat:" not in first["stdin"]
    assert second["stdin"].startswith("<orrery:untrusted>earlier in this chat:\n")
    assert "you: earlier line\norganizer: earlier reply" in second["stdin"]
    assert "you: hi" not in second["stdin"]  # the current line comes once, after the state
    assert "history (last 7 days):" in second["stdin"]
    assert _rows(week_home, 3) == [("you", "hi"), ("orrery", turn.RESTORED), ("organizer", "Back.")]

    again = make_fake(tmp_path, resume_missing(now.claude_id))
    r = run(week_home, again, "hi again")
    assert r == turn.TurnResult("", False, turn.FAILED)
    assert len(read_calls(again)) == 2
    assert _rows(week_home, 2) == [("you", "hi again"), ("orrery", turn.FAILED)]


def test_session_id_in_use_reseeds_once(week_home, tmp_path, caplog):
    """orrery died after claude made the session but before init: reseed, like a lost resume."""
    msg = "Error: Session ID 41b148be-30a9-4de1-b8c4-ab947607fc88 is already in use.\n"
    in_use = {"exit": 1, "stderr": msg}
    never = make_fake(tmp_path, in_use)
    assert run(week_home, never, "hi") == turn.TurnResult("", False, turn.FAILED)
    assert len(read_calls(never)) == 2  # one retry, never a loop
    assert _rows(week_home, 2) == [("you", "hi"), ("orrery", turn.FAILED)]
    assert any("reseeded" in r.getMessage() for r in caplog.records), "the give-up is logged"

    s = _active(week_home)
    fake = make_fake(tmp_path, in_use, OK)
    assert run(week_home, fake, "again") == turn.TurnResult("ok", True, turn.RESTORED)
    first, second = (c["argv"][-4:-2] for c in read_calls(fake))
    now = _active(week_home)
    assert first == ["--session-id", s.claude_id] and second == ["--session-id", now.claude_id]
    assert now.id == s.id and now.claude_id != s.claude_id and now.claude_live


def test_context_full_is_reported_not_rotated(week_home, tmp_path):
    s = _live(week_home)
    fake = make_fake(tmp_path, {"events": [INIT_OK, result_error("Prompt is too long")], "exit": 1})
    r = run(week_home, fake, "hi")
    assert r.line == turn.CONTEXT_FULL and not r.ok
    assert _active(week_home) == s and len(read_calls(fake)) == 1


@pytest.mark.parametrize(
    ("stderr", "line"),
    [
        ("API Error: rate limit reached; context: org quota\n", turn.FAILED),
        ("API Error: Input is too long for the requested model.\n", turn.CONTEXT_FULL),
    ],
)
def test_stderr_is_context_full_only_when_too_long(week_home, tmp_path, stderr, line):
    fake = make_fake(tmp_path, {"events": [INIT_OK, result_error("")], "exit": 1, "stderr": stderr})
    assert run(week_home, fake, "hi").line == line


def test_connected_without_organizer_tools_is_no_tools(week_home, tmp_path):
    seen = []
    fake = make_fake(tmp_path, {"events": [{**INIT_OK, "tools": []}, delta("x"), result_ok("x")]})
    r = run(week_home, fake, "hi", on_text=seen.append)
    assert r == turn.TurnResult("", False, turn.NO_TOOLS) and seen == []
    assert _rows(week_home, 2) == [("you", "hi"), ("orrery", turn.NO_TOOLS)]


def test_deaf_fake_with_large_stdin_times_out(week_home, tmp_path):
    """The fake never reads a message larger than the pipe: the write breaks when the turn is
    killed, and the turn still ends as timed out."""
    fake = make_fake(tmp_path, {"events": [], "sleep": 30, "deaf": True})
    r = run(week_home, fake, "x" * 1_000_000, timeout_sec=0.5)
    assert r.line == turn.TIMED_OUT


class _StuckStdin:
    """A pipe whose write broke with data still buffered: close's flush breaks too, once."""

    closed = False

    def write(self, s: str) -> int:
        raise BrokenPipeError

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            raise BrokenPipeError


def test_broken_stdin_close_is_cancelled(week_home, tmp_path, monkeypatch):
    """Popen's exit closes stdin again: a close that breaks must not turn cancelled into
    failed."""

    class StuckPopen(subprocess.Popen):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.stdin.close()  # the fake reads EOF
            self.stdin = _StuckStdin()

    monkeypatch.setattr(subprocess, "Popen", StuckPopen)
    fake = make_fake(tmp_path, {"events": [INIT_OK], "sleep": 30})
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    assert run(week_home, fake, "hi", cancel=cancel).line == turn.CANCELLED


def test_second_window_is_busy(week_home, tmp_path):
    inside, release = threading.Event(), threading.Event()

    def hold():
        with store.file_lock("organizer"):
            inside.set()
            release.wait(5)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    fake = make_fake(tmp_path, OK)
    try:
        assert inside.wait(5)
        r = run(week_home, fake, "hi")
    finally:
        release.set()
        t.join(5)
    assert r == turn.TurnResult("", False, turn.BUSY)
    assert read_calls(fake) == []
    with closing(db.open_existing(week_home)) as conn:
        assert conn.execute("select count(*) from transcript").fetchone()[0] == 0


def test_timeout_kills_silent_turn(week_home, tmp_path):
    fake = make_fake(tmp_path, {"events": [], "sleep": 30})
    t0 = time.monotonic()
    r = run(week_home, fake, "hi", timeout_sec=0.5)
    assert r.line == turn.TIMED_OUT and time.monotonic() - t0 < 2
    assert _rows(week_home, 1) == [("orrery", turn.TIMED_OUT)]


@pytest.mark.parametrize("fake_sleep", [30, 0])
def test_timeout_kills_grandchild_holding_stdout(week_home, tmp_path, fake_sleep):
    """A grandchild with our stdout (an orphaned `orrery mcp`, say) can't outlive the deadline,
    whether claude itself is still running or has already exited."""
    fake = make_fake(tmp_path, {"events": [], "sleep": fake_sleep, "spawn": ["sleep", "30"]})
    t0 = time.monotonic()
    r = run(week_home, fake, "hi", timeout_sec=0.5)
    assert r.line == turn.TIMED_OUT and time.monotonic() - t0 < 2


def test_on_text_error_kills_and_records(week_home, tmp_path):
    def boom(t):
        raise RuntimeError("screen gone")

    fake = make_fake(tmp_path, {"events": [INIT_OK, delta("A"), result_ok("A")], "linger": 30})
    t0 = time.monotonic()
    r = run(week_home, fake, "hi", on_text=boom)
    assert r == turn.TurnResult("", False, turn.FAILED) and time.monotonic() - t0 < 2
    assert _rows(week_home, 2) == [("you", "hi"), ("orrery", turn.FAILED)]
    with store.file_lock("organizer", blocking=False):  # released
        pass


def test_cancel_kills_turn(week_home, tmp_path):
    fake = make_fake(tmp_path, {"events": [INIT_OK], "sleep": 30})
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    t0 = time.monotonic()
    r = run(week_home, fake, "hi", cancel=cancel)
    assert r.line == turn.CANCELLED and time.monotonic() - t0 < 2


def test_bad_claude_path_is_failed(week_home, tmp_path):
    r = run(week_home, tmp_path / "nope", "hi")
    assert r == turn.TurnResult("", False, turn.FAILED)
    assert _rows(week_home, 2) == [("you", "hi"), ("orrery", turn.FAILED)]


def test_disabled_env_without_path_spawns_nothing(week_home, tmp_path, monkeypatch, caplog):
    fake = make_fake(tmp_path, OK)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: str(fake))
    r = run(week_home, None, "hi")
    assert r.line == turn.FAILED and read_calls(fake) == []
    assert "set claude_path under [ai]" in caplog.text  # the log names the fix


def test_missing_data_is_failed(tmp_path):
    fake = make_fake(tmp_path, OK)
    home = tmp_path / "no-home"
    s = Settings(home, claude_path=str(fake))
    r = turn.run_turn(home, s, "hi", "you", now=lambda: FROZEN_NOW, on_text=print,
                      cancel=threading.Event())  # fmt: skip
    assert r == turn.TurnResult("", False, turn.FAILED) and read_calls(fake) == []


def test_memory_in_state_follows_forget(week_home, tmp_path):
    remember_at(week_home, "keep Tuesdays light")
    fake = make_fake(tmp_path, OK)
    run(week_home, fake, "hi")
    with closing(db.open_existing(week_home)) as conn:
        (note,) = sessions.memory(conn)
        sessions.forget(conn, note.id, FROZEN_NOW)
    run(week_home, fake, "hi")
    first, second = (c["stdin"] for c in read_calls(fake))
    assert f"memory:\n#{note.id} keep Tuesdays light" in first
    assert "keep Tuesdays light" not in second and "memory:\nno notes" in second


def test_side_effect_runs_against_orrery_home(week_home, tmp_path):
    fake = make_fake(tmp_path, OK, side_effect="helpers:remember_at")
    assert run(week_home, fake, "hi").ok
    with closing(db.open_existing(week_home)) as conn:
        assert [n.text for n in sessions.memory(conn)] == ["protect Monday math"]


def test_side_effect_proposes_from_the_fake(week_home, tmp_path):
    fake = make_fake(tmp_path, OK, side_effect="helpers:propose_move_at")
    assert run(week_home, fake, "push thu math to fri").ok
    with closing(db.open_existing(week_home)) as conn:
        assert proposals.waiting(conn).author == "organizer"
