# Terminal-native design package

Phase 1 of the front-end redesign, design only. The direction is **B, terminal-native**. It is keyboard-first and monospace-forward, with the command palette at the centre, so the web console and the Super-agent TUI look and behave like one product. No product code changes here. The goal and its acceptance bar are in `inbox/dispatch-iter2-design.md`.

## What is in it

| Path | What it is |
| --- | --- |
| `system/SPEC.md` | The one contract for both surfaces: colours for dark (primary) and light with WCAG ratios, 256-colour mapping, type, space, motion, the seven states, the keyboard model and the palette grammar |
| `system/tokens.json` | The machine copy of the same set. Every colour on either surface comes from here (25 hex values) |
| `web/palette.html` | The Ctrl+K palette over Home: fuzzy matching, argument completion, the keyboard model. Interactive |
| `web/home.html` | The needs-you inbox and the running work |
| `web/tickets.html` | The filters and all seven states, in en and zh-CN |
| `web/dispatch.html` | Lines L1–L4, models, review verdicts, and approve/reject |
| `web/ticket.html` | The conversation, worker progress, the report and the git delivery check |
| `web/README.md` | How the mockups are built, the keys, CSS variable naming, and the checks |
| `web/check_mockups.py`, `web/check_palette.js` | Mockup checks: every hex is in tokens.json, nothing external, contrast. Headless palette behaviour checks |
| `tui/SPEC.md` | Every TUI screen in the new style, the layout contract at 40/80/120 (and 100), the TUI palette, the keys, zh-CN |
| `tui/tui_mock.py` | Renders all screens with `rich` (stdlib + rich only), runs 54 contract checks, and writes the frames |
| `tui/tui-<screen>.{svg,txt}` | The 100×32 primary renders (English) |
| `tui/tui-<screen>-{en,zh-CN}-w{40,80,120}.{svg,txt}` | Every screen at three widths in both languages. Screens: home, jobs, dispatch, review, running, completion, completion-args, permission |
| `plan/AUDIT.md` | The current web routes, components and TUI screens, where styles live, ad-hoc colour counts, and the top 15 problems with file:line |
| `plan/PLAN.md` | 30 implementation slices (S01–S30, rounds F1–F6). Each has a goal ≤ 800 characters, its own files, tests and acceptance, and there is an ownership matrix |

## How to open it

- Web: `open docs/design/terminal-native/web/palette.html` (or any of the five). They work offline with no build. Add `?theme=light` or `?theme=dark`, or `?reduced=1` for reduced motion. The top bar links the pages. On palette.html, press Ctrl+K and type `dsp`, `DSP`, `jobs` or `approve `.
- TUI: open the `.svg` files in a browser, or `cat` the `.txt` files. To regenerate them and rerun the contract: `python3 docs/design/terminal-native/tui/tui_mock.py`. It needs `rich` and exits non-zero if a check fails.
- Checks: `python3 docs/design/terminal-native/web/check_mockups.py` and `node docs/design/terminal-native/web/check_palette.js`.

## Key decisions

