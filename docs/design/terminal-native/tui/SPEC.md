# Terminal-native TUI — design specification

**Ticket** `3419e39e-ca69-4838-844d-547da6333dfc:line-2-r1-1`
**Line** TUI — owns `docs/design/terminal-native/tui/` and nothing else.

This document specifies the screens of the native terminal TUI, the layout
at four terminal widths and in two languages, the state vocabulary, the
keybindings, the shared Ctrl+K palette as the TUI draws it, and the
artefacts that prove the layout.

---

## 1. What this document is

The native TUI is the terminal front end of the platform. It is one
full-screen surface with these screens in it:

| Screen | Name | What it is for |
|---|---|---|
| home | `home` | What is running and what needs the operator |
| the Ticket list | `jobs` | Every Ticket the session knows, newest first |
| the dispatch roster | `dispatch` | One dispatch: its lines, its roster, its DAG |
| the review queue | `review` | The cross-reviews waiting for a decision |
| the running Ticket | `running` | The stream of a Ticket in flight |
| palette completion | `completion` | The open palette answering a command query (`/dsp`) |
| argument completion | `completion-args` | The open palette completing an argument (`approve `) |
| the permission gate | `permission` | The gate in front of a local action |

They are **screens of one surface**, not separate documents: they share
one frame, one chrome, one command region, one composer and one status bar.
The palette is not a screen of its own. It is the **command region** docked
above the composer (section 6): closed, it lists the commands that act on
the screen; opened with `ctrl+k` or with `/` at column 0, it takes the
focus and grows a query line. `completion` and `completion-args` are the
open states of that region.

Every screen is drawn in **English** and in **zh-CN** (section 11).

### 1.1 The layout contract

The platform's layout contract is a gate at three widths: **40, 80 and 120
columns**. The shipped TUI refuses to run below 80 columns
(`MIN_TUI_COLUMNS = 80` in `libs/ptc-cli/ptc_cli/superagent/tui.py`); the
native surface has to hold at 40. The mock checks every screen, in both
languages, at **120, 100, 80 and 40** columns and fails the build if any of
the following is true:

* a row is wider than the terminal — measured by the layout and again by
  rich's own export, indent and rail included;
* a screen loses a row it must keep (the composer prompt, the status bar);
* a state loses its glyph or its word;
* a prose row loses a character;
* a narrow row drops a column instead of stacking it;
* a status hint still says `shift+tab`.

The mock is the executable form of this document:

```
python3 docs/design/terminal-native/tui/tui_mock.py
```

The script finds the token set on its own: the default is the sibling file
next to it, `docs/design/terminal-native/system/tokens.json`, resolved from
the script's own directory, so the command works from any working
directory. Pass `--tokens <path>` to point it somewhere else.

It runs every check first. When one fails it prints the problems, writes
nothing and exits 1. When all hold it writes the artefacts of section 9,
deletes any `tui-*.svg` / `tui-*.txt` it did not write, and exits 0. The
output is deterministic: a second run changes no byte.

### 1.2 What is not in this document

The token set, the colour contract and the keyboard contract are **not**
defined here. They are owned by the sibling line and referenced, never
copied. The paths below are relative to this directory:

* `../system/SPEC.md` — the system contract (surfaces, ink, motion, keyboard,
  palette grammar).
* `../system/tokens.json` — the token set: `themes.dark.surfaces`,
  `themes.dark.ink`, `status[]`, `keyboard`, `palette`.

No hex value appears in this document and in the mock. A colour is always
named by its token, as `tokens.json` spells it: `ink.fg`, `ink.muted`,
`ink.baton`, `ink.tempo`, `ink.ok`, `ink.hold`, `ink.error`, `ink.rule`,
`ink.onAccent`, `surface.page`. The tables below write the ink part only
(`baton`, `muted`).

---

## 2. The frame

Every screen is the same five regions, top to bottom.

| # | Region | Rows | Anchor | Ink |
|---|---|---|---|---|
| 1 | The identity bar | 1 | pinned | `baton` / `fg` / `muted` |
| 2 | The transcript | the rest | top (`home`), bottom (every other screen) | per row |
| 3 | The command region | 1 header + N rows (+ 1 footer when open) | docked above the composer | `baton` + `muted` |
| 4 | The composer | 3 with a wall, 1 without | pinned to the bottom | `rule` + `fg` |
| 5 | The status bar | 1 | pinned to the bottom | `muted` |

### 2.1 The identity bar

The bar is chrome, not scrollback: it stays on screen while the transcript
moves. It is built from whole segments, and a segment that does not fit
drops by rank — a segment is never sliced.

| Segment | Ink | Drop rank |
|---|---|---|
| ` Conductor ` | `baton bold` | 100 |
| `Super-agent` | `fg bold` | 80 |
| `  ○ idle` — the stream state | `ok` when live, `muted` otherwise | 60 |
| `  deepseek-flash · session bad2c655` — the model and the session | `muted` | 40 |

The lowest rank drops first.

### 2.2 The composer

The composer is the only region that has a wall: a rounded box drawn in
`rule` around the prompt. The wall appears when the terminal is **wider
than 60 columns**; below that the composer is a bare line. The wall takes
one row above and one below the prompt, so the composer is three rows tall
with a wall and one row without.

The prompt is `❯ ` followed by the draft, or by the placeholder in `muted`
when the draft is empty. The placeholder has a long and a short form; the
short one is used when the long one does not fit the row, and it is cut
with `…` only below that:

| Language | Long | Short |
|---|---|---|
| en | `Describe a task, / for commands, @ to attach a file` | `Describe a task, / for commands` |
| zh-CN | `描述一个任务，/ 打开命令，@ 附加文件` | `描述一个任务，/ 打开命令` |

The caret is the `❯`: it is the one mark that is on every screen at every
width.

### 2.3 The status bar

Counts on the left, hints on the right, both in `muted`. The counts are the
mode and the fleet (`auto · ● 3 online`). The hints are the keys that are
live at that moment (`◆ 2 · shift+enter newline · alt+m mode`).

The hints are dropped from the **end** of the list as the terminal
narrows: the bar shows the longest leading run of hints that still fits.
The counts are never dropped for a hint.

| State of the screen | Hints, in order |
|---|---|
| idle (home, jobs, dispatch, review) | `◆ 2` · `shift+enter newline` · `alt+m mode` (· `? help` on home) |
| running | `esc interrupt` · `shift+enter newline` · `alt+m mode` |
| palette open | `tab accept` · `ctrl+down list` · `esc close` · `alt+n nav` |
| permission pending | the bar is taken over (section 7.8) |

`alt+m mode` replaces the shipped `shift+tab mode` (section 5.6).

The permission gate takes the bar over: the label gives way, the three keys
never do (section 7.8).

### 2.4 The chrome at the four widths

