"""Subject colours: a stable slot per subject, and the hue for a slot in a dark or light theme."""

import sqlite3

from orrery import schedule

DARK = ("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#9085e9")
LIGHT = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#6250d6")


def slots(conn: sqlite3.Connection) -> dict[str, int]:
    """Each subject's rank by ord across all subjects, so deactivating one repaints none."""
    return {s.name: i for i, s in enumerate(schedule.all_subjects(conn))}


def hex_for(slot: int, dark: bool) -> str | None:
    """The slot's hue; None from the seventh on, which wear the theme's muted text."""
    hues = DARK if dark else LIGHT
    return hues[slot] if 0 <= slot < len(hues) else None
