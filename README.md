# ragbaz-tui

The fleet's terminal entry point. It lists the other TUIs in the workspace and
starts them, and it is the **first surface built to the ragbaz design language** —
so it doubles as the worked example the other surfaces will follow.

```bash
./ragbaz-tui.py           # interactive
./ragbaz-tui.py --list    # plain text; also what you get with no TTY or TERM=dumb
```

`?` legend · `tab` cycle view · `r` re-probe · `enter`/`l` launch ·
`q` quit

## Why it exists

Four unrelated terminal style systems existed here before
[`tabenius/design-system`](https://github.com/tabenius/design-system). This surface provides the six behaviours a
conformant renderer provides, in curses, without asking curses to be anything it
is not:

```
layout(width, height) -> regions      the frame and its degradation ladder
resolve(role, colour_mode) -> bytes   truecolor -> ansi256 -> ansi16 -> none
glyph(key) -> str                     narrowed nerd -> unicode -> ascii
bindings() -> table                   one table, shared with the legend
resolve_key(key, editing) -> action   with field-scope precedence
truncate(text, width) -> str          escape- and width-safe
```

It reads the token and theme files rather than holding any values of its own, and
it imports the design system's reference colour ladder instead of re-deriving
nearest-256 — both required by
[`ragbaz-renderer-adapter`](https://github.com/tabenius/design-system/blob/main/skills/ragbaz-renderer-adapter/SKILL.md).

## Entries are probed, not assumed

[`tuis.toml`](tuis.toml) declares the registry; availability is the result of
*running* each entry point at startup. On this box:

```
ok  Frog           curses    available
--  WeftMark       textual   not installed
--  Nostoi         ratatui   not installed   (requires a cargo build)
--  Minotaur       cli       not installed   (requires a cargo build)
ok  Design system  ansi      available
ok  Design legend  ansi      available
```

A binary that was never built shows as unavailable rather than failing when you
try to launch it, and launching an unavailable entry asks for confirmation rather
than reporting a `FileNotFoundError` traceback.

## Two lessons from building it, both now in the spec

**Panes must sum to exactly the terminal width.** The first implementation
reserved a column between panes for a gutter; the panes then summed to
`width + 2` and the layout ran off the right edge. `docs/layout.md` now states the
rule and the worked table matches the code. `test_panes_sum_to_width` checks it at
seven terminal sizes.

**A clipped value must not look like a complete one.** "not installed" squeezed
into nine cells reads as "not install", which is a different and wrong value.
Meaningful fields are clipped with an explicit ellipsis (`clip()`, distinct from
the plain `truncate()` used for decoration).

## Tests

```bash
python3 tests/test_render.py
```

14 tests, stdlib only. They drive `draw()` against a recording window and assert
the resulting cells: pane widths sum correctly at seven sizes, chrome is one row
each, the degradation ladder fires at the documented thresholds, no write starts
outside the window, no literal escape reaches the screen, the title bar fills its
row exactly, every advertised key is dispatchable in both editing and
non-editing modes, a modal swallows everything but its own keys, and a missing
binary is reported rather than raised.

A pty screenshot is not a substitute for this: curses only writes deltas, so a
captured stream cannot be reassembled into a frame.

## Configuration

Same four layers as the rest of the fleet — defaults, then a TOML file, then
environment, then CLI — resolved through
[`design-system/docs/configuration.md`](https://github.com/tabenius/design-system/blob/main/docs/configuration.md).
`RAGBAZ_NERD_FONT=1` opts into Nerd Font glyphs; `RAGBAZ_DESIGN_SYSTEM` points at
the token files if they are not a sibling of this repository. `NO_COLOR` and
`TERM=dumb` are honoured, and `--list` is the no-TTY path.