| Region | 120 | 100 | 80 | 40 |
|---|---|---|---|---|
| Identity bar | 4 segments | 4 segments | 4 segments | 3 segments — the model and session drop |
| Composer | wall, 3 rows, long placeholder | the same | the same | no wall, 1 row, short placeholder |
| Status bar | counts left, hints right | same | same | hints clipped to the ones that fit |

---

## 3. The layout rules

### 3.1 The margin and the gap

Every region starts two columns from the left edge (`INSET = 2`). The
columns of a table are separated by two columns (`GAP = 2`). The running
card is the exception: its parts are separated by the literal ` · ` so the
card reads as one line of a sentence.

### 3.2 The fit rule — which column drops

A table row has one flexible column (the title or the subject) and the rest
are fixed. When the row does not fit in the room it has, the **lowest
priority column is sacrificed**:

1. a fixed column is **dropped**;
2. the flexible column **shrinks** to the room that is left.

The priority ladder is the same on every screen. A higher number is kept
longer; the **lowest** priority goes first.

| Priority | Columns that carry it |
|---|---|
| 100 | the selection marker, the state glyph, the line of a dispatch or a review |
| 95 | the state word, the canonical name of a command, the id of an argument row |
| 90 | the Ticket id, the review decision, the slash name of a command |
| 85 | the review verdict |
| 80 | the title or the subject — the flexible column |
| 75 | the next command |
| 70 | the model |
| 60 | the gate, the fix count |
| 50 | the hint of a command row, the subject of an argument row (truncatable) |
| 45–35 | the worker, the age, the reviewer, the branch |
| 30 | the row index, the clock time |

Two consequences are worth stating because they are the rules the whole
layout rests on:

* **A convenience column goes before the identity.** The row index, the
  clock, the worker, the age and the reviewer are the first things to go;
  the title, the state glyph and the state word are the last.
* **A truncatable column gives up its room before anything is dropped.**
  The hint of a command row is prose: it is cut with an ellipsis
  (`list Tickets; /jobs <id> shows one; /jobs watch…`) while the row stays
  whole. Only when the hint cannot reach its floor (16 columns) does the
  row drop a column — the slash name first, because the canonical name is
  what the command is and the hint is only what it does.

The title of a row is **not** truncatable: it is cut only when there is
nothing left to drop.

Three columns are **pinned**, on every screen: the marker of the focused
row (`▸`), the glyph of its state (`✔`) and the word that goes with it
(`Completed`, `已完成`). A pinned column is never dropped, whatever the
title does. A title that does not fit wraps in the narrow form and is cut
in the wide one, but it never pushes the mark of the row out of it.

### 3.3 The stack rule — what stacks at 40

At 40 columns and below a row **stacks** instead of shedding columns. The
row becomes two lines:

* the **identity line** — the columns that identify the row: the selection
  marker, the state glyph, the title;
* the **meta line** — every other column of the row, in priority order,
  joined by ` · ` and indented two columns under the identity line.

The meta line wraps over as many lines as it needs, and a part that is
wider than a whole line wraps inside itself instead of being cut.
**Nothing is dropped**: a narrow terminal shows less, not less of what it
shows. The check `check_stack_coverage` fails the build if a column of a
table is on neither line, or if the meta line lost any part of a cell.

The identity line keeps its markers: the selection marker and the state
glyph stay on the first line, and a title that does not fit **wraps under
them** instead of taking them with it. A Chinese title is twice as wide
as an English one of the same length, and it must not take the marker of
the row with it. The check `check_long_titles` fails the build when it
does.

The header row of a table keeps only the identity label in the narrow form.
The labels of the meta columns would only repeat the values under them.

### 3.4 Wrapping

A prose row that does not fit wraps; it is never cut. The room a row wraps
into is the terminal width **minus the indent and minus the rail or label
in front of the text** (`│ ` on the running stream, `palette › ` on the
query line): the text never competes with its prefix for columns, and no
line — indent, prefix and text together — is wider than the terminal.

The example the contract is built on: 19 Chinese characters are 38 cells.
Behind an indent of 2 and the rail `│ ` in a 40-column terminal they would
need 42 cells, so the row wraps after 18 characters (36 cells of text, 40
in all) and carries the 19th to the next line. `check_prefix_wrap` runs
exactly that case, measures every line with the layout and with rich, and
compares the text that comes out with the text that went in.

The rules of the break:

* English breaks on a word boundary.
* A word longer than the line, and a run of CJK text (which has no spaces
  to break on), fill the line they start on and are cut across as many
  lines as they need; every piece is kept. A short head such as `╰` or
  `共 2` never sits alone above them.
* A line never opens with a closing mark: `，。、；：？！` move down with
  the character above them.
* A wide character in a room of one column is still drawn, never dropped.

### 3.5 Borders

The composer wall is drawn when the terminal is wider than 60 columns. The
same threshold is the shipped rule (`_border_enabled` in `tui.py`).

---

## 4. States: glyph, label and token

Colour is never the only signal. Every state is written as a **glyph and a
word**, in one ink. `PTC_ASCII=1` swaps the glyphs for their ASCII twin and
keeps the word.

### 4.1 The Ticket states

These are the states of the shared token set (`status[]` of
`../system/tokens.json`). The same glyph and the same word are used on
every screen that shows a state. `check_tokens` fails the build when the
mock and the token set disagree on an id, a glyph, an ASCII twin, a word
or an ink.

| State id | Glyph | ASCII | Word (en) | Word (zh-CN) | Ink token | Meaning |
|---|---|---|---|---|---|---|
| `queued` | `○` | `o` | Queued | 排队中 | `muted` | Waiting for a Worker to claim the Ticket |
| `running` | `●` | `*` | Running | 运行中 | `tempo` | Worker is executing the Ticket |
| `blocked` | `▣` | `#` | Blocked | 已阻塞 | `hold` | Ticket blocked |
| `needs-you` | `◆` | `?` | Needs you | 需要你 | `hold` | Waiting for your decision |
| `failed` | `✖` | `x` | Failed | 失败 | `error` | Ticket failed |
| `completed` | `✔` | `+` | Completed | 已完成 | `ok` | Ticket completed |
| `archived` | `⊘` | `-` | Archived | 已归档 | `muted` | Ticket archived |

The word is never cut to fit: `Needs you` is `Needs you`, never
`Needs yo…`, and `需要你` is never `需要…`. A row that cannot hold the word
gives up a column instead. The state column is 9 cells: the longest
English word is 9, the longest zh-CN word is 6.

### 4.2 The review states

A cross-review verdict and a cross-review decision are states too, and they
carry a glyph next to their word.

| Verdict | Glyph | Word (en) | Word (zh-CN) | Ink token |
|---|---|---|---|---|
| `MERGE` | `✔` | MERGE | 合并 | `ok` |
| `BLOCK` | `✖` | BLOCK | 阻断 | `error` |
| none yet | — | `—` | `—` | `muted` |

