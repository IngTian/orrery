"""Settings and config.toml loading for orrery, and the one write: T's theme."""

import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from orrery import paths, store

SEED_SUBJECTS: tuple[tuple[str, str], ...] = ()
SEED_KINDS = ("learn", "rep", "fun", "warmup")

CONFIG_TEMPLATE = """\
# orrery configuration

# Day boundary: the hour (0-23) a new day starts; earlier hours count as the day before.
# Default: 5 (5am).
day_boundary_hour = 5

# Theme: any Textual theme name. T in the app picks one and saves it here. Default: "gruvbox".
# theme = "gruvbox"

[ai]
# Model: "sonnet", "opus", or "haiku". Default: "sonnet".
model = "sonnet"

# Path to the Claude CLI binary. Leave empty to use "claude" from PATH.
claude_path = ""

[seed]
# Seed subjects for orrery init (an array of [name, label] pairs). Default: none.
# subjects = [
#     ["math", "Math"],
#     ["spanish", "Spanish"],
# ]

# Seed kinds for orrery init (an array of strings).
# Default: ["learn", "rep", "fun", "warmup"].
# kinds = ["learn", "rep", "fun", "warmup"]

# Session cap for orrery init (1-9). Default: 4.
session_cap = 4
"""


@dataclass(frozen=True)
class Settings:
    """orrery settings loaded from config.toml."""

    root: Path
    day_boundary_hour: int = 5
    ai_model: str = "sonnet"
    claude_path: str | None = None
    seed_subjects: tuple[tuple[str, str], ...] = SEED_SUBJECTS
    seed_kinds: tuple[str, ...] = SEED_KINDS
    seed_cap: int = 4
    theme: str = "gruvbox"  # tui.themes.DEFAULT; checked against the app's themes at mount


def load_settings(root: Path | None = None) -> Settings | store.Unavailable:
    """Load settings from config.toml.

    Args:
        root: The data root directory. If None, uses paths.data_root().

    Returns:
        Settings instance, or Unavailable if the root or config is missing or invalid.
    """
    if root is None:
        root = paths.data_root()

    if not root.exists():
        return store.Unavailable("root directory does not exist")

    config_path = root / "config.toml"
    text = store.read_text(config_path)
    if isinstance(text, store.Unavailable):
        return text

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return store.Unavailable(f"invalid TOML: {e}")

    # Extract values with defaults
    day_boundary_hour = data.get("day_boundary_hour", 5)
    ai_section = data.get("ai", {})
    seed_section = data.get("seed", {})
    if not isinstance(ai_section, dict) or not isinstance(seed_section, dict):
        return store.Unavailable("[ai] and [seed] must be tables")
    ai_model = ai_section.get("model", "sonnet")
    claude_path = ai_section.get("claude_path", "")
    seed_subjects = seed_section.get("subjects", list(SEED_SUBJECTS))
    seed_kinds = seed_section.get("kinds", list(SEED_KINDS))
    seed_cap = seed_section.get("session_cap", 4)

    # Validate day_boundary_hour
    if not isinstance(day_boundary_hour, int) or not (0 <= day_boundary_hour <= 23):
        return store.Unavailable("day_boundary_hour must be an integer 0-23")

    # Validate seed_cap
    if not isinstance(seed_cap, int) or not (1 <= seed_cap <= 9):
        return store.Unavailable("seed.session_cap must be an integer 1-9")

    # Convert claude_path: empty string becomes None
    if claude_path == "":
        claude_path = None

    # Convert subjects to tuple of tuples
    try:
        seed_subjects_tuple = tuple(tuple(pair) for pair in seed_subjects)
    except (TypeError, ValueError):
        return store.Unavailable("seed.subjects must be an array of [name, label] pairs")

    # Convert kinds to tuple
    try:
        seed_kinds_tuple = tuple(seed_kinds)
    except TypeError:
        return store.Unavailable("seed.kinds must be an array of strings")

    return Settings(
        root=root,
        day_boundary_hour=day_boundary_hour,
        ai_model=ai_model,
        claude_path=claude_path,
        seed_subjects=seed_subjects_tuple,
        seed_kinds=seed_kinds_tuple,
        seed_cap=seed_cap,
        # Never Unavailable over a cosmetic setting: an unknown name falls back at mount.
        theme=str(data.get("theme") or Settings.theme),
    )


_TABLE_HEADER = re.compile(r"^\s*\[")


def update_config(path: Path, values: dict[str, str]) -> None:
    """Set top-level string keys in config.toml, leaving the rest of the file alone.

    Edited as text, since tomllib can't write and a round-trip would drop comments. A new key
    goes before the first table header: a scalar written after `[ai]` would be a field of
    that table and never read, and the file would still parse.
    """
    path = Path(os.path.realpath(path))  # a dotfile manager's symlink stays a link
    text = store.read_text(path)
    # On "\n" alone: splitlines also breaks at U+2028, U+2029 and U+0085, which a TOML string
    # may hold, and rejoined with "\n" that string would end mid-value.
    missing = isinstance(text, store.Unavailable) or not text
    lines = [] if missing else text.removesuffix("\n").split("\n")
    limit = next((i for i, line in enumerate(lines) if _TABLE_HEADER.match(line)), len(lines))

    pending: list[str] = []
    for key, value in values.items():
        rendered = f"{key} = {json.dumps(value, ensure_ascii=False)}"  # a TOML basic string
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
        for i in range(limit):
            if pattern.match(lines[i]):
                lines[i] = rendered
                break
        else:
            pending.append(rendered)

    if pending:
        if limit < len(lines) and lines[limit].strip():
            pending.append("")  # a blank line before the table, as a person would write it
        lines[limit:limit] = pending

    # Atomic, as this runs on a keystroke; refuses (RefusedWrite) to replace an unreadable file.
    store.atomic_write(path, "\n".join(lines).rstrip("\n") + "\n", backup=False)
