# Terminal-native tokens

One token set for the web app and the Super-agent TUI. Dark is the primary theme. Light has its own hex values. `tokens.json` in this directory is the machine copy of the same set.

Ink and the three body grounds are the TUI theme in `libs/ptc-cli/ptc_cli/superagent/tui_theme.py` (`_DARK`, `_LIGHT`, `_DARK_GROUNDS`, `_LIGHT_GROUNDS`). Selection fills are that file's `selection_bg`. The slash menu this palette mirrors is `SLASH_COMMANDS` in `libs/ptc-cli/ptc_cli/superagent/tui_completion.py`.

## Themes

`resolve_theme` picks the theme.

| Input | Theme |
| --- | --- |
| `CONDUCTOR_THEME=dark` | dark |
| `CONDUCTOR_THEME=light` | light |
| `CONDUCTOR_THEME` unset, `COLORFGBG` background not 7 or 15 | dark |
| `COLORFGBG` background 7 or 15 | light |
| `NO_COLOR` set, or `CONDUCTOR_THEME` of `none`, `no-color`, `plain` | no colour; glyphs stay |
| `CONDUCTOR_THEME=ansi16` (aliases `16`, `ansi`) | the 16-colour names below |
| any other `CONDUCTOR_THEME` | auto, as if unset |

An unknown `CONDUCTOR_THEME` falls through to auto. With no `COLORFGBG` sample, auto is dark.

## Colour

Surfaces are the grounds text sits on. `canvas` is the second ground from the TUI (`#1E1E1E` dark, `#F5F4F1` light). `accent` is a fill, and the only text on it is `ink.onAccent`.

### Dark

| Token | Hex | Role |
| --- | --- | --- |
| surface.page | #0F1115 | app ground |
| surface.canvas | #1E1E1E | card and input ground |
| surface.elevated | #282C34 | menu and palette ground |
| surface.selection | #1A2140 | selected row |
| surface.accent | #8B9CFF | accent fill, same hex as baton |
| ink.fg | #E6E8EE | body |
| ink.muted | #9AA3B5 | secondary body |
| ink.baton | #8B9CFF | accent, commands, focus |
| ink.tempo | #56C7D9 | running |
| ink.ok | #5BD38A | completed |
| ink.hold | #F2B544 | blocked and needs you |
| ink.error | #FF7A7A | failed |
| ink.onAccent | #0F1115 | text on the accent fill |
| ink.focus | #8B9CFF | focus ring, same hex as baton |
| ink.faint | #2A2F3A | decorative, not text |
| ink.rule | #3A4150 | hairline, not text |

### Light

| Token | Hex | Role |
| --- | --- | --- |
| surface.page | #FFFFFF | app ground |
| surface.canvas | #F5F4F1 | card and input ground |
| surface.elevated | #FDF6E3 | menu and palette ground |
| surface.selection | #DCE3FF | selected row |
| surface.accent | #3F4FC8 | accent fill, same hex as baton |
| ink.fg | #1B1E26 | body |
| ink.muted | #5B6272 | secondary body |
| ink.baton | #3F4FC8 | accent, commands, focus |
| ink.tempo | #0E7A8C | running |
| ink.ok | #1A7F37 | completed |
| ink.hold | #9A4A06 | blocked and needs you |
| ink.error | #C93838 | failed |
| ink.onAccent | #FFFFFF | text on the accent fill |
| ink.focus | #3F4FC8 | focus ring, same hex as baton |
| ink.faint | #C9CCD4 | decorative, not text |
| ink.rule | #C9CCD4 | hairline, not text |

`ink.focus` has the baton hex. On the web it is a 2px solid ring, offset 2px. In the TUI the focused row is bold and filled with `surface.selection`, and the palette draws a one-cell border in `ink.focus`.

## Contrast

WCAG 2 relative luminance. Each sRGB channel is divided by 255, then linearized (`c / 12.92` at or below 0.04045, otherwise `((c + 0.055) / 1.055) ^ 2.4`). Luminance is `0.2126 R + 0.7152 G + 0.0722 B`. The ratio is `(Llighter + 0.05) / (Ldarker + 0.05)`.

Body text on a surface it is allowed to use is at least 4.5:1. The focus ring against every surface, including selection, is at least 3:1. Ratios below are rounded to two decimal places from the unrounded value stored in `tokens.json`. A pair is legal only when the unrounded value meets the floor. The Ink and Surface columns are the `ink` and `surface` fields of `contrast.pairs`: the token name without its `ink.` or `surface.` prefix, so `onAccent` is `ink.onAccent` and `focus` is `ink.focus`.

The lowest legal body pair in dark is `muted` on `elevated` at 5.52:1. The lowest legal body pair in light is `tempo` on `canvas` at 4.57:1. The lowest focus pair is `light` `focus` on `selection` at 5.21:1.