| Decision | Glyph | Word (en) | Word (zh-CN) | Ink token |
|---|---|---|---|---|
| `approve` | `✔` | approve | 批准 | `ok` |
| `reject` | `✖` | reject | 驳回 | `error` |
| `pending` | `◆` | pending | 待定 | `hold` |

The gate of a line is a word as well: `reviewed` / 已评审, `build` /
构建中, `—`.

### 4.3 The states of the other screens

| Screen | State | How it is written |
|---|---|---|
| home | the Ticket state | glyph + word, in the roster row |
| `/jobs` | the Ticket state | glyph + word, in the Ticket row |
| `/dispatch` | the state of every line | glyph + word, in the roster row |
| `/review` | the verdict and the decision | glyph + word, in the queue row |
| running | the Ticket is running | the identity bar reads `● live` (`● 实时`) in `tempo`; the card's mark is the **spinner frame**, not a state glyph |
| completion | none — the list is a list of commands | the marker `▸` is the selection, not a state |
| completion-args | the dispatch an id belongs to | glyph + word: `◆ Needs you` for phase `awaiting_human` |
| permission | the gate is waiting for a person | `◆` in `hold`, the same mark as `needs-you` |

The spinner frames are `♪ ♫ ♬ ♫` — the frames of the shipped TUI
(`_SPINNER_FRAMES`). With reduced motion on, the spinner is a static frame
(`reduced_motion_enabled` in `tui.py`); the ASCII fallback is `*`.

---

## 5. Keybindings

The bindings are the ones of the shared token set (`keyboard` of
`../system/tokens.json`), of the decisions D1–D3 of this round, and of the
shipped TUI (`docs/ports/cli-tui.md`). Key names are written the way the
token set writes them (`ctrl+k`, `alt+n`, `ctrl+down`) and are never
localized; the word after the key is (`shift+enter 换行`).

### 5.1 Opening and closing the palette

| Key | Where | Action |
|---|---|---|
| `ctrl+k` | anywhere outside the palette | open the palette: the command region takes the focus, the query line is empty, the list is the idle order |
| `/` | the composer, caret at column 0 | open the palette with the query prefilled `/` — the same palette and the same matcher as `ctrl+k` |
| `escape` | inside the palette | close it, clear the query, return the focus to the opener (the composer, or the list or transcript that had it) |

### 5.2 Inside the open palette

The open palette is a focus trap between the **query** and the **result
list** (`keyboard.paletteTrap`).

| Key | Action |
|---|---|
| printable characters | typed into the query; the list re-filters on every key |
| `up` / `down` | move the highlight while the query has the caret (they insert nothing) |
| `ctrl+down` | move the focus from the query to the result list |
| `ctrl+up` | move the focus from the result list back to the query |
| `j` / `k` | on the result list: next / previous row. In the query: the characters `j` and `k` |
| any other printable character on the result list | return the focus to the query and insert the character |
| `alt+n` | toggle navigation mode (indicator `nav`, off when the palette opens): `j` / `k` move the highlight even in the query; any other printable character turns it off and is inserted |
| `tab` | accept the highlighted token (section 6.4). Tab **never leaves the palette** |
| `shift+tab` | nothing — it stays inside the trap |
| `enter` | run a complete form; on an incomplete form insert the highlighted argument instead (section 6.4) |
| `escape` | close, clear, restore the opener |

`ctrl+n` is not a binding: browsers keep it for a new window, and the web
and the TUI share one key model.

### 5.3 Lists, the transcript and `j` / `k`

| Key | Action |
|---|---|
| `j` | the next row of the focused list |
| `k` | the previous row of the focused list |
| `enter` | activate the highlighted row |
| `escape` | back: leave the list, close the palette, deny a permission |
| `pgup` / `pgdn` | scroll the conversation by ten rows |

`j` and `k` move the highlight **only** when the focused surface is the
result list, another list, or the transcript, or when navigation mode is
on. While the palette query or any other text field has the caret — even
an empty one — `j` and `k` are typed into it.

### 5.4 The focus order

The focus order is **`main-list-or-transcript` → `composer`**
(`keyboard.focusOrder`). `tab` moves to the next stop and `shift+tab` to
the previous one when the palette is closed. The palette traps the focus
between `query` and `result-list`. A pending decision traps it between
`reply-or-decision` and `composer` (`keyboard.needsYouTrap`).

### 5.5 The composer and the other keys

| Key | Action |
|---|---|
| `enter` | send the composer line |
| `shift+enter` | a newline in the composer, when the terminal reports it (kitty CSI `13;2u`, xterm `27;2;13~`) |
| `ctrl+j` | a newline in the composer, on every terminal that does not report `shift+enter` |
| `escape` then `enter`, pressed quickly | a newline in the composer, when the terminal reports neither; the escape flushes after 0.1 s |
| `alt+m` | cycle the mode: `auto` → `dispatch` → `read-only` (suppressed while a permission is pending) |
| `shift+tab` | the previous focus stop (section 5.4); never a mode key |
| `@path` + `tab` | attach a local file |
| `a` / `s` / `d` | answer a permission: allow once / session allow / deny |
| `escape` | deny a pending permission; otherwise stop watching the Ticket |
| `ctrl+c` | clear the composer; deny a pending permission; a second press within 1.5 s quits |
| `ctrl+d` | quit when the composer is empty |

A terminal that sends `shift+enter` as an ordinary `enter` has no key of
its own to report a newline. The composer therefore has **three routes to
a newline**, in this order: `shift+enter` when the terminal reports it,
`ctrl+j` everywhere else, and `escape` followed by `enter` pressed
quickly. The escape flushes after 0.1 s, so a fast escape and enter is a
newline and a slow one is not — the same rule the shipped TUI uses
(`docs/ports/cli-tui.md`, Keys).

While a permission is pending, `enter`, `up`/`down`, `alt+m` and **every**
newline binding — `shift+enter`, `ctrl+j` and `escape` then `enter` — are
switched off, and `a`/`s`/`d` answer the permission instead of typing;
`escape` denies. The composer takes the bindings back the moment the
permission is answered.

### 5.6 What changes from the shipped TUI

| Shipped (`libs/ptc-cli/.../tui.py`, `tui_completion.py`) | This design | Why |
|---|---|---|
| `shift+tab` (the `s-tab` binding) cycles the mode `auto` → `dispatch` → `read-only`; the status bar says `shift+tab mode` | `alt+m` cycles the mode; the status bar says `alt+m mode` (zh-CN `alt+m 模式`); `shift+tab` is the previous focus stop | D3: `shift+tab` is reverse tab on both surfaces and is never overridden (browsers and assistive tech need it) |
| `/` + `tab` opens `SlashCompleter`: a **case-sensitive prefix** of the command name without the slash, sorted by name, stopping at the first space | `/` at column 0 opens the shared palette prefilled `/`; the matcher is the case-insensitive fuzzy subsequence of D1, ranked as section 6.3 says | D1: one matcher on every surface |
| no argument completion in the slash menu | argument completion after the command token (section 6.4) | D2 |