1. **One token set.** It is the existing TUI theme colours, extended. Dark is primary. Body text is at least 4.5:1 and the focus ring at least 3:1, with every pair listed. Each state always has a colour, a glyph and a label (○ ● ▣ ◆ ✖ ✔ ⊘, plus zh-CN labels 排队中 … 已归档).
2. **One palette, one matcher.** Web Ctrl+K, TUI Ctrl+K and TUI `/` at column 0 all use the same case-insensitive fuzzy subsequence matcher. It matches the canonical name, the slash name and aliases. Results rank by exact, then prefix, then gap cost, then start index, then name. The shipped case-sensitive prefix `SlashCompleter` is replaced (plan S04).
3. **Argument completion** is a unique prefix of an id or name, not fuzzy. Tab after `dispatch ` offers dispatch ids. `approve` and `reject` offer only `awaiting_human` ids and never run without one (the hint reads "approve needs a dispatch id" / "approve 需要一个 dispatch id").
4. **Keys.** Ctrl+K opens the palette. Escape closes it and returns focus to the opener. Tab accepts a completion inside the palette. j/k move in lists but are typed in text fields. Alt+N is navigation mode. Ctrl+↓/↑ move between query and results.
5. **Shift+Tab conflict resolved.** Shift+Tab is always the previous focus stop, on both surfaces, because browsers and assistive tech need it. The TUI's mode cycle (auto, dispatch, read-only) moves from Shift+Tab to **Alt+M**, and the hint changes to `alt+m mode`. This changes shipped TUI behaviour (plan S10).
6. **Newline.** Shift+Enter, then Ctrl+J, then Esc followed quickly by Enter (TUI). All of them are off while a permission is pending, when a/s/d/Esc answer it instead.
7. **The TUI holds at 40 columns.** Rows stack into an identity line and a meta line instead of dropping columns, and no prose loses a character (CJK counts as 2 cells, and wrapping accounts for the rail and prefix).
8. **Plan order.** Tokens come first (S01 web Tailwind 4 theme, S02 `tui_theme.py`). Next is the web palette as its own slice (S03, sole owner of the Ctrl+K listener and of `App.tsx`), then the TUI palette engine and the TUI screens on new modules. `tui.py` has a single owner (S10), and then one slice per page. Locale strings go in per-slice files until a final sweep (S30).

## Open questions for the owner

- **TUI 40-column floor.** `MIN_TUI_COLUMNS` is still 80. The new renderers hold at 40, but lowering the floor is your call.
- **Alt+M for the mode cycle.** Please confirm this is acceptable, or pick a different chord. Also decide whether to add a palette command `mode <auto|dispatch|read-only>`.
- **zh-CN keyword search in the palette.** Should typing Chinese (e.g. 派发) match commands through their localized hint? Today only the English names and aliases match.
- **Unsliced TUI areas.** The running-stream restyle (`tui_blocks.py`, `tui_render.py`, `tui_rich.py`) has no slice yet. It needs a follow-up after S10.
- **Screenshot baselines.** Playwright screenshots are review artefacts, not pixel baselines. Do you want baselines?
- **Hints added beyond the brief.** The TUI added the hints "reply needs text" and "review needs an argument". Keep them or drop them?
- **S24 size.** S24 (Plugins) is large. Do you want to split it?

## Decisions (2026-11-10)

The owner delegated the open questions to the Planner; these are the answers the F rounds build on.

1. **TUI width floor:** keep `MIN_TUI_COLUMNS = 80` for now; lower it to 40 in a small slice after S10, once the layout contract passes against the real `tui.py`.
2. **Mode key:** Alt+M cycles the mode, and the palette also gets a `mode <auto|dispatch|read-only>` command as the fallback (macOS terminals send Alt+M only with "Option as Meta").
3. **Chinese search:** typing zh-CN (e.g. 派发) finds commands through an explicit zh-CN keyword table, not by matching hint text; added after S04.
4. **Running-ticket stream restyle:** a new slice S31 after S10 owns `tui_blocks.py`, `tui_render.py` and `tui_rich.py`.
5. **Screenshots:** Playwright screenshots stay review material; no pixel baselines for now (revisit after S30).
6. **TUI hints:** keep "reply needs text" and "review needs an argument".
7. **S24 (Plugins):** split into smaller slices before it is dispatched.
8. **Dispatch ids:** every surface shows the CLI's 8-hex short id.
9. **TUI location:** the TUI lives in `libs/ptc-cli` today, which is fork lineage; the F rounds change it in place, and the PTC retirement (R9b) moves it to `src/conductor_cli/tui` and points it at `/api/v1/autonomy`.

## Open first

1. `web/palette.html`: the centre of the design, interactive.
2. `tui/tui-home-en-w80.svg` and `tui/tui-home-zh-CN-w40.txt`: the same product in a terminal.
3. `web/dispatch.html`, then `web/home.html`.
4. `system/SPEC.md`: sections Keyboard and Palette grammar.
5. `plan/PLAN.md`: the Order table and slices S01–S10.
