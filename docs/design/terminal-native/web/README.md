# Terminal-native web mockups

Five self-contained HTML mockups for the terminal-native web app described in
[`../system/SPEC.md`](../system/SPEC.md) and tokenised in
[`../system/tokens.json`](../system/tokens.json). Each file is one surface: no
build step, no external asset, no font download, no network request. CSS and
JavaScript are inline. The JavaScript is the theme switch, the 100ms spinner,
the shared keyboard chrome (mode chip, composers, list keys) and the
interactive palette on `palette.html`. Everything works offline.

| File | Surface |
| --- | --- |
| `palette.html` | the palette over a dimmed Home: fuzzy filtering over the 28-command idle order, argument completion, and the fuzzy-rank, argument and keyboard specimens |
| `home.html` | the needs-you inbox (including the reply trap in front of the composer), the running work and the composer |
| `tickets.html` | the filter bar and one row per state — all seven, with the light-selection ink rule shown in both palettes |
| `dispatch.html` | lines L1–L4, the provider/model roster, review verdicts, and the `G6 人工` approve/reject decision |
| `ticket.html` | the conversation, a composer, worker progress, the worker report, and the read-only git delivery check |

## Opening them

```bash
open docs/design/terminal-native/web/palette.html
```

Any of the five opens the same way, straight from disk (`file://`). Query
strings:

- `?theme=dark` / `?theme=light` — start in a theme (the top-bar button toggles
  it; dark is primary). Example: `palette.html?theme=light`.
- `?reduced=1` — force the reduced-motion predicate, the same one
  `PTC_REDUCED_MOTION=1` and `TERM=dumb` hold in the TUI: every duration becomes
  0, the palette does not fade, and queued and running keep `○` and `●`.
  `prefers-reduced-motion: reduce` has the same effect.
- `palette.html?q=…` — prefill the palette query (the composers on the other
  pages use `?q=%2F` when `/` is pressed at column 0).

The pages link to each other through the top bar.

## The palette matcher

One matcher drives web Ctrl+K, TUI Ctrl+K and the TUI `/` at column 0.

- **Commands: case-insensitive fuzzy subsequence.** The needle is the query
  with one leading `/` removed, lower-cased. The haystacks are the canonical
  name, the slash name without `/`, and each alias with one leading `/`
  stripped — all lower-cased. A command matches when the needle's characters
  appear in order (greedy leftmost alignment). Rank by first difference: exact
  haystack > haystack starts with the needle > lower gap cost
  (`end - start - (needle - 1)`) > earlier start index > canonical name
  ascending; each command ranks by its best haystack. Empty needle = the idle
  order, unchanged. At most 60 rows.
  `dsp` and `DSP` → dispatch (gap 1) · `Disp` → dispatch first · `jobs` →
  tickets (alias) · `tkt` → tickets · `apv` → approve · `zzz` → no rows, the
  empty state `no command matches · 没有匹配的命令`.
- **Arguments: unique prefix, not fuzzy.** After the command token and a space,
  completion switches to arguments and matches a case-sensitive prefix of an id
  or name (as `_matched_target` does for hex ids). `dispatch ` lists the four
  active dispatch ids (all phases); `dispatch 9` narrows to the one id;
  an ambiguous prefix lists its matches and inserts nothing. `approve ` /
  `reject ` list only ids whose phase is `awaiting_human`. `reply ` offers
  nothing (free text).
- **Tab** accepts the highlighted token, plus a trailing space when the form
  still needs an argument; it never leaves the palette. **Enter** runs a
  complete form; on an incomplete form it inserts the highlighted argument
  instead, and when nothing is offered the row shows the error hint —
  `approve` / `reject` never run without an id:
  `approve needs a dispatch id · approve 需要一个 dispatch id`.
- **Mouse:** clicking a command row does exactly what Enter does on that row;
  clicking an argument row inserts that argument (Tab). One delegated listener
  reads the row index from the element, so every row activates itself.

## Keys

| Key | Where | Action |
| --- | --- | --- |
| `Ctrl+K` | anywhere outside the palette | open the palette (other pages go to `palette.html`) |
| `/` | composer, caret at column 0 | open the palette prefilled with `/` |
| `Esc` | palette | close, clear the query, return focus to the opener |
| `↓` / `↑` | palette query | move the highlight |
| `j` / `k` | result list, other lists, transcript | next / previous row; in a text field they are characters |
| `Alt+N` | palette | toggle navigation mode (chrome shows `nav`); then `j`/`k` move |
| `Ctrl+↓` / `Ctrl+↑` | palette | focus query → result list, and back |
| `Tab` | palette | accept the highlighted completion; never leaves the palette |
| `Shift+Tab` | everywhere | previous focus stop — never rebound; inert inside the palette trap |
| `Alt+M` | everywhere | cycle mode auto → dispatch → read-only; the top-bar chip shows it, hint `alt+m mode` (was `shift+tab mode`) |
| `Enter` | composer | send |
| `Shift+Enter` | composer | newline (`Ctrl+J` also) |

Alt+N and Alt+M are handled on `keydown` when Alt is the only modifier and
`code` is `KeyN` / `KeyM`, with `preventDefault`, so a macOS Option character is
not inserted. Every tab stop shows a 2px `ink.focus` ring offset 2px.
Everything is reachable by keyboard alone.

## CSS custom properties

A CSS custom property is the kebab-case of its token path in `tokens.json`,
with the group dropped for colours: `themes.*.surfaces.page` →
`--surface-page`, `themes.*.ink.onAccent` → `--ink-on-accent`,
`themes.*.ink.fg` → `--ink-fg`. The scales follow the entry names in
`space`, `radius`, `border` and `motion` the same way (`--space-3`,
`--radius-overlay`, `--border-focus`, `--duration-quick`). `[data-theme="light"]` redefines the colour properties with
the light hex values. Prose names the token as `tokens.json` spells it
(`ink.onAccent`).

## Checks

```bash
python3 docs/design/terminal-native/web/check_mockups.py   # colours, external refs, themes, motion, contrast, zh-CN data
node docs/design/terminal-native/web/check_palette.js      # matcher + argument completion + clicks, headless
```

`check_mockups.py` (stdlib only) asserts that every `#hex` in the five files is
a hex value in `../system/tokens.json` (case-insensitive), that no page has an
external URL, stylesheet link or script src, that each page has dark and light
themes and honours reduced motion, that each page carries a zh-CN string of
30+ characters, and that text on the accent fill is `ink.onAccent`:
7.47:1 dark (`#0F1115` on `#8B9CFF`), 6.64:1 light (`#FFFFFF` on `#3F4FC8`).
The elevation shadows are `tokens.json`'s own `rgba()` strings, and softer tints
are `color-mix()` of its inks rather than new hex values.

## zh-CN labels

Status marks carry the `tokens.json` `status[].labelZh` next to the English
label: 排队中 运行中 已阻塞 需要你 失败 已完成 已归档 (`tickets.html` table, plus a
zh-CN block of all seven states with long zh-CN titles). `palette.html` adds a
zh-CN idle-list specimen with the TUI's zh-CN hint strings. Command names, slash
names and the matcher do not change with the locale.

Designed at 1440px, usable at 390px: long zh-CN titles and ids wrap
(`overflow-wrap:anywhere`, `min-width:0` on grid/flex children) without a
horizontal page scroll.