A light selected row paints `tempo`, `ok`, and `error` in `fg`. Those three pairs are under 4.5:1 on `#DCE3FF`. The glyph stays. Dark selection clears every body ink, so a dark selected row keeps the status colour.

`faint` and `rule` are hairlines and empty-state texture. They are not text. Their ratios sit below 4.5:1 and are listed under `contrast.decorative` in `tokens.json`. A control is identified by its label and, when focused, by the focus ring.

### Dark body and focus

| Ink | Surface | Ink hex | Surface hex | Ratio | Legal |
| --- | --- | --- | --- | --- | --- |
| fg | page | #E6E8EE | #0F1115 | 15.42 | yes |
| fg | canvas | #E6E8EE | #1E1E1E | 13.61 | yes |
| fg | elevated | #E6E8EE | #282C34 | 11.43 | yes |
| fg | selection | #E6E8EE | #1A2140 | 12.84 | yes |
| muted | page | #9AA3B5 | #0F1115 | 7.45 | yes |
| muted | canvas | #9AA3B5 | #1E1E1E | 6.57 | yes |
| muted | elevated | #9AA3B5 | #282C34 | 5.52 | yes |
| muted | selection | #9AA3B5 | #1A2140 | 6.20 | yes |
| baton | page | #8B9CFF | #0F1115 | 7.47 | yes |
| baton | canvas | #8B9CFF | #1E1E1E | 6.59 | yes |
| baton | elevated | #8B9CFF | #282C34 | 5.53 | yes |
| baton | selection | #8B9CFF | #1A2140 | 6.21 | yes |
| tempo | page | #56C7D9 | #0F1115 | 9.51 | yes |
| tempo | canvas | #56C7D9 | #1E1E1E | 8.39 | yes |
| tempo | elevated | #56C7D9 | #282C34 | 7.04 | yes |
| tempo | selection | #56C7D9 | #1A2140 | 7.91 | yes |
| ok | page | #5BD38A | #0F1115 | 10.01 | yes |
| ok | canvas | #5BD38A | #1E1E1E | 8.84 | yes |
| ok | elevated | #5BD38A | #282C34 | 7.42 | yes |
| ok | selection | #5BD38A | #1A2140 | 8.33 | yes |
| hold | page | #F2B544 | #0F1115 | 10.32 | yes |
| hold | canvas | #F2B544 | #1E1E1E | 9.10 | yes |
| hold | elevated | #F2B544 | #282C34 | 7.65 | yes |
| hold | selection | #F2B544 | #1A2140 | 8.59 | yes |
| error | page | #FF7A7A | #0F1115 | 7.48 | yes |
| error | canvas | #FF7A7A | #1E1E1E | 6.60 | yes |
| error | elevated | #FF7A7A | #282C34 | 5.54 | yes |
| error | selection | #FF7A7A | #1A2140 | 6.23 | yes |
| onAccent | accent | #0F1115 | #8B9CFF | 7.47 | yes |
| focus | page | #8B9CFF | #0F1115 | 7.47 | yes |
| focus | canvas | #8B9CFF | #1E1E1E | 6.59 | yes |
| focus | elevated | #8B9CFF | #282C34 | 5.53 | yes |
| focus | selection | #8B9CFF | #1A2140 | 6.21 | yes |

### Light body and focus

