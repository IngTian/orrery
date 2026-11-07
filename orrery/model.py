"""Data model types for orrery."""

from dataclasses import dataclass
from datetime import date
from typing import Literal

Part = Literal["warmup", "main", "residual"]
PARTS: tuple[Part, ...] = ("warmup", "main", "residual")

Mark = Literal["full", "half", "none"]


@dataclass(frozen=True)
class Block:
    """A time block in the schedule: part, subject, kind, and session count."""

    part: Part
    subject: str
    kind: str
    sessions: int


@dataclass(frozen=True)
class Subject:
    """A subject with its name, label, rank and whether it's in use."""

    name: str
    label: str
    ord: int = 0
    active: bool = True


# Plan: maps dates to ordered tuples of blocks
Plan = dict[date, tuple[Block, ...]]

# Template: maps weekdays (0-6) to ordered tuples of blocks
Template = dict[int, tuple[Block, ...]]
