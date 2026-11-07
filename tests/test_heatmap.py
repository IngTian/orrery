"""The completion heatmap on its own: its levels, its ramp, and its cells (■ shaded by the
day's rate, □ planned with none done, · nothing planned, blank before starts or after today),
drawn in a bare app from the days it's given."""

from datetime import date, timedelta

from textual.app import App, ComposeResult
from textual.color import Color

from orrery.tui import heatmap
from orrery.tui.heatmap import ALL, Heatmap

W1, W2 = date(2026, 11, 30), date(2026, 12, 7)
STARTS, TODAY = date(2026, 12, 1), date(2026, 12, 10)  # a Tuesday; a Thursday
SLOTS = {"math": 0, "finance": 1}
DAYS = {
    date(2026, 12, 2): {"math": (1, 4)},  # 25%: level 1
    date(2026, 12, 3): {"math": (2, 4)},  # 50%: 2
    date(2026, 12, 4): {"math": (3, 4)},  # 75%: 3
    date(2026, 12, 5): {"math": (2, 2), "finance": (2, 2)},  # every one: 4
    date(2026, 12, 7): {"math": (0, 2)},  # planned, none done
    date(2026, 12, 8): {"math": (1, 2), "finance": (0, 3)},  # all 1 of 5; math 1 of 2
    date(2026, 12, 10): {"finance": (1, 1)},  # all 4; math nothing planned
}


class _Host(App):
    def compose(self) -> ComposeResult:
        yield Heatmap()


def _lines(w) -> list[str]:
    return [w.render_line(y).text for y in range(w.size.height)]


def _fg(w, x: int, y: int):
    at = 0
    for seg in w.render_line(y):
        if at <= x < at + seg.cell_length:
            return seg.style.color.triplet
        at += seg.cell_length
    raise AssertionError(f"nothing at {x}, {y}")


def _shade(w, toward: str, k: int):
    return Color.parse(heatmap.ramp(toward, w.background_colors[1].hex, k)).rgb


def _muted(w):
    return (w.background_colors[1] + Color.parse(w.app.get_css_variables()["foreground-muted"])).rgb


def test_level_thresholds_and_zero():
    """0 when planned and none done; else the quarter the rate falls in, 1 to 4."""
    assert [heatmap.level(d, 4) for d in range(5)] == [0, 1, 2, 3, 4]
    rates = [heatmap.level(d, 100) for d in (1, 25, 26, 50, 51, 75, 76, 100)]
    assert rates == [1, 1, 2, 2, 3, 3, 4, 4]
    assert heatmap.level(1, 3) == 2 and heatmap.level(2, 3) == 3


def test_ramp_mixes_toward_the_hue():
    """30, 55, 80 and 100% of the way from the background to the hue."""
    bg, hue = "#282828", "#3987e5"
    shades = [Color.parse(heatmap.ramp(hue, bg, k)) for k in (1, 2, 3, 4)]
    assert shades[-1].rgb == Color.parse(hue).rgb
    b, h = Color.parse(bg), Color.parse(hue)
    for shade, share in zip(shades, (0.30, 0.55, 0.80, 1.0), strict=True):
        for got, lo, hi in zip(shade.rgb, b.rgb, h.rgb, strict=True):
            assert abs(got - (lo + (hi - lo) * share)) <= 1