The shipped prefix rule is described here only as the current behaviour
that the TUI palette slice replaces; it is not a target. The keymap slice
of the plan changes the `s-tab` binding, its help text and its tests.

---

## 6. The palette in the TUI

There is no palette screen and no floating overlay. The palette is the
**docked command region** between the transcript and the composer. It
takes its rows from the transcript, never from the composer, so the
composer and the status bar stay where they are while it is open.

### 6.1 Closed: the fallback of the screen

Every screen carries the region, closed, as a short list of the commands
that act on it. The header is the word `commands` (`命令`) in
`baton bold`; the first row is the command the screen would act on by
default.

| Screen | The commands it offers |
|---|---|
| home | `tickets`, `reply`, `retry`, `help` |
| `/jobs` | `tickets`, `reply`, `retry`, `cancel` |
| `/dispatch` | `dispatch`, `approve`, `reject`, `review` |
| `/review` | `review`, `approve`, `reject`, `merge` |
| running | `status`, `details`, `stop`, `compact` |
| permission | `approve`, `reject`, `help` |

### 6.2 Open: the query line, the list, the footer

`ctrl+k` (or `/` at column 0) opens the same region **focused**:

```
  palette › /dsp▏
  ▸ dispatch      /dispatch   dispatch roster; /dispatch <id> reads one
  1 match · tab accepts · enter runs
```

| Row | Content | Ink |
|---|---|---|
| query line (replaces the header) | the label `palette › ` (`命令面板 › `), the query, the caret `▏` | label `baton bold`, query `fg` |
| result rows | at most 8 visible (`WINDOW_ROWS`), at most 60 in all (`palette.completionLimit`) | as the fallback rows |
| footer | the count and the keys, the empty state, or the error of an incomplete form | `muted`; the error in `error`; an ambiguous prefix in `hold` |

A command row has the grammar of the shared palette:

| Column | Width | Ink |
|---|---|---|
| the marker `▸` | 2 | `baton` |
| the canonical name | 12 | `fg` (`fg bold` when highlighted) |
| the slash name (`—` when there is none) | 10 | `muted` |
| the hint, localized | flexible, truncatable | `muted` |

The query line is a prose row with a label in front of it: it wraps in the
room the label leaves (section 3.4). The status bar shows the palette keys
(section 2.3).

### 6.3 The matcher (D1)

One matcher drives web `ctrl+k`, TUI `ctrl+k` and the TUI `/` at column 0.

* **Needle** — the query with one leading `/` removed, case-folded
  (Unicode casefold, `palette.fuzzy.fold`).
* **Haystacks** — the canonical name, the slash name without `/`, and every
  alias with one leading `/` stripped, all case-folded.
* **Match** — the needle's characters appear in the haystack in order, not
  necessarily next to each other (subsequence). The best haystack of a
  command counts.
* **Rank**, by the first difference: a haystack equal to the needle; a
  haystack that starts with the needle; the lower gap cost of the greedy
  leftmost alignment, `gapCost = endIndex - startIndex - (needleLength - 1)`;
  the earlier start index; the canonical name ascending.
* An empty needle shows `palette.idleOrder` unchanged. At most 60 rows.

| Query | First row | Why |
|---|---|---|
| `dsp`, `DSP`, `/dsp` | dispatch | indexes 0, 2, 3; gap cost 1; case folded; one `/` stripped |
| `Disp` | dispatch | prefix of `dispatch` |
| `jobs` | tickets | the alias `jobs` |
| `tkt` | tickets | subsequence of `tickets` |
| `apv` | approve | subsequence of `approve` |
| `zzz` | — | no rows; the footer reads `no command matches` (`没有匹配的命令`) |

### 6.4 Argument completion (D2)

After the command token and a space the list switches from commands to
arguments. The command token is resolved by an exact (folded) haystack
match, so `approve `, `/dispatch ` and `jobs ` all resolve.

* The argument match is a **unique prefix** of an id or a name,
  case-sensitive for hex ids as in `_matched_target` — never fuzzy.
* `dispatch ` lists every active dispatch id, in every phase.
* `approve ` and `reject ` list **only** the ids whose phase is
  `awaiting_human`.
* `reply ` offers nothing: its argument is free text.
* An **ambiguous prefix** (`approve 7f` when `7f3a91c2` and
  `7f9e02b1` both wait) lists the matches and inserts nothing; the
  footer, in `hold`, says how many ids match and asks for more of the id.

| Key | On a command row | On an argument row |
|---|---|---|
| `tab` | insert the highlighted token — the slash name when the palette was opened with `/` and the command has one, the canonical name otherwise — plus a trailing space when the command cannot run without an argument (`apv` → `approve `, `dsp` → `dispatch`, `/dsp` → `/dispatch`) | insert the highlighted id (`dispatch ` → `dispatch 7f3a91c2`); nothing on an ambiguous prefix |
| `enter` | run the command when its bare form is complete; otherwise insert it as `tab` would | run a complete form (`approve 7f3a91c2`); on an incomplete form insert the highlighted id; on an ambiguous prefix do nothing |

`approve` and `reject` **without an id are incomplete**: Enter never runs
them. When an id is offered, Enter inserts the highlighted one. When none
is offered the footer shows the error hint in `error`:

```
  palette › approve ▏
  approve needs a dispatch id
```

zh-CN: `approve 需要一个 dispatch id`. The same hint is written for
`reject`. A dispatch that is not `awaiting_human` (`2c81e0d4`, running)
is never offered to `approve` or `reject`, and Enter does not run
`approve 2c81e0d4`.

### 6.5 The palette at the four widths

| Width | A command row | An argument row |
|---|---|---|
| 120 | `▸ tickets  /jobs  list Tickets; /jobs <id> shows one; /jobs watch <id> follows it` | `▸ 7f3a91c2   ◆ Needs you  Ship the terminal-native review queue screen` |
| 100 | the same, whole | the same, whole |
| 80 | the hint is cut: `… /jobs watch…` | whole |
| 40 | the slash name drops, the hint is cut: `▸ tickets  list Tickets; /jobs…` | the subject is cut: `▸ 7f3a91c2   ◆ Needs you  Ship the…` |

The marker, the name or id, and the state glyph and word are pinned; they
never drop.

---

## 7. The screens

### 7.1 `home`

**What it is for.** What is running and what needs the operator.

**Regions.** identity bar → transcript → the command region (`commands`,
`tickets`/`reply`/`retry`/`help`) → composer → status bar.

**The transcript.** The greeting (`Conductor`, the instruction, the
`/ commands  @ attach  ? keys` line), the attention line
(`◆ 2 need you — /jobs --attention`), then the roster — one row per Ticket
that needs attention, with no header row of its own. The transcript is
anchored at the **top**: the roster follows the greeting.

