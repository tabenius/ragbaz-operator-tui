#!/usr/bin/env python3
"""ragbaz-tui — the fleet's terminal entry point.

Starts the other TUIs in the workspace, and is the first surface to conform to
the ragbaz design language. It provides the six behaviours a conformant renderer
provides, and nothing more is asked of curses:

    layout(width, height) -> regions      the frame and its degradation ladder
    resolve(role, colour_mode) -> bytes   truecolor -> ansi256 -> ansi16 -> none
    glyph(key) -> str                     narrowed nerd -> unicode -> ascii
    bindings() -> table                   one table, shared with the legend
    resolve_key(key, editing) -> action   with field-scope precedence
    truncate(text, width) -> str          escape- and width-safe

Stdlib only, like frog. It imports the design system's reference colour ladder
rather than re-deriving nearest-256, as the renderer-adapter skill requires.
"""

from __future__ import annotations

import curses
import os
import pathlib
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field

HERE = pathlib.Path(__file__).resolve().parent
DESIGN = pathlib.Path(os.environ.get("RAGBAZ_DESIGN_SYSTEM", HERE.parent / "design-system"))
if not (DESIGN / "tokens.toml").exists():
    DESIGN = HERE.parent / "design-system"

sys.path.insert(0, str(DESIGN / "examples"))
try:
    import tokens as DS  # noqa: E402  the shared ladder
except ImportError:  # pragma: no cover - the design system is required
    sys.exit(f"ragbaz-tui: design system not found at {DESIGN} (set RAGBAZ_DESIGN_SYSTEM)")

TOKENS = tomllib.loads((DESIGN / "tokens.toml").read_text())
THEMES = tomllib.loads((DESIGN / "themes.toml").read_text())
REGISTRY = tomllib.loads((HERE / "tuis.toml").read_text())

GLOBAL = "*"
ROLE_ORDER = [r["name"] for r in TOKENS["role"]]


# --------------------------------------------------------------- behaviours --

def glyph_mode() -> str:
    """nerd is opt-in, never detected; then narrow unicode -> ascii."""
    if os.environ.get("RAGBAZ_NERD_FONT"):
        return "nerd"
    enc = (sys.stdout.encoding or "").lower().replace("-", "")
    return "unicode" if enc in ("utf8", "utf16", "utf32") else "ascii"


def g(key: str) -> str:
    return TOKENS["glyphs"]["set"][key][glyph_mode()]


def color_mode() -> str:
    if os.environ.get("NO_COLOR") and not os.environ.get("FORCE_COLOR"):
        return "none"
    if os.environ.get("TERM") == "dumb":
        return "none"
    return "ansi256" if curses.has_colors() else "none"


def _strip(text: str) -> str:
    import re
    return re.sub(r"\033\[[0-9;]*m", "", text)


def visible(text: str) -> str:
    """Curses colours with attributes, so text reaching it must be plain."""
    return re.sub(r"\033\[[0-9;]*m", "", text)


def truncate(text: str, width: int) -> str:
    """Clip to `width` visible cells. Escape-safe by construction: escapes are
    removed first, so a split can never land inside one."""
    if width <= 0:
        return ""
    return visible(text)[:width]


def clip(text: str, width: int) -> str:
    """truncate, but visibly. "not installed" clipped to 9 cells reads as
    "not install" - a different, wrong value. Fields that carry meaning get an
    ellipsis so a clipped value is never mistaken for a complete one."""
    if width <= 0:
        return ""
    t = visible(text)
    if len(t) <= width:
        return t
    mark = g("ellipsis")
    return (t[:width - len(mark)] + mark) if width > len(mark) else t[:width]


# ------------------------------------------------------------------ binding --

@dataclass(frozen=True, slots=True)
class Key:
    action: str
    keys: tuple[str, ...]
    help: str
    view: str = GLOBAL
    scope: str = "view"  # global | view | nav | field


BINDINGS: list[Key] = [
    Key("help", ("?",), "this legend", GLOBAL, "global"),
    Key("view.next", ("tab",), "next view", GLOBAL, "global"),
    Key("config", ("c",), "configuration", GLOBAL, "global"),
    Key("refresh", ("r",), "re-probe every entry", GLOBAL, "global"),
    Key("quit", ("q",), "quit", GLOBAL, "global"),
    Key("tui.select", ("j", "down"), "next entry", "tuis", "nav"),
    Key("tui.select", ("k", "up"), "previous entry", "tuis", "nav"),
    Key("tui.launch", ("enter", "l"), "start the selected TUI", "tuis", "nav"),
    Key("tui.details", ("d",), "more about the selected entry", "tuis", "view"),
    Key("modal.confirm", ("y",), "confirm", GLOBAL, "field"),
    Key("modal.cancel", ("n", "escape"), "cancel", GLOBAL, "field"),
]