| Ink | Surface | Ink hex | Surface hex | Ratio | Legal |
| --- | --- | --- | --- | --- | --- |
| fg | page | #1B1E26 | #FFFFFF | 16.66 | yes |
| fg | canvas | #1B1E26 | #F5F4F1 | 15.15 | yes |
| fg | elevated | #1B1E26 | #FDF6E3 | 15.45 | yes |
| fg | selection | #1B1E26 | #DCE3FF | 13.07 | yes |
| muted | page | #5B6272 | #FFFFFF | 6.11 | yes |
| muted | canvas | #5B6272 | #F5F4F1 | 5.56 | yes |
| muted | elevated | #5B6272 | #FDF6E3 | 5.67 | yes |
| muted | selection | #5B6272 | #DCE3FF | 4.80 | yes |
| baton | page | #3F4FC8 | #FFFFFF | 6.64 | yes |
| baton | canvas | #3F4FC8 | #F5F4F1 | 6.03 | yes |
| baton | elevated | #3F4FC8 | #FDF6E3 | 6.15 | yes |
| baton | selection | #3F4FC8 | #DCE3FF | 5.21 | yes |
| tempo | page | #0E7A8C | #FFFFFF | 5.02 | yes |
| tempo | canvas | #0E7A8C | #F5F4F1 | 4.57 | yes |
| tempo | elevated | #0E7A8C | #FDF6E3 | 4.66 | yes |
| tempo | selection | #0E7A8C | #DCE3FF | 3.94 | no |
| ok | page | #1A7F37 | #FFFFFF | 5.08 | yes |
| ok | canvas | #1A7F37 | #F5F4F1 | 4.62 | yes |
| ok | elevated | #1A7F37 | #FDF6E3 | 4.71 | yes |
| ok | selection | #1A7F37 | #DCE3FF | 3.98 | no |
| hold | page | #9A4A06 | #FFFFFF | 6.26 | yes |
| hold | canvas | #9A4A06 | #F5F4F1 | 5.69 | yes |
| hold | elevated | #9A4A06 | #FDF6E3 | 5.80 | yes |
| hold | selection | #9A4A06 | #DCE3FF | 4.91 | yes |
| error | page | #C93838 | #FFFFFF | 5.11 | yes |
| error | canvas | #C93838 | #F5F4F1 | 4.65 | yes |
| error | elevated | #C93838 | #FDF6E3 | 4.74 | yes |
| error | selection | #C93838 | #DCE3FF | 4.01 | no |
| onAccent | accent | #FFFFFF | #3F4FC8 | 6.64 | yes |
| focus | page | #3F4FC8 | #FFFFFF | 6.64 | yes |
| focus | canvas | #3F4FC8 | #F5F4F1 | 6.03 | yes |
| focus | elevated | #3F4FC8 | #FDF6E3 | 6.15 | yes |
| focus | selection | #3F4FC8 | #DCE3FF | 5.21 | yes |

## Truecolour and 256-colour

Truecolour terminals and the web use the hex.

A 256-colour terminal uses the xterm palette:

- 0–15 are the ANSI colours `(0,0,0)`, `(128,0,0)`, `(0,128,0)`, `(128,128,0)`, `(0,0,128)`, `(128,0,128)`, `(0,128,128)`, `(192,192,192)`, `(128,128,128)`, `(255,0,0)`, `(0,255,0)`, `(255,255,0)`, `(0,0,255)`, `(255,0,255)`, `(0,255,255)`, `(255,255,255)`.
- 16–231 are the 6×6×6 cube. A channel is `0` then `95, 135, 175, 215, 255`. The index is `16 + 36*r + 6*g + b`.
- 232–255 are gray `8 + 10*(index − 232)`.

The published index is the palette entry with the smallest sum of squared 8-bit RGB deltas whose contrast against every mapped surface that token may sit on still meets the floor (4.5 for body ink, 3 for the focus ring). The surface in that test is the surface's own 256-colour entry. A tie takes the lower index. Decorative `faint` and `rule` use the geometric nearest entry and have no floor. `onAccent` is measured against the 256-colour baton ink.

The palette hex is what the terminal draws. It is an approximation of the truecolour hex.

### Dark

| Token | Truecolour | 256 | Palette hex |
| --- | --- | --- | --- |
| surface.page | #0F1115 | 233 | #121212 |
| surface.canvas | #1E1E1E | 234 | #1C1C1C |
| surface.elevated | #282C34 | 236 | #303030 |
| surface.selection | #1A2140 | 235 | #262626 |
| ink.fg | #E6E8EE | 255 | #EEEEEE |
| ink.muted | #9AA3B5 | 248 | #A8A8A8 |
| ink.baton | #8B9CFF | 111 | #87AFFF |
| ink.tempo | #56C7D9 | 80 | #5FD7D7 |
| ink.ok | #5BD38A | 78 | #5FD787 |
| ink.hold | #F2B544 | 215 | #FFAF5F |
| ink.error | #FF7A7A | 210 | #FF8787 |
| ink.focus | #8B9CFF | 111 | #87AFFF |
| ink.onAccent | #0F1115 | 233 | #121212 |
| ink.faint | #2A2F3A | 236 | #303030 |
| ink.rule | #3A4150 | 238 | #444444 |

### Light

| Token | Truecolour | 256 | Palette hex |
| --- | --- | --- | --- |
| surface.page | #FFFFFF | 15 | #FFFFFF |
| surface.canvas | #F5F4F1 | 255 | #EEEEEE |
| surface.elevated | #FDF6E3 | 230 | #FFFFD7 |
| surface.selection | #DCE3FF | 189 | #D7D7FF |
| ink.fg | #1B1E26 | 234 | #1C1C1C |
| ink.muted | #5B6272 | 59 | #5F5F5F |
| ink.baton | #3F4FC8 | 25 | #005FAF |
| ink.tempo | #0E7A8C | 24 | #005F87 |
| ink.ok | #1A7F37 | 23 | #005F5F |
| ink.hold | #9A4A06 | 58 | #5F5F00 |
| ink.error | #C93838 | 125 | #AF005F |
| ink.focus | #3F4FC8 | 62 | #5F5FD7 |
| ink.onAccent | #FFFFFF | 15 | #FFFFFF |
| ink.faint | #C9CCD4 | 252 | #D0D0D0 |
| ink.rule | #C9CCD4 | 252 | #D0D0D0 |