**The columns.** `glyph(1) state(9) title(flexible) worker(8) age(6)
when(5) next(13)`.

| Column | 120 | 100 | 80 | 40 |
|---|---|---|---|---|
| `glyph` | `✔` `◆` `✖` | kept | kept | identity line |
| `state` | `Completed` | kept | kept | meta line, first |
| `title` | whole | whole | whole | identity line |
| `worker` | `kimi` | kept | kept | meta line |
| `age` | `12s` | kept | kept | meta line |
| `when` | `09:41` | kept | **dropped** | meta line |
| `next` | `/jobs 1` | kept | kept | meta line |

At 40 the row is (en, then zh-CN):

```
  ✔  Read the adapter registry
    Completed · /jobs 1 · kimi · 12s
    09:41
  ✔  读取适配器注册表
    已完成 · /jobs 1 · kimi · 12s
    09:41
```

The meta line wraps over two lines when it does not fit.

**The states.** The seven Ticket states of section 4.1, as glyph + word.

**The keys.** `ctrl+k` opens the palette, `/` at column 0 opens it
prefilled `/`, `j`/`k` move in the roster, `enter` acts on the highlighted
row, `alt+m` cycles the mode.

---

### 7.2 `/jobs`

**What it is for.** Every Ticket the session knows, newest first.

**Regions.** identity bar → transcript → the command region (`commands`,
`tickets`/`reply`/`retry`/`cancel`) → composer → status bar.

**The transcript.** The title `jobs` (`任务列表`), the table header, one row
per Ticket, and the summary `5 Tickets · 1 needs you · 1 running`
(`5 个 Ticket · 1 个需要你 · 1 个运行中`).

**The columns.** `mark(1) index(3) id(8) glyph(1) state(9) title(flexible)
worker(8) age(6) next(13)`.

| Column | 120 | 100 | 80 | 40 |
|---|---|---|---|---|
| `mark` | `▸` | kept | kept | identity line |
| `index` | `1` | kept | **dropped** | meta line, last |
| `id` | `7f782add` | kept | kept | meta line |
| `glyph` | `✔` | kept | kept | identity line |
| `state` | `Completed` | kept | kept | meta line, first |
| `title` | whole | whole | whole | identity line |
| `worker` | `kimi` | kept | en: **dropped**; zh-CN: kept | meta line |
| `age` | `12s` | kept | **dropped** | meta line |
| `next` | `/jobs 1` | kept | kept | meta line |

At 80 the English titles are wide enough that the row index, the worker
and the age go; the zh-CN titles are shorter in cells, so the worker stays.
At 40 the row is:

```
  ▸  ✔  Read the adapter registry
    Completed · /jobs 1 · 7f782add
    kimi · 12s · 1
```

**The states.** The seven Ticket states of section 4.1, as glyph + word.

**The keys.** As `home`, plus `enter` to open the highlighted Ticket.

---

### 7.3 `/dispatch`

**What it is for.** One dispatch: its lines, its roster and its DAG.

**Regions.** identity bar → transcript → the command region (`commands`,
`dispatch`/`approve`/`reject`/`review`) → composer → status bar.

**The transcript.** The dispatch header (`dispatch`,
`7f3a91c2  APPROVAL  62%  integrate/r2`, the subject, `waiting on a
human for G6`), the roster table (header and one row per line), the DAG
(`dag`, `L0: L1`, `L1: L2, L4`, `L2: L3`) and the unassigned block
(`unassigned`, `L4  worker pool exhausted`). zh-CN: `派发`, `待审批`,
`G6 正在等待人工决定`, `名册`, `依赖图`, `未分配`, `L4  执行者池已耗尽`.

**The columns.** `line(4) glyph(1) status(9) model(8) gate(8) fix(3)
verdict(7) depends(7)`. The roster has no flexible column: every column of
it is fixed, so it drops by priority like any other table.

| Column | 120 | 100 | 80 | 40 |
|---|---|---|---|---|
| `line` | `L1` | kept | kept | identity line |
| `glyph` | `✔` | kept | kept | identity line |
| `status` | `Completed` | kept | kept | identity line |
| `model` | `kimi` | kept | kept | meta line |
| `gate` | `reviewed` | kept | kept | meta line |
| `fix` | `1` | kept | kept | meta line |
| `verdict` | `✔ MERGE` | kept | kept | meta line |
| `depends` | `L1` | kept | kept | meta line |

The roster is 63 columns wide, so it is whole at 80. At 40 it is:

```
  L1    ✔  Completed
    kimi · reviewed · 1 · ✔ MERGE · —
  L1    ✔  已完成
    kimi · 已评审 · 1 · ✔ 合并 · —
```

**The states.** The Ticket states of section 4.1 for the line, and the
review verdict of section 4.2 for the verdict column.

**The keys.** As `home`, plus `approve` and `reject` from the command
region, which answer the gate that is waiting on a human (section 7.7).

---

### 7.4 `/review`

**What it is for.** The cross-reviews waiting for a decision.

**Regions.** identity bar → transcript → the command region (`commands`,
`review`/`approve`/`reject`/`merge`) → composer → status bar.

**The transcript.** The title `review queue` (`评审队列`), the table
header, one row per review, then the decision that is in front of the
operator: `G6 human gate  approve L3` (`G6 人工闸门  approve L3`),
`the reviewed commit is pinned at 4b2f7803`,
`conductor dispatch approve 7f3a91c2`.

**The columns.** `line(4) subject(flexible) reviewer(8) gate(8) fix(3)
verdict(7) decision(9)`.

| Column | 120 | 100 | 80 | 40 |
|---|---|---|---|---|
| `line` | `L3` | kept | kept | identity line |
| `subject` | whole | whole | whole | identity line |
| `reviewer` | `L1` | kept | kept | meta line, last |
| `gate` | `reviewed` | kept | kept | meta line |
| `fix` | `1` | kept | kept | meta line |
| `verdict` | `✔ MERGE` | kept | kept | meta line, first |
| `decision` | `✔ approve` | kept | kept | meta line |

At 40 the row is:

```
  L3    Add the review queue screen
    ✔ MERGE · ✔ approve · 1 · reviewed
    L1
```

**The states.** The review verdict and decision of section 4.2, as glyph +
word.

**The keys.** As `home`, plus `approve` / `reject` for the decision and
`merge` for the local merge behind it.

---

### 7.5 The running Ticket stream

**What it is for.** The stream of a Ticket in flight.

**Regions.** identity bar (the stream state is `● live`, `● 实时`) →
transcript → the command region (`commands`,
`status`/`details`/`stop`/`compact`) → composer → status bar
(`auto · ● 3 online` on the left, `esc interrupt · shift+enter newline ·
alt+m mode` on the right).

**The transcript.** The prompt, the conductor line (`♩ kimi is conducting on
branch feat/tui.`), the tool steps, the progress card, and the railed block
of the Worker output (`│ ▸ grep "review"`, `│ ╰ …/review_merge.py`, the
answer). Tool output — commands, paths, `128 passed in 4.21s` — is not
localized; the prose is.