VIEWS = [v for v in dict.fromkeys(k.view for k in BINDINGS) if v != GLOBAL]


@dataclass
class State:
    entries: list = field(default_factory=list)
    index: int = 0
    page: int = 0
    page_size: int = 8
    modal: str | None = None      # None | "unavailable" | "nonzero-exit"
    editing: bool = False
    status: str = ""

    def live(self) -> list[Key]:
        live = [k for k in BINDINGS if k.view in (self.view, GLOBAL)]
        if self.modal:
            # a modal is modal: nothing outside it is reachable
            live = [k for k in live if k.scope == "field"]
        elif self.editing:
            live = [k for k in live if k.scope in ("global", "field")]
            live.sort(key=lambda k: 0 if k.scope == "field" else 1)
        else:
            live = [k for k in live if k.scope in ("global", "view", "nav")]
        seen: set[str] = set()
        out = []
        for k in live:
            if any(key in seen for key in k.keys):
                continue
            seen.update(k.keys)
            out.append(k)
        return out

    def resolve(self, key: str) -> Key | None:
        return next((k for k in self.live() if key in k.keys), None)

    @property
    def view(self) -> str:
        return "tuis"

    def clamp(self) -> None:
        self.index = max(0, min(self.index, len(self.entries) - 1))
        pages = max(1, -(-len(self.entries) // self.page_size))
        self.page = max(0, min(self.page, pages - 1))


# -------------------------------------------------------------------- layout --

def addstr(win, y: int, x: int, text: str, attr: int = 0) -> None:
    """Writing the last cell of the last window raises; never die on layout."""
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


@dataclass
class Rect:
    x: int
    y: int
    w: int
    h: int


@dataclass
class Regions:
    title: Rect
    nav: Rect | None
    main: Rect
    detail: Rect | None
    status: Rect
    legend: Rect


def layout(width: int, height: int) -> Regions:
    """The frame from docs/layout.md: chrome heights are counts, not fractions."""
    h, w = height, width
    title = Rect(0, 0, w, 1)
    status = Rect(0, max(0, h - 2), w, 1)
    legend = Rect(0, max(0, h - 1), w, 1)
    content_y, content_h = 1, max(0, h - 3)

    # the degradation ladder, as a pure function of (width, height)
    show_nav = w >= 52
    show_detail = w >= 76
    show_legend = h >= 10

    # Panes are adjacent: separation is drawn with the box glyphs, so no column
    # is reserved for a gutter. Reserving them made the panes sum to w + 2.
    nav_w = max(12, min(24, round(w * 0.20))) if show_nav else 0
    rest = max(0, w - nav_w)
    detail_w = 0
    if show_detail:
        detail_w = max(24, min(48, round(rest * 0.40)))
        if rest - detail_w < 20:      # main has a floor of 20 columns
            detail_w = 0

    main_w = rest - detail_w
    nav = Rect(0, content_y, nav_w, content_h) if nav_w else None
    main = Rect(nav_w, content_y, max(0, main_w), content_h)
    detail = Rect(nav_w + main_w, content_y, detail_w, content_h) if detail_w else None
    if not show_legend:
        legend = Rect(0, max(0, h - 1), 0, 0)
    return Regions(title, nav, main, detail, status, legend)


# --------------------------------------------------------------------- probe --

def probe(entry: dict) -> tuple[bool, str]:
    cmd = entry.get("probe") or entry["command"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=5,
                           cwd=(HERE.parent / entry["cwd_from"]) if entry.get("cwd_from") else None)
        if p.returncode == 0:
            return True, "available"
        return False, f"probe exited {p.returncode}"
    except FileNotFoundError:
        return False, "not installed"
    except subprocess.TimeoutExpired:
        return False, "probe timed out"


@dataclass
class Entry:
    id: str
    title: str
    subtitle: str
    command: list
    kind: str
    project: str
    docs: str
    available: bool
    why: str

    @property
    def cwd(self) -> pathlib.Path | None:
        return None


def load_entries() -> list[Entry]:
    out = []
    for e in REGISTRY["tui"]:
        ok, why = probe(e)
        out.append(Entry(e["id"], e["title"], e["subtitle"], e["command"],
                         e.get("kind", "?"), e.get("project", "?"),
                         e.get("docs", ""), ok, why))
    return out


# --------------------------------------------------------------------- paint --

class Painter:
    """Resolves roles through the shared ladder, then into curses pairs."""

    def __init__(self, win, theme: str = "ragbaz") -> None:
        self.win = win
        self.values = THEMES["themes"][theme].get("role", {})
        self.mode = color_mode()
        self.pairs: dict[str, int] = {}
        if self.mode != "none":
            curses.start_color()
            curses.use_default_colors()
            for i, name in enumerate(ROLE_ORDER, start=1):
                hexv = self.values.get(name)
                if not hexv:
                    continue
                idx = DS.nearest256(hexv) if self.mode == "ansi256" else DS.nearest16(hexv)
                try:
                    curses.init_pair(i, idx, -1)
                    self.pairs[name] = i
                except curses.error:
                    pass

    def attr(self, role: str, *mods: str) -> int:
        a = curses.A_NORMAL
        if "dim" in mods:
            a |= curses.A_DIM
        if "bold" in mods:
            a |= curses.A_BOLD
        if "reverse" in mods:
            a |= curses.A_REVERSE
        pair = self.pairs.get(role)
        return curses.color_pair(pair) | a if pair else a

    def put(self, y: int, x: int, text: str, role: str = "ink.body", *mods: str,
            width: int | None = None) -> None:
        limit = width or (self.win.getmaxyx()[1] - x)
        if limit <= 0:
            return
        addstr(self.win, y, x, truncate(text, limit)[:limit], self.attr(role, *mods))


def bar(stdscr, painter: Painter, r: Rect, left: str, right: str = "") -> None:
    """A bar is left + filler + right and occupies exactly r.w cells."""
    if r.w <= 0 or r.h <= 0:
        return
    left, right = truncate(left, r.w), truncate(right, max(0, r.w - len(left) - 2))
    fill_n = r.w - len(left) - len(right)
    line = left + (g("box_h") * max(0, fill_n)) + right
    addstr(stdscr, r.y, r.x, line[:r.w], painter.attr("ink.invert", "reverse"))


# --------------------------------------------------------------------- modal --

def modal(stdscr, painter: Painter, title: str, body: list[str]) -> None:
    h, w = stdscr.getmaxyx()
    bw, bh = min(w - 4, 66), len(body) + 4
    r = Rect(2, max(1, (h - bh) // 2), bw, bh)
    stdscr.attron(curses.A_REVERSE)
    addstr(stdscr, r.y - 1, r.x - 1, (g("box_tl") + g("box_h") * (bw + 2) + g("box_tr"))[:w], 0)
    addstr(stdscr, r.y + bh, r.x - 1, (g("box_bl") + g("box_h") * (bw + 2) + g("box_br"))[:w], 0)
    for i in range(bh):
        addstr(stdscr, r.y + i, r.x - 1, (g("box_v") + " " * bw + g("box_v"))[:w], 0)
    stdscr.attroff(curses.A_REVERSE)
    painter.put(r.y + 1, r.x, title, "ink.emphasis", "bold", width=bw)
    for i, line in enumerate(body):
        painter.put(r.y + 2 + i, r.x, line, "ink.body", width=bw)


# ---------------------------------------------------------------------- main --

def draw(stdscr, painter: Painter, state: State) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    r = layout(w, h)

    bar(stdscr, painter, r.title, " ragbaz ", THEMES["default_theme"])
    available = sum(1 for e in state.entries if e.available)
    bar(stdscr, painter, r.status,
        f" {available}/{len(state.entries)} available", state.status)

    sel = state.entries[state.index] if state.entries else None

    def put(y: int, x: int, text: str, role: str = "ink.body", *mods, width=None) -> None:
        painter.put(y, x, text, role, *mods, width=width)

    if r.nav:
        put(r.nav.y, r.nav.x, "views", "ink.muted", "dim", width=r.nav.w)
        for i, v in enumerate(VIEWS):
            current = (v == state.view)
            mark = g("branch") if current else " "
            put(r.nav.y + 1 + i, r.nav.x, f"{mark} {v}",
                "ink.emphasis" if current else "ink.muted", width=r.nav.w)

    start = state.page * state.page_size
    for row, e in enumerate(state.entries[start:start + state.page_size]):
        y = r.main.y + row
        if y >= r.main.y + r.main.h:
            break
        selected = state.entries[state.index] is e
        role = "ink.body" if e.available else "ink.faint"
        # columns are derived from main.w, so a narrow terminal clips the least
        # important field (the status) rather than spilling into the detail pane
        mark = g("branch") if selected else " "
        name_w = max(10, min(18, r.main.w - 24))
        kind_w = min(9, max(7, r.main.w - name_w - 16))
        put(y, r.main.x, f"{mark} {truncate(e.title, name_w).ljust(name_w)}", role,
            *("reverse" if selected else ()), width=r.main.x and name_w + 2)
        put(y, r.main.x + name_w + 2, truncate(e.kind, kind_w).ljust(kind_w),
            "ink.muted", width=kind_w)
        put(y, r.main.x + name_w + 2 + kind_w,
            clip(e.why, r.main.w - name_w - kind_w - 2),
            "status.ok" if e.available else "status.warn")

    if r.detail and sel:
        painter.put(r.detail.y, r.detail.x, " about", "ink.muted", "dim", width=r.detail.w)
        for i, (label, value) in enumerate((
            ("id", sel.id), ("project", sel.project),
            ("toolkit", sel.kind), ("status", sel.why),
            ("docs", sel.docs or "-"),
        )):
            put(r.detail.y + 1 + i, r.detail.x, clip(f"{label}: {value}", r.detail.w),
                "ink.muted" if label != "status"
                else ("status.ok" if sel.available else "status.warn"), width=r.detail.w)

    if r.legend.h:
        hints = "  ".join(f"{k.keys[0]} {k.action}" for k in state.live() if k.scope in ("global", "nav"))
        put(r.legend.y, r.legend.x, hints, "ink.faint", "dim", width=r.legend.w)

    if state.modal:
        if state.modal == "unavailable":
            modal(stdscr, painter, "not available", [
                f"{sel.title if sel else '?'} is not installed here.",
                "", f"  {sel.command[0] if sel else ''} - {sel.why if sel else ''}", "",
                "y  confirm anyway    n / escape  cancel",
            ])
        elif state.modal == "nonzero-exit":
            modal(stdscr, painter, "exited non-zero", [
                state.status, "", "n / escape  dismiss",
            ])
    stdscr.refresh()


def launch(entry: Entry) -> int:
    """Hand the terminal over, run the child, take it back."""
    curses.endwin()
    if os.environ.get("TERM"):
        os.environ["TERM"] = os.environ["TERM"]
    cwd = HERE.parent / entry.project if (HERE.parent / entry.project).exists() else None
    try:
        return subprocess.run(entry.command, cwd=cwd).returncode
    except FileNotFoundError:
        return 127
    finally:
        os.environ["RAGBAZ_RESUME"] = "1"


def plain_list(entries: list[Entry]) -> int:
    """No TTY, or TERM=dumb: still useful, and never garbled."""
    for e in entries:
        mark = "ok  " if e.available else "--  "
        print(f"{mark}{e.title:<14} {e.kind:<9} {e.subtitle}")
        print(f"    status: {e.why}   docs: {e.docs}")
    return 0


def run(stdscr) -> int:
    curses.curs_set(0)
    painter = Painter(stdscr)
    state = State(entries=load_entries(), status="")
    while True:
        state.clamp()
        draw(stdscr, painter, state)
        key = stdscr.getch()
        action = state.resolve(chr(key) if 0 < key < 256 else str(key)) if key != -1 else None
        name = action.action if action else None

        if state.modal:
            if key in (ord("n"), 27):
                state.modal = None
            elif key == ord("y") and state.modal == "unavailable":
                state.modal = None
                sel = state.entries[state.index]
                code = launch(sel)
                curses.wrapper(run)
                state.status = f"{sel.title} exited {code}"
                if code:
                    state.modal = "nonzero-exit"
            continue

        if name == "quit":
            return 0
        if name == "tui.select" and "down" in action.keys:
            state.index += 1
        elif name == "tui.select":
            state.index -= 1
        elif name == "tui.launch":
            sel = state.entries[state.index]
            if sel.available:
                code = launch(sel)
                curses.wrapper(run)
                state.status = f"{sel.title} exited {code}"
                if code:
                    state.modal = "nonzero-exit"
            else:
                state.modal = "unavailable"
        elif name == "refresh":
            state.entries = load_entries()
            state.status = "re-probed"


def main() -> int:
    args = [a for a in sys.argv[1:]]
    entries = load_entries()
    if "--list" in args or not sys.stdout.isatty() or os.environ.get("TERM") == "dumb":
        return plain_list(entries)
    curses.wrapper(run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())