On the light theme, `ink.baton` and `ink.focus` share `#3F4FC8` and use different 256-colour indexes. Baton is body text, so its index keeps 4.5:1, including on selection. The focus ring keeps 3:1, and index 62 is the nearer entry that still clears that floor. Dark baton and dark focus both land on index 111.

`ansi16` uses prompt_toolkit names and does not claim a WCAG ratio: `fg` default, `muted` and `faint` and `rule` `ansibrightblack`, `baton` and `tempo` and focus `ansicyan`, `ok` `ansigreen`, `hold` `ansiyellow`, `error` `ansired`, selection unset (the selected row is reverse), `tool` `ansimagenta`. `NO_COLOR` paints no colour.

## Type

One monospace family for chrome, ids, models, and commands: **JetBrains Mono**, then `ui-monospace`, `SFMono-Regular`, `Menlo`, `Consolas`, `monospace`.

An optional proportional face for prose: **Geist**, then `Noto Sans SC` for CJK glyphs the Latin face does not cover, then the platform sans. When Geist is not available, prose uses the mono family. The TUI does not load a webfont. It uses the terminal's monospace for every role, including prose.

| Role | Face | Size | Line height |
| --- | --- | --- | --- |
| Chrome, ids, models, commands | mono | 12px on the web, one cell in the TUI | 1.45 web, one row TUI |
| Prose | Geist when present, otherwise mono | 15px on the web | 1.65 |

Ids, counts, and durations use tabular figures (`tabular-nums`). Chrome weight is 500 for a label and 400 for a value. The TUI uses bold only for titles, headings, the selected menu row, and strong text.

## Space, radius, border, elevation

The px scale is the web scale. The cell counts are the TUI scale. They are paired tokens, not a conversion, because a terminal cell is not a fixed number of pixels.

| Token | Web px | TUI rows | TUI columns |
| --- | --- | --- | --- |
| space.0 | 0 | 0 | 0 |
| space.1 | 4 | 0 | 1 |
| space.2 | 8 | 1 | 2 |
| space.3 | 12 | 1 | 3 |
| space.4 | 16 | 2 | 4 |
| space.5 | 24 | 3 | 6 |
| space.6 | 32 | 4 | 8 |

List rows separate columns with `space.2`. The web inset is `space.1`. The TUI inset is two columns, the column count of `space.2`, which is the indent already painted before a row glyph.

| Token | Web | TUI |
| --- | --- | --- |
| radius.none | 0 | 0, always |
| radius.control | 2px, controls and rows | 0 |
| radius.card | 3px, cards | 0 |
| radius.overlay | 6px, palette and menus, the maximum | 0 |

| Token | Width | Colour |
| --- | --- | --- |
| border.hairline | 1px | ink.rule |
| border.focus | 2px | ink.focus |

| Token | Web | TUI |
| --- | --- | --- |
| elevation.0 | flat | flat |
| elevation.1 | palette, menus, dialogs: `0 8px 24px rgba(15, 17, 21, 0.45)` in dark, `0 8px 24px rgba(27, 30, 38, 0.12)` in light | a hairline rule border, no shadow |

Operational surfaces stay at elevation 0. Elevation 1 is only the floating palette and menus.

## Motion

Motion marks a state change. Durations match `web/src/lib/motion.ts`. The spinner matches the TUI clock (`int(monotonic_seconds * 10)`, so a frame is 100ms).

| Token | Value |
| --- | --- |
| ease.standard | `cubic-bezier(0.16, 1, 0.3, 1)` |
| duration.quick | 160ms, menu and small appearance |
| duration.fold | 240ms, a section opening |
| duration.enter | 280ms, a dialog |
| duration.exit | 140ms |
| duration.reveal | 360ms, streamed prose, curve `cubic-bezier(0.33, 1, 0.68, 1)` |
| duration.spinner | 100ms |
| spinner.frames | ♪ ♫ ♬ ♫ |
| spinner.ascii | `*` |

Queued and running replace their glyph with the spinner while motion is allowed. Completed, blocked, needs-you, failed, and archived do not animate.

Reduced motion is on when any of these hold:

- the web `prefers-reduced-motion: reduce`
- `PTC_REDUCED_MOTION` is `1`, `true`, `yes`, or `on`
- `TERM=dumb`