**The card.** `mark(1) subject(flexible) worker(8) branch(11) id(8)
age(5)`, separated by the literal ` · ` so the card is one line of a
sentence:

```
  ♬ Add the review queue screen · kimi · feat/tui · 7f782add · 42s
```

| Column | 120 | 100 | 80 | 40 |
|---|---|---|---|---|
| `mark` | `♬` | kept | kept | identity line |
| `subject` | whole | kept | kept | identity line |
| `worker` | `kimi` | kept | kept | meta line |
| `branch` | `feat/tui` | kept | kept | meta line |
| `id` | `7f782add` | kept | kept | meta line |
| `age` | `42s` | kept | kept | meta line |

The card occupies 75 columns, so it is whole at 80. At 40 it is:

```
  ♬ Add the review queue screen
    kimi · feat/tui · 7f782add · 42s
```

**The rail.** The Worker block hangs behind `│ ` in `hold`. The text wraps
in the room the indent and the rail leave (section 3.4); at 40 the path
fills its line and continues under the rail:

```
  │ ╰ src/server/services/autonomy/revie
  │ w_merge.py
  │ 共 2 个注册名，未改任何文件。原生注
  │ 册表里的适配器名称至今仍按字母顺序排
```

**The states.** The card's mark is the spinner frame (`♪ ♫ ♬ ♫`), not a
state glyph — the Ticket is running, and the identity bar says so with
`● live` in `tempo`. The ink of the card is `tempo`.

**The keys.** `escape` interrupts the watch (the Ticket keeps running),
`ctrl+c` stops it. The command region carries `status`, `details`, `stop`
and `compact`.

---

### 7.6 Palette completion (`completion`)

**What it is for.** The answer to a command query.

**Regions.** identity bar → transcript (the greeting) → the open command
region: the query line `palette › /dsp▏`, the result rows, the footer →
composer (unchanged, the placeholder) → status bar with the palette keys.

**How it got there.** The operator typed `/` at column 0 of an empty
composer: the palette opened prefilled `/`, focused on the query. Then
`dsp`. The matcher of section 6.3 strips the `/`, folds the case, and
answers `dispatch` (indexes 0, 2, 3; gap cost 1). The same rows come back
for `ctrl+k` then `dsp`, or `DSP`.

```
  palette › /dsp▏
  ▸ dispatch      /dispatch   dispatch roster; /dispatch <id> reads one
  1 match · tab accepts · enter runs
```

zh-CN:

```
  命令面板 › /dsp▏
  ▸ dispatch      /dispatch   派发名册；/dispatch <id> 查看一个
  1 个匹配 · tab 接受 · enter 运行
```

**The columns.** The command row of section 6.2.

| Column | 120 | 100 | 80 | 40 |
|---|---|---|---|---|
| `mark` | `▸` | kept | kept | kept |
| `name` | `dispatch` | kept | kept | kept |
| `slash` | `/dispatch` | kept | kept | **dropped** |
| `hint` | whole | whole | whole | cut: `dispatch roster;…` |

The list answers only its query: the check forbids any other command on
this screen.

**The states.** None. The `▸` marker is the selection, not a state.

**The keys.** Section 5.2. `tab` gives `/dispatch`, `enter` runs
`dispatch` (its bare form is complete), `escape` closes and returns the
focus to the composer.

---

### 7.7 Argument completion (`completion-args`)

**What it is for.** Choosing the dispatch an `approve` answers.

**Regions.** As section 7.6; the query line is `palette › approve ▏`, the
rows are argument rows (section 6.4), the footer counts the offered ids.

```
  palette › approve ▏
  ▸ 7f3a91c2   ◆ Needs you  Ship the terminal-native review queue screen
    7f9e02b1   ◆ Needs you  Fix the narrow layout
  2 waiting for a person · tab or enter inserts the id
```

zh-CN:

```
  命令面板 › approve ▏
  ▸ 7f3a91c2   ◆ 需要你     把终端原生的评审队列屏幕上线
    7f9e02b1   ◆ 需要你     修复窄屏布局
  2 个等待人工 · tab 或 enter 插入 id
```

**The columns.** `mark(2) id(12) glyph(1) state(9) title(flexible,
truncatable)`. The marker, the id, the glyph and the word are pinned; the
subject is cut first (`Ship the…` at 40).

**What is offered.** Only the ids in phase `awaiting_human`. The running
dispatch `2c81e0d4` is not on the screen (the check forbids it).
`approve 7f` lists both and inserts nothing; `approve 7f3` +
`tab` gives `approve 7f3a91c2`; `enter` on `approve ` inserts the
highlighted id; `enter` on `approve 7f3a91c2` runs it. With no dispatch
waiting, the footer reads `approve needs a dispatch id` in `error`.

**The states.** The state of the dispatch, glyph + word (section 4.3).

**The keys.** Section 5.2.

---

### 7.8 The permission prompt

**What it is for.** The gate in front of a local action: merge, review,
shell.

**Regions.** identity bar → transcript → the command region (`commands`,
`approve`/`reject`/`help`) → composer → the status bar, taken over by the
permission.

**The transcript.** The greeting (two rows), a blank row, then the
permission card: the mark row, the action, the target and the three keys.

```
  ◆ permission: merge feat/tui
    action: local-merge
    into integrate/r2
    [a]llow once  [s]ession allow  [d]eny
```

zh-CN:

```
  ◆ 权限：merge feat/tui
    动作：local-merge
    合入 integrate/r2
    [a]允许一次  [s]本会话允许  [d]拒绝
```

The detail of the permission hangs under its mark. The hang is an indent
and not a part of the text, so a line that wraps keeps it.

**The layout at the four widths.**

| Region | 120 | 100 | 80 | 40 |
|---|---|---|---|---|
| Identity bar | 4 segments | 4 segments | 4 segments | 3 segments — the model and session drop |
| The card | 4 rows | 4 rows | 4 rows | en: 5 rows — the key row wraps over two lines; zh-CN: 4 rows |
| Command region | `mark` `name` `slash` `hint` | the same | the same | `mark` `name` `hint` — the slash name drops, the hint is cut |
| Composer | wall, 3 rows | wall, 3 rows | wall, 3 rows | no wall, 1 row |
| Status bar | the label is whole | the label is whole | en: the label is cut, `◆ merge feat/tui (local-merge) — into…`; zh-CN: whole | the label is cut: `◆ merge…` |

**What drops and what stacks.** The permission screen has no table of its
own: its card is prose, and prose wraps. The only columns on it are the
columns of the command region, and they follow section 6 — at 40 the slash
name of a command drops and its hint is cut, because the canonical name is
what the command is. The three keys of the status bar never drop: the
label gives way, the keys do not.

**The status bar.** The bar gives the label to the keys:

