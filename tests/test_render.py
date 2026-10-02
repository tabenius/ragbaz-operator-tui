#!/usr/bin/env python3
"""Conformance tests for ragbaz-tui.

Driving draw() against a recording window is more precise than a pty screenshot:
curses only writes deltas, so a captured stream cannot be reassembled into a
frame, whereas recorded addstr() calls can be checked cell by cell.

Stdlib only. Run: python3 tests/test_render.py
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("hub", ROOT / "ragbaz-tui.py")
hub = importlib.util.module_from_spec(spec)
sys.modules["hub"] = hub  # dataclasses(slots=True) needs the module registered
spec.loader.exec_module(hub)


class FakeWindow:
    """Records writes and reconstructs a character grid."""

    def __init__(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols
        self.grid = [[" "] * cols for _ in range(rows)]
        self.writes: list[tuple[int, int, str]] = []

    def getmaxyx(self) -> tuple[int, int]:
        return self.rows, self.cols

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        if not (0 <= y < self.rows) or not (0 <= x < self.cols):
            return
        self.writes.append((y, x, text))
        for i, ch in enumerate(text):
            if 0 <= x + i < self.cols:
                self.grid[y][x + i] = ch

    def erase(self) -> None:
        self.grid = [[" "] * self.cols for _ in range(self.rows)]
        self.writes.clear()

    def refresh(self) -> None:
        pass

    def line(self, y: int) -> str:
        return "".join(self.grid[y]).rstrip()


def make_painter(win):
    """Painter without touching the real terminal."""
    import curses

    # colour_pair() also insists on initscr(), so stub every curses entry point
    # the Painter touches rather than initialising a real terminal in a test
    for name, value in (("has_colors", lambda: True), ("start_color", lambda: None),
                        ("use_default_colors", lambda: None),
                        ("init_pair", lambda *a: None),
                        ("color_pair", lambda n: 0)):
        setattr(curses, name, value)
    return hub.Painter(win)


def entries():
    return [
        hub.Entry("frog", "Frog", "", ["frog", "tui"], "curses", "frog", "docs", True, "available"),
        hub.Entry("nostoi", "Nostoi", "", ["nostoi", "tui"], "ratatui", "nostoi", "docs", False, "not installed"),
    ]


class Layout(unittest.TestCase):
    """docs/layout.md: chrome heights are counts, and widths must sum to width."""

    CASES = [(120, 40), (100, 30), (80, 24), (76, 24), (75, 24), (60, 20), (40, 12)]

    def test_panes_sum_to_width(self):
        for w, h in self.CASES:
            with self.subTest(size=f"{w}x{h}"):
                r = hub.layout(w, h)
                panes = sum(x.w for x in (r.nav, r.main, r.detail) if x)
                self.assertEqual(panes, w, "panes overflow or leave a gap")

    def test_no_gutter_columns_are_reserved(self):
        # the trap that made an early version run off the right edge
        r = hub.layout(80, 24)
        self.assertEqual((r.nav.w, r.main.w, r.detail.w), (16, 38, 26))

    def test_chrome_is_one_row_each(self):
        r = hub.layout(80, 24)
        self.assertEqual((r.title.h, r.status.h, r.legend.h), (1, 1, 1))
        self.assertEqual(r.main.h, 21)

    def test_degradation_ladder(self):
        self.assertIsNotNone(hub.layout(76, 24).detail, "detail appears at 76")
        self.assertIsNone(hub.layout(75, 24).detail, "detail drops below 76")
        self.assertIsNone(hub.layout(51, 20).nav, "nav collapses below 52")
        self.assertEqual(hub.layout(80, 8).legend.h, 0, "legend drops below 10 rows")


class Paint(unittest.TestCase):
    def test_truncate_strips_escapes_and_clips_visible_cells(self):
        # a curses renderer colours with attributes, so text must arrive plain
        self.assertEqual(hub.truncate("\033[38;5;12mhello\033[0m", 5), "hello")
        self.assertEqual(hub.truncate("abcdef", 3), "abc")
        self.assertEqual(hub.truncate("abc", 0), "")

    def test_rendered_frame_contains_no_literal_escapes(self):
        win = FakeWindow(24, 80)
        painter = make_painter(win)
        state = hub.State(entries=entries())
        hub.draw(win, painter, state)
        for _, _, text in win.writes:
            self.assertNotIn("\033", text, "an escape reached the screen literally")

    def test_no_write_starts_outside_the_window(self):
        for w, h in [(80, 24), (76, 24), (60, 20), (40, 12), (30, 6)]:
            with self.subTest(size=f"{w}x{h}"):
                win = FakeWindow(h, w)
                painter = make_painter(win)
                hub.draw(win, painter, hub.State(entries=entries()))
                for y, x, _ in win.writes:
                    self.assertTrue(0 <= y < h and 0 <= x < w, f"write at {y},{x} outside {w}x{h}")

    def test_title_bar_fills_its_row_exactly(self):
        win = FakeWindow(24, 80)
        hub.draw(win, make_painter(win), hub.State(entries=entries()))
        bar = win.line(0)
        self.assertTrue(bar.startswith(" ragbaz "), bar)
        self.assertEqual(len(bar), 80, "the bar does not fill the row")
        # between the left label and the right label there is only filler
        left, right = " ragbaz ", hub.THEMES["default_theme"]
        self.assertTrue(bar.endswith(right), bar)
        self.assertEqual(set(bar[len(left):-len(right)]), {hub.g("box_h")}, bar)

    def test_a_clipped_value_is_marked(self):
        # "not installed" squeezed into 9 cells must not read as "not install"
        self.assertEqual(hub.clip("not installed", 9), "not inst" + hub.g("ellipsis"))
        self.assertEqual(hub.clip("available", 9), "available")

    def test_selected_entry_uses_two_signals(self):
        # reverse video AND a marker glyph: reverse alone vanishes in a log
        win = FakeWindow(24, 80)
        painter = make_painter(win)
        hub.draw(win, painter, hub.State(entries=entries()))
        self.assertIn(hub.g("branch"), "".join("".join(r) for r in win.grid))

    def test_entry_status_is_visible_without_colour(self):
        win = FakeWindow(24, 80)
        hub.draw(win, make_painter(win), hub.State(entries=entries()))
        text = "\n".join(win.line(y) for y in range(win.rows))
        self.assertIn("available", text)
        self.assertIn("not installed", text, "status must be words, not colour")


class Bindings(unittest.TestCase):
    def test_every_visible_key_is_dispatchable(self):
        for editing in (False, True):
            state = hub.State(entries=entries(), editing=editing)
            for binding in state.live():
                for key in binding.keys:
                    resolved = state.resolve(key)
                    self.assertIsNotNone(resolved, f"{key} is advertised but unresolvable")
                    self.assertEqual(resolved.action, binding.action)

    def test_modal_swallows_everything_but_its_own_keys(self):
        state = hub.State(entries=entries(), modal="unavailable")
        self.assertIsNone(state.resolve("q"), "a modal that lets you quit is not modal")
        self.assertIsNone(state.resolve("tab"))
        self.assertEqual(state.resolve("y").action, "modal.confirm")
        self.assertEqual(state.resolve("escape").action, "modal.cancel")


class Probe(unittest.TestCase):
    def test_missing_binary_is_reported_not_raised(self):
        ok, why = hub.probe({"probe": ["definitely-not-a-real-binary-xyz", "--help"]})
        self.assertFalse(ok)
        self.assertIn("not installed", why)


if __name__ == "__main__":
    unittest.main(verbosity=2)