The same predicate is `reduced_motion_enabled` in `libs/ptc-cli/ptc_cli/superagent/tui.py`. While it holds, every duration is 0, fades do not run, and queued and running keep ○ and ●. The spinner does not advance.

## Status

Each state has a colour, a glyph, and a label. The ASCII glyph is the `PTC_ASCII=1` fallback from `tui_theme._ASCII`, plus `#` for blocked and `-` for archived.

| State | Ink | Glyph | ASCII | Label | Label zh-CN | Meaning |
| --- | --- | --- | --- | --- | --- | --- |
| queued | muted | ○ | o | Queued | 排队中 | Waiting for a Worker to claim the Ticket |
| running | tempo | ● | * | Running | 运行中 | Worker is executing the Ticket |
| blocked | hold | ▣ | # | Blocked | 已阻塞 | Ticket blocked |
| needs-you | hold | ◆ | ? | Needs you | 需要你 | Waiting for your decision |
| failed | error | ✖ | x | Failed | 失败 | Ticket failed |
| completed | ok | ✔ | + | Completed | 已完成 | Ticket completed |
| archived | muted | ⊘ | - | Archived | 已归档 | Ticket archived |

Blocked and needs-you share `hold`. The glyph and the label distinguish them. Archived uses `muted` and ⊘, the mark the TUI already draws for a cancelled phase. The progress rank in `tui.py` still folds blocked into waiting and archived into cancelled. A surface that implements this contract draws the seven rows above anyway.

On a light selected row, running, completed, and failed switch their ink to `fg`.

### Bilingual strings

Every label exists in `en` and `zh-CN`. In `tokens.json` the English status label is `status[].label` and the zh-CN label is `status[].labelZh`; `strings` holds the rule below.

- Labels are never truncated or ellipsised: status labels, command names, key hints, and error hints. Free text (titles, prose, replies) may wrap or truncate; a label may not.
- Width is measured in terminal cells. A CJK character (East Asian Wide or Fullwidth) is 2 cells; everything else is 1. `需要你` is 6 cells, `Needs you` is 9.
- A column sized for a label fits the widest label of both locales (`strings.widestStatusLabelCells`: en 9, zh-CN 6). The glyph and ink never depend on the locale.

## Keyboard

One model for both surfaces.

| Key | Action |
| --- | --- |
| Ctrl+K | Open the palette from anywhere that is not already inside the palette. |
| `/` | Open the palette, with the query prefilled with `/`, when the composer caret is at column 0. |
| Escape | Close the palette, clear its query, and return focus to the opener. |
| Down | Next row in the focused list. Also moves the palette highlight while the query has the caret. |
| Up | Previous row in the focused list. Also moves the palette highlight while the query has the caret. |
| j | Next row only when the result list, another list, or the transcript is focused, or when navigation mode is on. In the query, the character j. |
| k | Previous row only when the result list, another list, or the transcript is focused, or when navigation mode is on. In the query, the character k. |
| Alt+N | Toggle palette navigation mode. The mode is off when the palette opens. |
| Ctrl+Down | Move focus from the palette query to the result list. |
| Ctrl+Up | Move focus from the result list back to the query. |
| Enter | Activate the focused row, or send the composer when the composer is focused. Inside the palette, run a complete form; on an incomplete form insert the highlighted argument instead. |
| Enter during input-method composition | Ignored on every surface: while an IME is composing (web `KeyboardEvent.isComposing` or keyCode 229; TUI: the terminal delivers the committed text, so only the commit is seen), Enter confirms the candidate and never runs a palette row, sends the composer or inserts an argument. zh-CN users confirm candidates with Enter. |
| Shift+Enter | Newline in the composer. Always on the web; in the TUI when the terminal reports it (kitty CSI `13;2u`, xterm CSI `27;2;13~`). |
| Ctrl+J | Newline in the composer, on both surfaces. |
| Escape, then Enter | Newline in the TUI composer when Enter follows Escape within 0.1 s (Escape flushes after 0.1 s). |
| Tab | Move to the next stop in the focus order. Inside the open palette, Tab accepts the highlighted completion and does not leave the palette. |
| Shift+Tab | Move to the previous stop in the focus order, on both surfaces. Inside the open palette it does nothing and focus stays in the trap. |
| Alt+M | Cycle the mode: auto, dispatch, read-only, then auto again. The status-bar hint is `alt+m mode`. |
| a / s / d | While a permission is pending: allow once, allow for the session, deny. |

j and k insert text while the palette query or any other text field has the caret, including when the query is empty. Navigation mode is the only time they do not. The first character of `jobs` or `reject` is therefore typed. Up and Down move the highlight while the query has the caret. They do not insert a character.