```
 ◆ merge feat/tui (local-merge) — into integrate/r2  a allow · s session · d deny
 ◆ merge feat/tui（local-merge）— 合入 integrate/r2  a 允许 · s 会话 · d 拒绝
```

**The states.** The gate is waiting for a person: `◆` in `hold`, the same
mark as `needs-you`. It is the only state on the screen, and it is on the
card and in the status bar.

**The keys.** `a` allows once, `s` allows for the session, `d` denies,
`escape` and `ctrl+c` deny. While a permission is pending, `enter`,
`up`/`down`, `alt+m` and **every** newline binding — `shift+enter`,
`ctrl+j` and `escape` then `enter` — are switched off, and `a`/`s`/`d`
answer the permission instead of typing.

---

## 8. The command list

Every command of the palette, in `palette.idleOrder` of the token set (28
commands). This is the order the palette shows when the query is empty.
`check_tokens` fails the build when the mock's copy drifts from
`palette.idleOrder` or from the slash name, aliases and hint of
`palette.commands`.

| # | Canonical name | Slash | Aliases | Hint (en) | Hint (zh-CN) |
|---|---|---|---|---|---|
| 1 | workers | `/worker` | `worker` | show which worker has the current Ticket | 显示当前 Ticket 由哪个 worker 执行 |
| 2 | dispatch | `/dispatch` | | dispatch roster; /dispatch <id> reads one | 派发名册；/dispatch <id> 查看一个 |
| 3 | approve | — | | approve a dispatch that is waiting for a person | 批准一个等待人工的派发 |
| 4 | reject | — | | reject a dispatch that is waiting for a person | 驳回一个等待人工的派发 |
| 5 | tickets | `/jobs` | `/j`, `jobs`, `/attach` | list Tickets; /jobs <id> shows one; /jobs watch <id> follows it | 列出 Ticket；/jobs <id> 查看一个；/jobs watch <id> 跟随它 |
| 6 | workspace | `/workspace` | | switch the Home workspace | 切换 Home 工作区 |
| 7 | reply | `/reply` | | answer the Ticket that needs you | 回复需要你的 Ticket |
| 8 | retry | `/retry` | | retry a failed or waiting Ticket | 重试失败或等待中的 Ticket |
| 9 | cancel | `/cancel` | | stop a running Ticket (/cancel <id\|index>) | 停止运行中的 Ticket（/cancel <id\|index>） |
| 10 | resume | `/resume` | | list saved sessions; /resume <id> reopens one | 列出已保存的会话；/resume <id> 重新打开一个 |
| 11 | status | `/status` | | reread Supervisor progress for the current Ticket | 重新读取当前 Ticket 的 Supervisor 进度 |
| 12 | help | `/help` | `?` | show the TUI key map and commands | 显示 TUI 按键和命令 |
| 13 | quit | `/quit` | `/exit`, `/q`, `exit` | quit the TUI | 退出 TUI |
| 14 | skills | `/skills` | | local skills status | 本地技能状态 |
| 15 | mcp | `/mcp` | | local MCP server status | 本地 MCP 服务器状态 |
| 16 | memory | `/memory` | | browse remembered memory | 浏览已记住的记忆 |
| 17 | agents | `/agents` | | local fleet, native runtimes, daemons | 本地机群、原生运行时、守护进程 |
| 18 | review | `/review` | | queue a local cross-review (/review run <line> --by <line>) | 排队一次本地交叉评审（/review run <line> --by <line>） |
| 19 | merge | `/merge` | | merge a line locally after its tests (/merge <line> --into <branch>) | 测试通过后在本地合并一条线（/merge <line> --into <branch>） |
| 20 | stop | `/stop` | | stop a running Ticket (/stop <id\|index>) | 停止运行中的 Ticket（/stop <id\|index>） |
| 21 | signals | `/signals` | | Sources and job counts | 来源和任务计数 |
| 22 | clear | `/clear` | | clear the conversation | 清空对话 |
| 23 | details | `/details` | | toggle Worker details (default: Supervisor view) | 切换 Worker 详情（默认：Supervisor 视图） |
| 24 | model | `/model` | | show the Router model and configured providers | 显示 Router 模型和已配置的提供方 |
| 25 | compact | `/compact` | | drop activity noise and duplicate rows from the transcript | 从记录中去掉活动噪声和重复行 |
| 26 | expand | `/expand` | | expand every tool block | 展开所有工具块 |
| 27 | collapse | `/collapse` | | collapse every tool block | 折叠所有工具块 |
| 28 | new | `/new` | | start a fresh session (persisted) | 开始一个新会话（会保存） |

`approve` and `reject` have no slash name: they are reachable by their
canonical name only, and the composer mirrors them as
`conductor dispatch approve <id>` and `conductor dispatch reject <id>`.
`/jobs`, `/j` and `jobs` all reach `tickets`. A command that cannot run
bare — `approve`, `reject`, `reply`, `review`, `merge`, `stop` — gets a
trailing space when `tab` accepts it.

---

## 9. The artefacts

All in `docs/design/terminal-native/tui/`, written by the mock and nothing
else. Eight screens; each in two languages (`en`, `zh-CN`) at the three
contract widths, as text and as SVG; plus the primary render of each
screen. **112 files**:

| Kind | Name | Count |
|---|---|---|
| Primary render, English, **100 × 32** (the width of the native view, `tui_rich.DEFAULT_WIDTH = 100`) | `tui-<screen>.svg`, `tui-<screen>.txt` | 8 × 2 = 16 |
| Every language at every contract width, **32 rows** | `tui-<screen>-<lang>-w<cols>.svg`, `tui-<screen>-<lang>-w<cols>.txt` | 8 × 2 × 3 × 2 = 96 |

`<screen>` is one of `home`, `jobs`, `dispatch`, `review`, `running`,
`completion`, `completion-args`, `permission`; `<lang>` is `en` or
`zh-CN`; `<cols>` is `40`, `80` or `120`. For example
`tui-home-zh-CN-w40.txt`, `tui-completion-args-en-w120.svg`.

| Screen | Primary | Per language and width |
|---|---|---|
| home | `tui-home.svg` `tui-home.txt` | `tui-home-{en,zh-CN}-w{40,80,120}.{svg,txt}` |
| `/jobs` | `tui-jobs.svg` `tui-jobs.txt` | `tui-jobs-{en,zh-CN}-w{40,80,120}.{svg,txt}` |
| `/dispatch` | `tui-dispatch.svg` `tui-dispatch.txt` | `tui-dispatch-{en,zh-CN}-w{40,80,120}.{svg,txt}` |
| `/review` | `tui-review.svg` `tui-review.txt` | `tui-review-{en,zh-CN}-w{40,80,120}.{svg,txt}` |
| running | `tui-running.svg` `tui-running.txt` | `tui-running-{en,zh-CN}-w{40,80,120}.{svg,txt}` |
| completion | `tui-completion.svg` `tui-completion.txt` | `tui-completion-{en,zh-CN}-w{40,80,120}.{svg,txt}` |
| completion-args | `tui-completion-args.svg` `tui-completion-args.txt` | `tui-completion-args-{en,zh-CN}-w{40,80,120}.{svg,txt}` |
| permission | `tui-permission.svg` `tui-permission.txt` | `tui-permission-{en,zh-CN}-w{40,80,120}.{svg,txt}` |