async def test_heatmap_cells_square_dot_blank_and_levels():
    """Weeks across, Mon to Sun down, two columns a day, the months over the weeks they
    start (Nov gives way to Dec, a week on); the legend on the right."""
    async with _Host().run_test(size=(60, 12)) as pilot:
        h = pilot.app.query_one(Heatmap)
        h.show(DAYS, [W1, W2], STARTS, TODAY, ALL, SLOTS)
        await pilot.pause()
        rows = _lines(h)
        assert rows[0].rstrip() == "       Dec"
        assert [row[:9] for row in rows[1:8]] == [
            "Mon    □ ",  # Nov 30 before starts; Dec 7 planned, none done
            "     · ■ ",  # Dec 1 nothing planned
            "Wed  ■ · ",
            "     ■ ■ ",
            "Fri  ■   ",  # Dec 11 after today
            "     ■   ",
            "Sun  ·   ",
        ]
        fg = h.app.get_css_variables()["foreground"]
        for (x, y), k in {(7, 2): 1, (5, 3): 1, (5, 4): 2, (7, 4): 4, (5, 5): 3, (5, 6): 4}.items():
            assert _fg(h, x, y) == _shade(h, fg, k), (x, y)
        assert _fg(h, 7, 1) == _fg(h, 5, 2) == _muted(h)
        assert rows[1][15:].rstrip() == "less ■ ■ ■ ■ more"
        assert [row[15:].rstrip() for row in rows[2:5]] == [
            "",
            "□ planned, none done",
            "· nothing planned",
        ]
        assert [_fg(h, 15 + 5 + 2 * i, 1) for i in range(4)] == [
            _shade(h, fg, k) for k in (1, 2, 3, 4)
        ]

        h.show(DAYS, [W1, W2], STARTS, TODAY, "math", SLOTS)  # the subject's own days
        await pilot.pause()
        rows = _lines(h)
        assert [row[:9] for row in rows[1:8]] == [
            "Mon    □ ",
            "     · ■ ",
            "Wed  ■ · ",
            "     ■ · ",  # Dec 10 planned finance only
            "Fri  ■   ",
            "     ■   ",
            "Sun  ·   ",
        ]
        assert _fg(h, 7, 2) == _shade(h, "#3987e5", 2) and _fg(h, 5, 6) == _shade(h, "#3987e5", 4)
        h.show(DAYS, [W1, W2], STARTS, TODAY, "finance", SLOTS)
        await pilot.pause()
        assert _lines(h)[2][:9] == "     · □ "


async def test_heatmap_narrows_to_a_column_a_week_and_puts_the_legend_under():
    """A year where two columns a day don't fit: one each, and the legend under the grid."""
    weeks = [W2 - timedelta(weeks=52 - i) for i in range(53)]
    async with _Host().run_test(size=(80, 20)) as pilot:
        h = pilot.app.query_one(Heatmap)
        h.show(DAYS, weeks, STARTS, TODAY, ALL, SLOTS)
        await pilot.pause()
        rows = [row.rstrip() for row in _lines(h)]
        assert all(len(row) <= 5 + 53 for row in rows[1:8])
        assert rows[1].endswith("□") and rows[5].endswith("■")  # Dec 7, Dec 4: the last two
        assert rows[8:13] == [
            "",
            "less ■ ■ ■ ■ more",
            "",
            "□ planned, none done",
            "· nothing planned",
        ]
        months = rows[0]
        assert months.startswith("     Dec") and months.count("Nov") == 1  # Nov 2026's
        assert months.index("Jan") - months.index("Dec") == 4  # Dec 8 2025, then Jan 5


async def test_heatmap_recolours_on_theme_change():
    """The ramp is mixed again from the new theme's background and text, and a subject's from
    its light step."""
    async with _Host().run_test(size=(60, 12)) as pilot:
        app = pilot.app
        h = app.query_one(Heatmap)
        h.show(DAYS, [W1, W2], STARTS, TODAY, ALL, SLOTS)
        await pilot.pause()
        was = _fg(h, 5, 6)
        app.theme = "catppuccin-latte"
        await pilot.pause()
        await pilot.pause()
        fg = app.get_css_variables()["foreground"]
        assert _fg(h, 5, 6) == _shade(h, fg, 4) != was
        assert _fg(h, 5, 3) == _shade(h, fg, 1)
        h.show(DAYS, [W1, W2], STARTS, TODAY, "math", SLOTS)
        await pilot.pause()
        assert _fg(h, 5, 3) == _shade(h, "#2a78d6", 1)


async def test_heatmap_subject_past_the_sixth_slot_shades_toward_the_muted_text():
    """A subject from the seventh slot on has no hue: its days are mixed toward the theme's
    muted text, made solid over the background; in an ANSI theme, toward its text."""
    async with _Host().run_test(size=(60, 12)) as pilot:
        app, h = pilot.app, pilot.app.query_one(Heatmap)
        h.show(DAYS, [W1, W2], STARTS, TODAY, "math", {"math": 6})
        await pilot.pause()
        muted = Color(*_muted(h)).hex
        assert _fg(h, 7, 2) == _shade(h, muted, 2) and _fg(h, 5, 6) == _muted(h)
        app.theme = "ansi-dark"
        await pilot.pause()
        await pilot.pause()
        assert [row[:9] for row in _lines(h)[1:3]] == ["Mon    □ ", "     · ■ "]