j and k move the highlight only when the result list, another list, or the transcript is focused, or when palette navigation mode is on. Navigation mode is off when the palette opens. Alt+N toggles it, and the chrome shows `nav` while it is on. In that mode, j and k move the highlight and are not inserted, even in an empty query. Any other printable character turns the mode off and is inserted into the query. Escape closes the palette and clears the mode.

Alt+N is the toggle on the web and in the TUI. The web page handles it on `keydown` when Alt is the only modifier and `code` is `KeyN`, then calls `preventDefault`, so the chord is taken and a macOS Option character is not inserted. The page does not bind Ctrl+N. Browsers keep Ctrl+N for a new window, and an ordinary page cannot take that chord. This model does not ask for fullscreen or the Keyboard Lock API.

Ctrl+Down moves focus from the query to the result list. Ctrl+Up moves it back. On the result list, j and k move the highlight. Any other printable character returns focus to the query and inserts that character. The open palette calls `preventDefault` for Alt+N, Ctrl+Down, and Ctrl+Up. Up and Down still move the highlight while the query has the caret. When a host does not deliver Alt+N, navigation mode stays off, and Up, Down, and Ctrl+Down still move the highlight. On the result list, j and k move without navigation mode.

Focus order when the palette is closed:

1. The main list, or the transcript when no list is on screen.
2. The composer.

Shift+Tab walks this order backwards on the web and in the TUI. No surface rebinds Shift+Tab: browsers need it for reverse tab order, and assistive technology relies on it.

The status line is not in the cycle. j and k on the focused transcript scroll one row. PageUp and PageDown scroll ten rows, which is the TUI binding, and they are optional on the web.

The open palette is a focus trap:

1. The query.
2. The result list.

Inside the trap, Tab accepts the highlighted completion and Shift+Tab does nothing. Ctrl+Down and Ctrl+Up move focus between the query and the result list. Escape closes the trap, clears the query, and returns focus to the opener. A needs-you prompt is a trap in front of the composer until Escape dismisses it or Enter sends the reply.

### Mode cycle

Alt+M cycles the mode through `auto`, `dispatch`, and `read-only`, then wraps to `auto`. The web page handles it like Alt+N: on `keydown` when Alt is the only modifier and `code` is `KeyM`, then `preventDefault`, so a macOS Option character is not inserted. The status-bar hint reads `alt+m mode`. In the TUI, Alt+M is suppressed while a permission is pending, as the old binding was.

**Change from the shipped TUI.** The shipped TUI binds Shift+Tab (`s-tab` in `libs/ptc-cli/ptc_cli/superagent/tui.py`) to cycle the mode, and its hint reads `shift+tab mode`. That binding moves to Alt+M on both surfaces, and Shift+Tab becomes the previous focus stop. The TUI keymap slice changes the `s-tab` binding, the help text, and the tests together.

### Composer newline

Enter sends. A newline is Shift+Enter when the terminal reports it (kitty CSI `13;2u`, xterm CSI `27;2;13~`), Ctrl+J everywhere, or Escape then Enter pressed within 0.1 s (Escape flushes after 0.1 s). On the web, Shift+Enter is the newline and Ctrl+J is also accepted. The TUI forms are the ones in `docs/ports/cli-tui.md`.

### Permission prompt

While a permission is pending, Enter, Up, Down, Alt+M, and every newline binding are suppressed. `a` allows once, `s` allows for the session, `d` denies, and Escape denies. `a`, `s`, and `d` answer the prompt and are not typed into the composer. The hint row reads `[a]llow once  [s]ession allow  [d]eny`.

### Escape while watching

The TUI still treats a second Escape, while a watch is running and the palette is closed, as the interrupt confirmation. That confirmation is a TUI binding on top of this model. The shared back key remains Escape.

## Palette grammar

The palette and the slash menu are one grammar. The idle list follows `SLASH_COMMANDS` insertion order. `approve` and `reject` have no slash name; they sit immediately after `dispatch`, because they are the `conductor dispatch` verbs `approve <id>` and `reject <id>` (`VERBS` in `src/conductor_cli/dispatch.py`). `/exit` and `/q` are aliases of `quit`. `/j` and `jobs` are aliases of `tickets`. `/attach <target>` is `tickets watch <target>`.

### One matcher

One command matcher drives the web Ctrl+K palette, the TUI Ctrl+K palette, and the TUI `/` at composer column 0, which opens the same palette with the query prefilled with `/`. The rule is case-insensitive fuzzy subsequence matching.

