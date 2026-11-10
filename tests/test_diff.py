"""Tests for orrery.tui.diff: a proposal's diff and the chat bar under it."""

from helpers import TODAY, propose_move
from rich.cells import cell_len
from textual.content import Content

from orrery import proposals
from orrery.tui import keymap
from orrery.tui.diff import chat_bar, render_diff

THU = TODAY


def _styles_of(text: Content, line: str, part: str | None = None) -> set[str]:
    """The styles of every span covering part (all of line by default), as it sits at the end
    of the first occurrence of line."""
    part = part or line
    start = text.plain.index(line) + line.rindex(part)
    return {str(s.style) for s in text.spans if s.start <= start and s.end >= start + len(part)}


def test_diff_strikes_the_struck_phrase(week_db):
    p = propose_move(week_db, rationale="a [b]reason[/b]")
    lines = proposals.diff(week_db, p, THU)
    text = render_diff(lines, 80)
    rows = text.plain.split("\n")
    assert rows[:4] == [
        f"proposal #{p.id} · organizer",
        "  a [b]reason[/b]",
        "− Thu 11/12  math · learn ×2, geography ×1",
        "+ Thu 11/12  math · learn ×2, geography ×1",
    ]
    assert isinstance(text, Content)  # theme tokens, resolved by Textual: no Rich theme
    struck = [s for s in text.spans if s.style == "strike"]
    assert [text.plain[s.start : s.end] for s in struck] == ["math · learn ×2"]
    assert struck[0].start == text.plain.index("+ Thu") + 2 + len("Thu 11/12  ")
    assert _styles_of(text, f"proposal #{p.id} · organizer") == {"bold"}
    assert _styles_of(text, "  a [b]reason[/b]") == {"$foreground-muted"}
    assert _styles_of(text, "− ") == {"$foreground-muted"}
    old = "Thu 11/12  math · learn ×2, geography ×1"
    assert _styles_of(text, old) == {"$foreground-muted strike"}
    assert _styles_of(text, "+ Thu 11/12") == {"$accent"}
    styles = " ".join(str(s.style) for s in text.spans)
    assert "#" not in styles and "red" not in styles  # no hex and no red


def test_a_narrow_diff_wraps_each_line_under_its_text(week_db):
    """Wrapped, the rationale and the − and + lines hang two columns in, never at the edge;
    the struck phrase stays struck across the wrap."""
    p = propose_move(week_db, rationale="Thursday is full; Friday has room")
    text = render_diff(proposals.diff(week_db, p, THU), 20)
    rows = text.plain.split("\n")
    assert rows[:5] == [
        f"proposal #{p.id} · organizer",
        "  Thursday is full;",
        "  Friday has room",
        "− Thu 11/12  math ·",
        "  learn ×2,",
    ]
    assert all(cell_len(r) <= 20 and r[:1] in "−+ " and r[1:2] == " " for r in rows[1:])
    struck = [text.plain[s.start : s.end] for s in text.spans if s.style == "strike"]
    assert struck == ["math ·", "learn ×2"]  # the + Thu line's, split by the wrap


def test_chat_bar_spans_the_width_on_every_line():
    bar_text = chat_bar(keymap.footer("proposal"), 40)
    assert isinstance(bar_text, Content)
    assert bar_text.plain.rstrip() == "y keep · n discard" and cell_len(bar_text.plain) == 40
    assert _styles_of(bar_text, "y") == {"$foreground on $panel", "$accent"}
    assert _styles_of(bar_text, bar_text.plain, " ") == {"$foreground on $panel"}  # last cell
    long = chat_bar(Content("a long line " * 5), 20)
    rows = long.plain.split("\n")
    assert len(rows) > 1 and {cell_len(x) for x in rows} == {20}