* An `.svg` carries the colour of the dark theme and points at local fonts
  only (JetBrains Mono, then the system monospace and a CJK fallback), so
  it opens offline.
* A `.txt` is the glyph grid of the same render, trailing spaces trimmed.
* The older names `tui-<screen>-w40.txt`, `-w80.txt`, `-w120.txt` are gone;
  the mock deletes any `tui-*` artefact it did not write.

### 9.1 Running the mock

```
python3 docs/design/terminal-native/tui/tui_mock.py
```

The token set belongs to the sibling line, and the script looks for it on
its own: the default is `docs/design/terminal-native/system/tokens.json`,
resolved from the script's own directory, so the command is the same from
the repository root and from anywhere else. To point it at another file:

```
python3 docs/design/terminal-native/tui/tui_mock.py --tokens docs/design/terminal-native/system/tokens.json
```

On a branch that does not have the token set yet, the mock checks the
**layout without colour** and leaves the artefacts untouched.

The last two lines of a good run are:

```
8 screens x 2 languages x 3 widths, plus 8 primary renders: 112 files
layout contract holds: 54 checks, 2 languages, 120, 100, 80, 40 columns
```

The mock imports `rich` and the standard library, and nothing else.

---

## 10. The checks

The mock is the contract. Every check runs before anything is written;
one failure means exit 1 and no artefact touched.

| Check | What it proves |
|---|---|
| `check_contract` | for every screen, in both languages, at 120, 100, 80 and 40: 32 rows; every row fits the terminal; rich's exported grid is the layout's grid (rich did not wrap or widen a line); the composer prompt and the status bar are there; every anchor is on screen (state glyphs and words, the ids, the query of an open palette, the offered argument ids); nothing forbidden is (`shift+tab` anywhere, a command the query does not match, a dispatch that is not `awaiting_human` on the argument screen) |
| `check_lossless` | for every screen, in both languages, at every width: every text row of the transcript and the command region — railed or not — keeps every character after wrapping, and no line, indent and prefix included, is wider than the terminal |
| `check_prefix_wrap` | wrapping counts the indent and the prefix: the review's case (19 Chinese characters, indent 2, rail `│ `, 40 columns) and the other cases of `ROW_WRAP_CASES` keep every character, wrap instead of cutting, and fit in the terminal measured by the layout **and** by rich |
| `check_wrapping` | the wrapping helper keeps every character, never opens a line with a closing mark, and draws a wide character in a room of one column |
| `check_stack_coverage` | a narrow row stacks **every** column of its table, and the meta line keeps the whole text of every cell |
| `check_long_titles` | a long title — an English one and a Chinese one, which is twice as wide — keeps the marker and the state glyph of its row at 120, 100, 80 and 40, and the narrow form keeps the title whole |
| `check_palette_match` | D1: `dsp`, `DSP`, `/dsp`, `Disp` → dispatch first; `jobs`, `/jobs`, `tkt` → tickets; `apv` → approve; `zzz` → no rows and `no command matches`; gap cost of `dsp` in `dispatch` is 1; the empty query is the idle order. D2: `dispatch ` offers every id; `approve ` / `reject ` only `awaiting_human`; `reply ` nothing; an ambiguous prefix inserts nothing; a unique prefix completes; the prefix is case-sensitive; Tab adds the trailing space only when an argument is still needed; Enter inserts on an incomplete form, runs a complete one, and shows `approve needs a dispatch id` / `approve 需要一个 dispatch id` when nothing is offered |
| `check_locales` | en and zh-CN carry the same string keys; every state has a zh-CN word; every command has a zh-CN hint |
| `check_tokens` | the mock agrees with `../system/tokens.json`: the seven status ids in order with glyph, ASCII twin, word and ink; the 28 commands of `palette.idleOrder` with their slash names, aliases and hints, and whether each runs bare; `completionLimit` 60; the key names `ctrl+k`, `/`, `escape`, `alt+n`, `ctrl+down`, `ctrl+up`; `focusOrder` and `paletteTrap`; `palette.fuzzy` is a subsequence matcher with `caseSensitive: false` and a `fold` |

---

## 11. Languages

The TUI is drawn in English and in zh-CN. A localized UI translates the
prose and the labels; it does **not** translate what the operator types or
what a program printed:

| Localized | Not localized |
|---|---|
| greetings, instructions, summaries, table headers, the region header, the palette label and footer, status-bar hint words, the placeholder, the permission card and label, the identity bar's stream and session words, state / verdict / decision / gate words, command hints, Ticket and dispatch titles | command names and slash names, key names (`ctrl+k`, `alt+m`, `shift+enter`), ids, branches, paths, models, worker names, tool output, `Conductor`, `Super-agent`, `Ticket`, `Home`, `Worker`, `Supervisor` |

The glyphs are the same in every language. A zh-CN cell is two columns
wide, so every layout rule is checked in both languages: the state column
holds `Needs you` (9) and `需要你` (6); a zh-CN title wraps under its
marker at 40; the CJK run fills its line and never opens a line with a
closing mark (section 3.4).

| Chrome | en | zh-CN |
|---|---|---|
| stream | `○ idle` / `● live` | `○ 空闲` / `● 实时` |
| counts | `auto · ● 3 online` | `自动 · ● 3 在线` |
| hints | `shift+enter newline`, `alt+m mode`, `? help`, `esc interrupt` | `shift+enter 换行`, `alt+m 模式`, `? 帮助`, `esc 中断` |
| palette hints | `tab accept`, `ctrl+down list`, `esc close`, `alt+n nav` | `tab 接受`, `ctrl+down 列表`, `esc 关闭`, `alt+n 导航` |
| region header | `commands` | `命令` |
| query label | `palette › ` | `命令面板 › ` |
| permission keys | `a allow · s session · d deny` | `a 允许 · s 会话 · d 拒绝` |

---

## 12. What the next line inherits

* The layout of the eight screens, at four widths and in two languages, as
  the artefacts above.
* The priority ladder of section 3.2, the stack rule of section 3.3 and the
  wrap rule of section 3.4, as the rules any new table or prose row follows.
* The state vocabulary of section 4, taken from the shared token set, with
  the zh-CN words of section 4.1.
* The keybindings of section 5, including the change of section 5.6
  (`shift+tab` → `alt+m` for the mode; `SlashCompleter` → the shared
  fuzzy palette).
* The palette of section 6: docked, opened by `ctrl+k` or `/` at column 0,
  one matcher (D1), argument completion (D2).