1. **Needle.** The query with one leading `/` removed, then lower-cased (Unicode casefold).
2. **Haystacks.** For each command: the canonical name, the slash name without `/`, and each alias with one leading `/` removed, all lower-cased the same way.
3. **Match.** The needle's characters appear in the haystack in order, not necessarily next to each other. The alignment is greedy left to right and always takes the leftmost later character. Each command is ranked by its best haystack.
4. **Rank** by the first difference: a haystack equal to the needle, then a haystack that starts with the needle, then the lower gap cost, then the earlier start index, then the canonical name ascending. Gap cost is `endIndex - startIndex - (needleLength - 1)`; a contiguous run scores 0.
5. **Empty needle** shows the idle list, unchanged. At most 60 rows are shown (`completionLimit`). The first ranked row is highlighted.

Every surface and every check satisfies these examples:

| Query | First row | Haystack | Indexes | Gap cost |
| --- | --- | --- | --- | --- |
| `dsp` | dispatch | dispatch | 0, 2, 3 | 1 |
| `DSP` | dispatch | dispatch | 0, 2, 3 | 1 |
| `jobs` | tickets | jobs (alias) | 0, 1, 2, 3 | 0 |
| `Disp` | dispatch | dispatch | 0, 1, 2, 3 | 0 |
| `tkt` | tickets | tickets | 0, 3, 5 | 3 |
| `apv` | approve | approve | 0, 1, 5 | 3 |
| `zzz` | no rows | none | none | none |

With no rows the list shows the empty state `no command matches` (zh-CN `没有匹配的命令`). The same examples are `palette.fuzzy.examples` in `tokens.json`, and `palette.fuzzy.caseSensitive` is `false`.

**Current shipped behaviour, replaced.** Today the TUI composer's `SlashCompleter` (`libs/ptc-cli/ptc_cli/superagent/tui_completion.py`) completes the command token only while the line has no space, as a case-sensitive prefix of the name with the slash removed, sorted by command name, at most 60 rows (`COMPLETION_LIMIT`), with the `display_meta` strings as descriptions. That is the current shipped behaviour that the TUI palette slice replaces. It is not the target design.

### Argument completion

After the command token and a space, completion switches to arguments. An argument matches as a unique **prefix** of an id or a name, never fuzzy. Hex ids compare case-sensitively, as `_matched_target` does. The steps below are the shapes `handle_command` in `libs/ptc-cli/ptc_cli/superagent/tui.py` already parses, plus the dispatch and memory forms.

- `dispatch ` then Tab lists the active dispatch ids in every phase. Tab inserts the highlighted id.
- `approve ` and `reject ` list only ids whose phase is `awaiting_human`.
- `approve` and `reject` without an id are incomplete. Enter does not run them. Enter inserts the highlighted id when one is offered; otherwise the row shows the error hint `approve needs a dispatch id` (zh-CN `approve 需要一个 dispatch id`), and likewise `reject needs a dispatch id` (zh-CN `reject 需要一个 dispatch id`).
- `reply ` offers nothing; the body is free text.
- An ambiguous prefix lists every match and inserts nothing.

Tab accepts the highlighted token and adds a trailing space when the form still needs an argument. Tab never leaves the palette. Enter runs a complete form; on an incomplete form it inserts the highlighted argument instead. Shift+Tab does nothing inside the palette.

A **target** is what `_matched_target` in `libs/ptc-cli/ptc_cli/superagent/tui.py` resolves. The token is stripped. Empty text, or text that still contains whitespace, is not a target. Web and TUI share this order and stop at the first hit:

1. A full Ticket UUID (`8-4-4-4-12` hex). The target is the token as written.
2. An in-range index. `#N` or a bare integer `N`, 1-based, into the last painted list (`_snapshot_ids`: the numbered snapshot, or the first three recent Tickets when that snapshot is empty). The target is that row's Ticket id. An in-range index wins over a prefix. `int` parses the digits, so `00000001` is index 1 when the list has a first row.
3. A unique id prefix. The token matches `[0-9a-fA-F-]{8,}` and contains at least eight hex digits. Known ids are the snapshot ids, then recent Tickets and jobs, then the current Ticket, keeping the first copy of each id. When exactly one of those ids starts with the token, case-sensitive, the target is that full id.

A bare integer past the end of the painted list continues to step 3. `12345678` is not a painted row when the list is shorter than that index, and it still resolves when exactly one known Ticket id starts with `12345678`, including an id that is only on the jobs list. A leading `#` stays on the index step: `#` is outside `[0-9a-fA-F-]`, so an out-of-range `#N` does not prefix-match.

If the prefix step finds no id, or the token is not a prefix, `_matched_target` returns nothing and the token is not a target. `_resolve_target` then raises `no job at index` when the token is `#N` or all digits, and returns any other unresolved token unchanged. Two or more prefix matches raise `{token} matches more than one job: {ids}` with the ids joined by commas. Completion lists those matches and inserts nothing. Completion order is list index, then known ids in that same order.

A **dispatch-id** completes from the active dispatch roster by unique prefix, in every phase for `dispatch` and only `awaiting_human` for `approve` and `reject`. A **line** completes from worktree line names. After `--by`, the line under review is left out. A **proposal-id** completes from pending memory proposals. A **session-id** completes from saved sessions by unique prefix. A **worker-id** completes from the last fleet list. Free text (`recall` query, `propose` entry, `reply` body) does not complete.

`memory approve` and `memory reject` stay under `memory`. The top-level verbs `approve` and `reject` are dispatch decisions.

`@path` is not a palette verb. `AtFileCompleter` still completes it from the local filesystem.

| Command | Slash | Forms | Hint |
| --- | --- | --- | --- |
| workers | /worker | workers<br>workers <worker-id> | show which worker has the current Ticket |
| dispatch | /dispatch | dispatch<br>dispatch <dispatch-id> | dispatch roster; /dispatch <id> reads one |
| approve | none | approve <dispatch-id> | approve a dispatch that is waiting for a person |
| reject | none | reject <dispatch-id> | reject a dispatch that is waiting for a person |
| tickets | /jobs | tickets<br>tickets --attention<br>tickets <target><br>tickets watch <target> | list Tickets; /jobs <id> shows one; /jobs watch <id> follows it |
| workspace | /workspace | workspace | switch the Home workspace |
| reply | /reply | reply <text><br>reply <target> <text> | answer the Ticket that needs you |
| retry | /retry | retry<br>retry <target> | retry a failed or waiting Ticket |
| cancel | /cancel | cancel<br>cancel <target> | stop a running Ticket (/cancel <id|index>) |
| resume | /resume | resume<br>resume <session-id> | list saved sessions; /resume <id> reopens one |
| status | /status | status | reread Supervisor progress for the current Ticket |
| help | /help | help | show the TUI key map and commands |
| quit | /quit | quit | quit the TUI |
| skills | /skills | skills | local skills status |
| mcp | /mcp | mcp | local MCP server status |
| memory | /memory | memory<br>memory recall <query><br>memory propose <entry><br>memory approve <proposal-id><br>memory reject <proposal-id><br>memory export [--limit <n>] [--include-secret] | browse remembered memory |
| agents | /agents | agents | local fleet, native runtimes, daemons |
| review | /review | review run <line> --by <line><br>review status [line] | queue a local cross-review (/review run <line> --by <line>) |
| merge | /merge | merge <line> --into <branch> | merge a line locally after its tests (/merge <line> --into <branch>) |
| stop | /stop | stop <target> | stop a running Ticket (/stop <id|index>) |
| signals | /signals | signals | Sources and job counts |
| clear | /clear | clear | clear the conversation |
| details | /details | details | toggle Worker details (default: Supervisor view) |
| model | /model | model | show the Router model and configured providers |
| compact | /compact | compact | drop activity noise and duplicate rows from the transcript |
| expand | /expand | expand | expand every tool block |
| collapse | /collapse | collapse | collapse every tool block |
| new | /new | new | start a fresh session (persisted) |

### Primary forms

**workers.** `workers` shows the worker on the current Ticket (`/worker`). `workers <worker-id>` reads one id from the fleet list.

**dispatch.** `dispatch` lists the active roster. `dispatch <dispatch-id>` reads one. Repeating the command refreshes; it does not follow a heartbeat.

**approve.** `approve <dispatch-id>` posts the dispatch approve call. The slash menu has no name for it. Bare `approve` is incomplete and does not run.

**reject.** `reject <dispatch-id>` posts the dispatch reject call. The slash menu has no name for it. Bare `reject` is incomplete and does not run.

**tickets.** `tickets` lists. `tickets --attention` lists the ones that need you. `tickets <target>` shows one. `tickets watch <target>` follows one.

**retry.** `retry` retries the current Ticket. `retry <target>` retries a failed or waiting Ticket. Completion prefers those two statuses.

**review.** `review run <line> --by <line>` queues a cross-review. `review status [line]` lists the queue. The bare word `review` is incomplete and does not run.

**memory.** `memory` browses. `memory recall <query>` searches. `memory propose <entry>` stages an entry. `memory approve <proposal-id>` and `memory reject <proposal-id>` decide a proposal. `memory export` takes optional `--limit <n>` and `--include-secret`. The slash command `/memory` is the browse form; the other forms are the `conductor memory` subcommands.

The remaining commands run as their slash handlers: `reply [<target>] <text>`, `cancel <target>`, `stop <target>`, `resume` with an optional session id, `merge <line> --into <branch>`, and the no-argument commands in the table (`workspace`, `status`, `help`, `quit`, `skills`, `mcp`, `agents`, `signals`, `clear`, `details`, `model`, `compact`, `expand`, `collapse`, `new`).
