# Terminal-native implementation plan

Input for implementation rounds **F1…F6**. Each slice below is one dispatch
line: its **Goal** paragraph is the dispatch goal (≤ 800 characters, checked
by the script in [Checks](#checks)), its **Files owned** are the only paths it
may write, and no path appears under two slices ([ownership matrix](#ownership-matrix)).

Design inputs (read-only for every slice; never copied into product code by
hand without the sync test the slice names):

- tokens and grammar: [`../system/tokens.json`](../system/tokens.json),
  [`../system/SPEC.md`](../system/SPEC.md)
- web surfaces: [`../web/`](../web/) (`palette.html`, `home.html`,
  `tickets.html`, `dispatch.html`, `ticket.html`, `README.md`)
- TUI screens and the layout contract: [`../tui/SPEC.md`](../tui/SPEC.md) and
  the `../tui/tui-*-{en,zh-CN}-w{40,80,120}.txt` frames
- current state with file:line evidence: [`AUDIT.md`](AUDIT.md)

Binding rules every slice inherits:

- **D1 matcher**: one case-insensitive fuzzy subsequence matcher (needle and
  haystacks lower-cased, one leading `/` stripped; haystacks = canonical name,
  slash name, aliases; rank exact > prefix > lower gap cost > earlier start >
  name; empty needle = `palette.idleOrder`; max 60 rows). The shipped
  `SlashCompleter` case-sensitive prefix rule (`tui_completion.py:101`) is
  current shipped behaviour that slice S04 replaces, not the target.
- **D2 arguments**: after the command token + space, unique case-sensitive
  prefix of an id, never fuzzy; `approve`/`reject` without an id never run.
- **D3 keys**: Shift+Tab = previous focus on both surfaces; mode cycle moves
  to Alt+M; Tab accepts a completion and never leaves the palette.
- **Colours**: only the 25 hex values of `tokens.json` (plus its own `rgba()`
  shadows and `color-mix()` of token inks).
- **Bilingual**: every UI slice keeps **en-US and zh-CN** working; long
  strings in both languages must not clip actions or break the layout.

## Order

| Round | Slices | Why |
|---|---|---|
| F1 | S01 web tokens · S02 TUI tokens | token stage first, in parallel |
| F2 | S03 web command palette | the palette, immediately after the tokens, as its own slice |
| F3 | S04 TUI palette engine · S05 TUI Home · S06 TUI `/jobs` · S07 TUI `/review` · S08 TUI dispatch view · S09 TUI permission prompt · S12 web shell chrome · S13 web UI primitives | additive TUI renderers (no `tui.py` edits) and the web chrome |
| F4 | S10 TUI keymap + integration (`tui.py`) · S11 line shell · S14 web Inbox · S17 web Dispatch | the one `tui.py` switch-over; Inbox owns `superagent.css` |
| F5 | S15 web Tickets · S16 web Ticket (both depend on S14) · S18–S29 remaining web pages | one page per line |
| F6 | S30 main locale catalogs sweep | the only writer of `en-US.json` / `zh-CN.json` |

## Shared-file rules

| Shared file | Single owner | How the others avoid it |
|---|---|---|
| `web/src/App.tsx` (top-level router + `AuthenticatedShell`) | **S03** | S03 mounts `<PaletteHost />` there; no other slice edits it |
| Ctrl+K / Alt+M global listener | **S03** (`features/palette/PaletteHost.tsx`) | others call the exported `openPalette(opener)`; nobody adds a second listener |
| `web/src/components/Main/Main.tsx` (inner router) | **S12** | no slice changes routes; pages are restyled in place |
| `web/src/locales/en-US.json`, `zh-CN.json`, `locales/__tests__/keys.test.ts` | **S30** (last) | frozen until F6: new or changed copy goes into the slice's **own** feature locale files (below) |
| `web/src/styles/tokens.css`, `index.css`, `main.tsx` | **S01** | others use the Tailwind theme / custom properties S01 defines |
| `web/e2e/fixtures.js` | none (not edited) | specs pass `mockAPI(page, overrides)` |
| `web/e2e/terminal-native/matrix.js` (screenshot helper) | **S01** | import-only for every other slice |
| `web/src/lib/featureLocales.ts` | **S03** | import-only |
| `web/src/features/superagent/superagent.css` | **S14** | other superagent pages add their own new CSS file |
| `libs/ptc-cli/ptc_cli/superagent/tui.py` and every test that imports it | **S10** | S04–S09 add renderers in their own modules; S10 switches `tui.py` to them and refreshes the snapshots |
| `libs/ptc-cli/ptc_cli/superagent/tui_theme.py` | **S02** | others read it (keep public names) |
| `libs/ptc-cli/ptc_cli/superagent/tui_i18n.py`, `tests/unit_tests/tui_layout_contract.py` | **S02** | import-only |

**Web locale disjointness rule.** The catalogs are two single JSON files
(`web/src/locales/{en-US,zh-CN}.json`, 4980 lines, one top-level namespace
per area, lazy-loaded by `web/src/i18n.ts`). Two slices may not edit them, so
each UI slice creates its own pair
`<its directory>/locales/tn<Slice>.{en-US,zh-CN}.json` holding exactly one
top-level namespace `tn<Slice>` (e.g. `tnPalette`, `tnInbox`). The feature
module imports both files and registers them at module load through S03's
`registerFeatureLocales()` (`i18n.addResourceBundle(lng, 'translation', …,
deep=true, overwrite=false)`), so `i18n.ts`, `scripts/locale-preload.ts` and
`src/test/setup.ts` stay untouched. Each slice's own `__tests__` asserts
en-US/zh-CN key parity of its pair with `assertParity()` from
`featureLocales.ts`, and that `tn<Slice>` collides with no main-catalog
namespace. Old keys a slice stops using are deleted only by S30. The existing
unused `commandPalette.*` block (no `t('commandPalette.…')` call in `web/src`)
is not reused; S30 deletes it.

**TUI string rule.** TUI copy lives next to the renderer that prints it, as a
`STRINGS = {"en": {...}, "zh-CN": {...}}` table in that slice's module, read
through `tui_i18n.t(STRINGS, key)` (S02). Locale: `CONDUCTOR_LOCALE`, else
`LC_ALL` / `LC_MESSAGES` / `LANG` starting with `zh` → zh-CN, else en.

## Validation every slice runs

Web slices:

```bash
cd web && pnpm typecheck && pnpm test && pnpm lint
cd web && pnpm exec playwright test e2e/terminal-native/<slice spec>.spec.js
```

The screenshot matrix (`shootMatrix` from S01's `matrix.js`) writes 8 PNGs per
page — `{dark,light} × {1440,390} × {en-US,zh-CN}` — to
`web/test-results/terminal-native/<slice>/<name>-<theme>-<width>-<locale>.png`
(`/test-results/` is already in `web/.gitignore`; the reviewer opens them).
Theme is set through `localStorage.theme`, locale through the `locale`
cookie. Every web spec also asserts: Tab reaches each primary control, the
2px `ink.focus` ring is visible on keyboard focus, and with
`reducedMotion: 'reduce'` no computed transition/animation duration is > 0.

TUI slices:

```bash
cd libs/ptc-cli && uv run pytest <the slice's test files> -q
```

The **layout contract** is `tests/unit_tests/tui_layout_contract.py` (S02):
`check_layout(render, widths=(40, 80, 120), locales=("en", "zh-CN"), keep=…)`
fails when a row is wider than the width (measured with `rich.cells.cell_len`,
so CJK counts 2), a kept row (composer prompt, status bar) is lost, a state
loses its glyph or word, a prose row loses a character, or a narrow row drops
a column instead of stacking it — the rules of `../tui/SPEC.md` §1.1.

---

## S01 · Web tokens → Tailwind 4 theme

**Round:** F1 · **Depends on:** none (the design branch with `tokens.json` merged)

**Goal:** Make docs/design/terminal-native/system/tokens.json the only colour, type, space, radius and motion source of the web app. New web/src/styles/terminal-native.css declares the kebab-case custom property of each token path (--surface-page, --ink-on-accent, …) for dark (default) and [data-theme=light], plus reduced motion; index.css exposes them in its Tailwind 4 @theme inline block (bg-surface-page, text-ink-on-accent, rounded-control, font-mono). tokens.css keeps every legacy --color-* name as an alias of a new property so unconverted pages still render. Delete styles/conductor.css and its import in main.tsx. Add the shared Playwright screenshot helper. Only the 25 token hex values may appear in owned files; en-US and zh-CN keep rendering.

**Files owned:**
- `web/src/styles/terminal-native.css` (new)
- `web/src/styles/tokens.css`
- `web/src/index.css`
- `web/src/styles/conductor.css` (deleted)
- `web/src/main.tsx`
- `web/src/styles/__tests__/tokenRefs.test.ts`
- `web/src/styles/__tests__/terminalNativeTokens.test.ts` (new)
- `web/e2e/terminal-native/matrix.js` (new)
- `web/e2e/terminal-native/tokens.spec.js` (new)

**Tests:**
```bash
cd web && pnpm typecheck && pnpm test && pnpm lint
cd web && pnpm exec playwright test e2e/terminal-native/tokens.spec.js
```

**Acceptance:**
- `terminalNativeTokens.test.ts` (node env) reads the repo's `tokens.json` and asserts every property in `terminal-native.css` equals its token value in both themes, and fails on any hex in `terminal-native.css` / `tokens.css` / `index.css` outside the 25.
- `conductor.css` is gone and `grep -c conductor web/src/main.tsx` prints 0 (AUDIT §6 #1).
- `tokenRefs.test.ts` still passes: every referenced `--color-*` is declared.
- `matrix.js` exports `shootMatrix(page, { slice, name, path })` producing the 8 PNGs described above, using `mockAPI` from `e2e/fixtures.js` (not edited).
- `tokens.spec.js` shoots `/inbox` and `/settings` through the full matrix; focus ring visible; reduced motion holds.
- en-US and zh-CN both render at 1440 and 390 with no horizontal page scroll.

---

## S02 · TUI tokens → `tui_theme.py`, i18n and layout-contract helpers

**Round:** F1 · **Depends on:** none

**Goal:** Make tui_theme.py the TUI's single copy of system/tokens.json: dark and light truecolour inks, the xterm-256 and ansi16 fallbacks, the seven status ids with glyphs ○ ● ▣ ◆ ✖ ✔ ⊘ and ASCII o * # ? x + -, and reduced motion (PTC_REDUCED_MOTION=1, TERM=dumb). baton/accent take the token accent instead of #8B9CFF / #3F4FC8. Keep every public name (resolve_theme, style_for, contrast_ratio, text_tokens, grounds_for) and style role so tui.py and the snapshots stay untouched. Add new tui_i18n.py (locale from CONDUCTOR_LOCALE, else LC_ALL/LC_MESSAGES/LANG zh* -> zh-CN, else en; t(table, key)) and the shared layout-contract helper every TUI screen test imports.

**Files owned:**
- `libs/ptc-cli/ptc_cli/superagent/tui_theme.py`
- `libs/ptc-cli/tests/unit_tests/test_tui_theme.py`
- `libs/ptc-cli/ptc_cli/superagent/tui_i18n.py` (new)
- `libs/ptc-cli/tests/unit_tests/test_tui_i18n.py` (new)
- `libs/ptc-cli/tests/unit_tests/tui_layout_contract.py` (new; helper module, not collected)

**Tests:**
```bash
cd libs/ptc-cli && uv run pytest tests/unit_tests/test_tui_theme.py tests/unit_tests/test_tui_i18n.py -q
cd libs/ptc-cli && uv run pytest tests/unit_tests/test_tui_snapshots.py tests/unit_tests/test_superagent_tui_units.py -q   # unchanged, must stay green
```

**Acceptance:**
- `test_tui_theme.py` loads the repo `tokens.json` and asserts each ink, xterm-256 index and ansi16 name equals `tui_theme`'s value for dark and light (fixes AUDIT §6 #2).
- No hex literal in `tui_theme.py` outside the 25 token values.
- `tui_layout_contract.check_layout` implements the five rules above with `rich.cells.cell_len`; its own self-test in `test_tui_i18n.py` shows a 41-cell row failing at 40.
- `tui_i18n.t()` raises in tests when a zh-CN key is missing from a table (parity), and falls back to en at runtime.
- The snapshot style files (role letters, not hex) pass unchanged.

---

## S03 · Web command palette (first feature slice)

**Round:** F2 · **Depends on:** S01

**Goal:** Build the web command palette in new web/src/features/palette/ per system/SPEC.md (Keyboard, Palette grammar) and web/palette.html. App.tsx (sole owner) mounts <PaletteHost/> in AuthenticatedShell; PaletteHost owns the only global Ctrl+K and Alt+M listeners and exports openPalette(opener). Matcher = D1 (case-insensitive fuzzy, ranked, 60 rows, idleOrder when empty); arguments = D2 (dispatch ids; approve/reject only awaiting_human; incomplete approve/reject inserts the id or shows the needs-id hint; reply offers nothing); keys = D3 (Tab accepts, Shift+Tab inert, Ctrl+Up/Down, Alt+N, Esc restores the opener). Copy in new tnPalette en-US/zh-CN files via new lib/featureLocales.ts. Approve/reject open /dispatch/:id?confirm=…, never act silently. IME-composing Enter never acts (SPEC); test it.

**Files owned:**
- `web/src/App.tsx` (sole owner: one import + one `<PaletteHost />` inside `AuthenticatedShell`)
- `web/src/features/palette/PaletteHost.tsx` (new)
- `web/src/features/palette/CommandPalette.tsx` (new)
- `web/src/features/palette/matcher.ts` (new)
- `web/src/features/palette/commands.ts` (new)
- `web/src/features/palette/argComplete.ts` (new)
- `web/src/features/palette/composerMode.ts` (new)
- `web/src/features/palette/palette.css` (new)
- `web/src/features/palette/locales/tnPalette.en-US.json` (new)
- `web/src/features/palette/locales/tnPalette.zh-CN.json` (new)
- `web/src/features/palette/__tests__/matcher.test.ts` (new)
- `web/src/features/palette/__tests__/argComplete.test.ts` (new)
- `web/src/features/palette/__tests__/CommandPalette.keyboard.test.tsx` (new)
- `web/src/features/palette/__tests__/commandsSync.test.ts` (new)
- `web/src/features/palette/__tests__/locales.test.ts` (new)
- `web/src/lib/featureLocales.ts` (new)
- `web/src/lib/__tests__/featureLocales.test.ts` (new)
- `web/e2e/terminal-native/palette.spec.js` (new)

**Tests:**
```bash
cd web && pnpm typecheck && pnpm test && pnpm lint
cd web && pnpm test src/features/palette src/lib/__tests__/featureLocales.test.ts
cd web && pnpm exec playwright test e2e/terminal-native/palette.spec.js
```

**Acceptance:**
- `matcher.test.ts` iterates `palette.fuzzy.examples` from `tokens.json` and the D1 list: `dsp` → dispatch first (gap 1), `DSP` → dispatch, `jobs` → tickets (alias), `Disp` → dispatch first, `tkt` → tickets, `apv` → approve, `zzz` → no rows; empty needle returns the 28 `idleOrder` rows unchanged; never more than 60 rows.
- `commandsSync.test.ts` asserts `commands.ts` matches `palette.commands` / `idleOrder` in `tokens.json` (28 commands, slash names, aliases).
- `argComplete.test.ts` (`listActiveDispatches` mocked): `dispatch ` lists all active ids; `approve ` / `reject ` list only `awaiting_human` ids; unique case-sensitive hex prefix inserts, an ambiguous prefix lists and inserts nothing; `reply ` offers nothing.
- `CommandPalette.keyboard.test.tsx`: Ctrl+K opens with the query focused; Tab inserts the highlighted token (+ space when an argument is still needed) and focus stays in the palette; Shift+Tab does nothing; Enter on `approve` with an offered id inserts it, with none shows `approve needs a dispatch id` / `approve 需要一个 dispatch id` and runs nothing; Enter on a complete form runs it; Esc closes, clears and `document.activeElement` is the opener again; Ctrl+Down / Ctrl+Up move query ↔ list; j/k insert text unless Alt+N navigation mode; clicking a row activates it exactly like Enter; Alt+M (Alt only, `code === 'KeyM'`, `preventDefault`) cycles auto → dispatch → read-only → auto.
- `zzz` shows `no command matches` / `没有匹配的命令`.
- `git diff web/src/App.tsx` is the import plus one element; `grep -rn "KeyK\|KeyM" web/src --include='*.ts*'` hits only `features/palette/`.
- `featureLocales.test.ts` covers `registerFeatureLocales`, `assertParity` and the namespace-collision check; the main catalogs are not edited.
- `palette.spec.js` runs the matrix over `/inbox` for four states — idle, `dsp`, `approve ` (arguments), `zzz` (empty): 32 PNGs; long zh-CN hints are not clipped at 390.

---

## S04 · TUI palette engine and overlay renderer

**Round:** F3 · **Depends on:** S02, S03 (same D1/D2 contract and example table)

**Goal:** Implement the TUI palette engine and overlay renderer, mirroring S03. New tui_palette.py holds the 28-command table of tokens.json palette.commands, the D1 matcher (case-insensitive fuzzy subsequence; rank exact > prefix > gap cost > start > name; 60 rows; idleOrder when empty), D2 argument completion (unique case-sensitive id prefix; dispatch ids of all phases; approve/reject only awaiting_human; incomplete approve/reject inserts the highlighted id or shows 'approve needs a dispatch id' / zh-CN; reply offers nothing) and render_palette(state, width, locale). In tui_completion.py, SlashCompleter's case-sensitive startswith prefix rule is replaced by the D1 matcher and TuiCompleter routes post-space text to argument completion. No tui.py edits.

**Files owned:**
- `libs/ptc-cli/ptc_cli/superagent/tui_palette.py` (new)
- `libs/ptc-cli/ptc_cli/superagent/tui_completion.py`
- `libs/ptc-cli/tests/unit_tests/test_tui_palette.py` (new)

**Tests:**
```bash
cd libs/ptc-cli && uv run pytest tests/unit_tests/test_tui_palette.py -q
cd libs/ptc-cli && uv run pytest tests/unit_tests/test_superagent_tui_units.py tests/unit_tests/test_superagent_hidden_text.py tests/unit_tests/test_superagent_dispatch_view.py -q   # not edited, must stay green
```

**Acceptance:**
- `test_tui_palette.py` runs the same example table as S03 (from `palette.fuzzy.examples`): `dsp`, `DSP`, `jobs`, `Disp`, `tkt`, `apv`, `zzz`; and through `SlashCompleter`, `/DSP` completes `/dispatch`.
- Command table equals `tokens.json` `palette.commands` (28, `idleOrder`); `approve` / `reject` have no slash name; `/jobs`, `/j`, `jobs` alias tickets.
- Argument cases of S03 pass against an injected id provider.
- `SLASH_COMMANDS`, `SlashCompleter`, `TuiCompleter`, `AtFileCompleter`, `expand_mentions`, `slash_help_lines` keep their names; `test_slash_completer_completes_commands` (`/jo` → `["/jobs"]`, `/jobs ` → `[]`) passes unedited.
- `render_palette` passes `check_layout` at 40/80/120 × en/zh-CN for idle, `dsp`, `approve ` with ids, `approve` incomplete (hint) and `zzz` (`no command matches` / `没有匹配的命令`); frames follow `../tui/tui-completion-{en,zh-CN}-w{40,80,120}.txt` and `../tui/tui-completion-args-{en,zh-CN}-w{40,80,120}.txt`.

---

## S05 · TUI screen: Home

**Round:** F3 · **Depends on:** S02

**Goal:** Add new tui_home.py: a pure renderer home_rows(tickets, attention, theme, locale, width) for the Home screen of tui/SPEC.md §7.1 — needs-you count first, then recent Tickets with state glyph and word from tui_theme, title, worker, age and the next command with its digit index — in en and zh-CN through tui_i18n, stacking per §3.3 at 40 columns. It covers everything tui.py _home_rows shows today (heading, hint line, attention row, numbered rows) in the new layout. tui.py is not edited; S10 switches Home to this module.

**Files owned:**
- `libs/ptc-cli/ptc_cli/superagent/tui_home.py` (new)
- `libs/ptc-cli/tests/unit_tests/test_tui_home.py` (new)

**Tests:**
```bash
cd libs/ptc-cli && uv run pytest tests/unit_tests/test_tui_home.py -q
```

**Acceptance:**
- `check_layout` passes at **40 / 80 / 120** columns in **en** and **zh-CN** for: empty Home, 3 Tickets, 9 Tickets with 2 needing you.
- All seven states show glyph + word; with `PTC_ASCII=1` the ASCII glyphs.
- A zh-CN title longer than the column is truncated with `…` on a cell boundary (never half a wide character); an en title of 80 characters likewise.
- Row structure matches `../tui/tui-home-{en,zh-CN}-w{40,80,120}.txt` (same column order and stacking; data may differ).

---

## S06 · TUI screen: `/jobs`

**Round:** F3 · **Depends on:** S02

**Goal:** Add new tui_jobs.py: pure renderers for the /jobs screen of tui/SPEC.md §7.2 — the Ticket list with filters, one Ticket's detail, the watch header used by /jobs watch and /attach, and the --attention view — with state glyph and word from tui_theme, en and zh-CN copy through tui_i18n, and the §3.2 fit rule and §3.3 stack rule at narrow widths. It covers every row tui.py _jobs_command prints today ('no Jobs', list lines, needs-you question, summary). tui.py is not edited; S10 switches /jobs, /j and /attach to it.

**Files owned:**
- `libs/ptc-cli/ptc_cli/superagent/tui_jobs.py` (new)
- `libs/ptc-cli/tests/unit_tests/test_tui_jobs.py` (new)

**Tests:**
```bash
cd libs/ptc-cli && uv run pytest tests/unit_tests/test_tui_jobs.py -q
```

**Acceptance:**
- `check_layout` passes at **40 / 80 / 120** in **en** and **zh-CN** for list (one row per state), detail, watch header, `--attention`, and empty.
- At 40 columns columns stack, never drop (§3.3); the id stays copyable in full.
- Long zh-CN and en titles/questions wrap or truncate per §3.4 without losing the state word.
- Row structure matches `../tui/tui-jobs-{en,zh-CN}-w{40,80,120}.txt`.

---

## S07 · TUI screen: `/review`

**Round:** F3 · **Depends on:** S02

**Goal:** Add new tui_review.py: pure renderers for the /review screen of tui/SPEC.md §7.4 — the usage error ('usage: /review run <line> --by <line> | /review status [line]'), a queued or running review with progress, and the result rows using the review states of §4.2 (verdict glyph, word, reviewer line, model) — with en and zh-CN copy through tui_i18n and stacking at 40 columns. It does not run subprocesses; tui_local.py stays as it is. tui.py is not edited; S10 switches /review run and /review status output to it.

**Files owned:**
- `libs/ptc-cli/ptc_cli/superagent/tui_review.py` (new)
- `libs/ptc-cli/tests/unit_tests/test_tui_review.py` (new)

**Tests:**
```bash
cd libs/ptc-cli && uv run pytest tests/unit_tests/test_tui_review.py -q
```

**Acceptance:**
- `check_layout` passes at **40 / 80 / 120** in **en** and **zh-CN** for usage error, running, and a result with every review verdict.
- Each verdict shows glyph + word (and ASCII with `PTC_ASCII=1`).
- Row structure matches `../tui/tui-review-{en,zh-CN}-w{40,80,120}.txt`.

---

## S08 · TUI screen: dispatch view (additive)

**Round:** F3 · **Depends on:** S02

**Goal:** Add render_dispatch_view(payload, width, locale) to dispatch_view.py for the /dispatch screen of tui/SPEC.md §7.3: lines L1–L4, provider and model per line, review verdicts, and the awaiting_human approve/reject decision row with the exact approve <id> / reject <id> commands, in en and zh-CN through tui_i18n and stacking at 40 columns. render_dispatch_lines keeps its current output so tui.py and its tests are untouched; S10 switches /dispatch to the new renderer.

**Files owned:**
- `libs/ptc-cli/ptc_cli/superagent/dispatch_view.py`
- `libs/ptc-cli/tests/unit_tests/test_superagent_dispatch_view.py`

**Tests:**
```bash
cd libs/ptc-cli && uv run pytest tests/unit_tests/test_superagent_dispatch_view.py -q
```

**Acceptance:**
- `check_layout` passes at **40 / 80 / 120** in **en** and **zh-CN** for an active dispatch, one awaiting_human, and none.
- Existing `render_dispatch_lines` assertions pass unchanged.
- Row structure matches `../tui/tui-dispatch-{en,zh-CN}-w{40,80,120}.txt`.

---

## S09 · TUI screen: permission prompt (additive)

**Round:** F3 · **Depends on:** S02

**Goal:** Add render_permission_prompt(request, width, locale) to tui_permissions.py for the permission prompt of tui/SPEC.md §7.7: the tool and its argument summary, and the three answers a allow once / s session allow / d deny with Escape = deny, in en and zh-CN through tui_i18n and stacking at 40 columns. PermissionRequest and the existing helpers keep their names and behaviour; tui.py is not edited; S10 switches the pending-permission rows to the new renderer.

**Files owned:**
- `libs/ptc-cli/ptc_cli/superagent/tui_permissions.py`
- `libs/ptc-cli/tests/unit_tests/test_tui_permissions.py` (new)

**Tests:**
```bash
cd libs/ptc-cli && uv run pytest tests/unit_tests/test_tui_permissions.py -q
cd libs/ptc-cli && uv run pytest tests/unit_tests/test_superagent_tui_units.py -q   # not edited, must stay green
```

**Acceptance:**
- `check_layout` passes at **40 / 80 / 120** in **en** and **zh-CN** with a short and a very long command argument.
- The three answer keys and Escape are always visible (kept rows).
- Row structure matches `../tui/tui-permission-{en,zh-CN}-w{40,80,120}.txt`.

---

## S10 · TUI keymap (D3) + palette mount + screen switch-over (sole `tui.py` owner)

**Round:** F4 · **Depends on:** S02, S04, S05, S06, S07, S08, S09

**Goal:** Switch the full-screen TUI to the terminal-native design; sole owner of tui.py and its tests. D3: Shift+Tab (s-tab) moves to the previous focus stop instead of cycling mode; mode cycle moves to Alt+M (escape, m), suppressed while a permission is pending; status hint 'shift+tab mode' becomes 'alt+m mode'; /help's 'Shift+Tab cycle mode' line becomes Alt+M plus 'Shift+Tab previous focus'. Ctrl+K and '/' at column 0 open the S04 palette (prefill '' / '/'); Tab, Enter, Esc per D2/D3. Home, /jobs, /review, /dispatch and the permission prompt render through S05–S09. Drop _FALLBACK_COLORS for tui_theme; _UNSUPPORTED and shell-global notices get en/zh-CN via tui_i18n. Refresh snapshots.

**Files owned:**
- `libs/ptc-cli/ptc_cli/superagent/tui.py`
- `libs/ptc-cli/tests/unit_tests/test_tui_keymap.py` (new)
- `libs/ptc-cli/tests/unit_tests/test_superagent_tui_app.py`
- `libs/ptc-cli/tests/unit_tests/test_superagent_tui_units.py`
- `libs/ptc-cli/tests/unit_tests/test_tui_review_blockers.py`
- `libs/ptc-cli/tests/unit_tests/test_tui_snapshots.py`
- `libs/ptc-cli/tests/unit_tests/tui_snapshots/` (every file)
- `libs/ptc-cli/tests/unit_tests/test_tui_redteam.py`
- `libs/ptc-cli/tests/unit_tests/test_tui_motion_cache.py`
- `libs/ptc-cli/tests/unit_tests/test_tui_motion_redteam.py`
- `libs/ptc-cli/tests/unit_tests/test_superagent_carriage_return.py` (imports `tui.py`)
- `libs/ptc-cli/tests/unit_tests/test_superagent_hidden_text.py` (imports `tui.py`; also exercises `/jobs`)

**Tests:**
```bash
cd libs/ptc-cli && uv run pytest tests/unit_tests/test_tui_keymap.py tests/unit_tests/test_superagent_tui_app.py tests/unit_tests/test_superagent_tui_units.py tests/unit_tests/test_tui_review_blockers.py tests/unit_tests/test_tui_snapshots.py tests/unit_tests/test_tui_redteam.py tests/unit_tests/test_tui_motion_cache.py tests/unit_tests/test_tui_motion_redteam.py -q
cd libs/ptc-cli && TUI_SNAPSHOT_UPDATE=1 uv run pytest tests/unit_tests/test_tui_snapshots.py -q   # once, then review the diff
cd libs/ptc-cli && uv run pytest tests/unit_tests -q   # whole unit suite green
```

**Acceptance:**
- The `s-tab` binding (today `tui.py:4156`, calling `_cycle_mode`) now moves focus to the previous stop of `main-list-or-transcript → composer`; `_cycle_mode` is bound only to Alt+M (`"escape", "m"`) with the `not_pending` filter.
- `grep -rn "shift+tab mode" libs/ptc-cli/ptc_cli` prints nothing; the footer hint (today `tui.py:2129`) is `alt+m mode`; the help text (today `tui.py:201`) lists `Alt+M  cycle mode: auto, dispatch, read-only`, `Shift+Tab  previous focus` and `Ctrl+K  palette`.
- `test_tui_keymap.py` drives the key bindings: Shift+Tab moves focus; Alt+M cycles and is ignored while a permission is pending; Ctrl+K opens the palette with the query focused; `/` at column 0 opens it prefilled `/`, `/` elsewhere inserts; in the palette Tab accepts, Shift+Tab is inert, Esc closes and focus returns to the composer; `approve` without an id + Enter shows the needs-id hint and sends no request.
- Composer newline per D3 unchanged: Shift+Enter when reported (kitty `CSI 13;2u`, xterm `27;2;13~`), Ctrl+J and Esc Enter (today `tui.py:4050–4051`); while a permission is pending newline, Enter and Up/Down are suppressed and a/s/d/Escape answer.
- `check_layout` at **40 / 80 / 120** × **en / zh-CN** on the status bar + composer frame; the refreshed snapshots show Home, `/jobs`, `/review`, `/dispatch`, the permission prompt and the slash menu in the new layout.
- `grep -c "#[0-9A-Fa-f]\{6\}" libs/ptc-cli/ptc_cli/superagent/tui.py` prints 0 (AUDIT §6 #3); `_UNSUPPORTED` has an en twin (AUDIT §6 #15).
- `MIN_TUI_COLUMNS` stays 80 in this slice (see Open questions).

---

## S11 · Line shell parity

**Round:** F4 · **Depends on:** S02, S04, S05, S06

**Goal:** Bring the Rich line shell (shell.py, the fallback below 80 columns, for pipes and CONDUCTOR_NO_TUI) to the same product as the TUI: /help lists the S04 command table instead of the shorter HELP_COMMANDS tuple, unknown commands get D1 suggestions from tui_palette, Home and /jobs print through tui_home / tui_jobs at the terminal width, and _UNSUPPORTED plus every shell notice reads en or zh-CN through tui_i18n. HELP_COMMANDS stays exported for its tests, derived from the table. No tui.py edits.

**Files owned:**
- `libs/ptc-cli/ptc_cli/superagent/shell.py`
- `libs/ptc-cli/tests/unit_tests/test_superagent_shell.py`

**Tests:**
```bash
cd libs/ptc-cli && uv run pytest tests/unit_tests/test_superagent_shell.py -q
```

**Acceptance:**
- `/help` output in the line shell and the TUI list the same commands (AUDIT §6 #14).
- Help, Home and `/jobs` output pass `check_layout` at **40 / 80 / 120** in **en** and **zh-CN**.
- `/dsp` in the line shell suggests `/dispatch`.

---

## S12 · Web shell chrome (sidebar, tab bar, inner router)

**Round:** F3 · **Depends on:** S01, S03

**Goal:** Restyle the authenticated chrome to the terminal-native rail of web/home.html: mono labels, square rows on token surfaces, the 2px ink.focus ring, a visible palette trigger (Ctrl+K hint) that calls S03's openPalette, distinct lucide icons per destination (today /lines, /dispatch, /agent-fleet share GitBranch and /plugins, /skills-mcp share Blocks), the mobile bottom tab bar at 390, and the page loader filling the pane. Owns Main.tsx (the inner router) without changing any route. New copy in tnShell en-US/zh-CN files; en and zh-CN nav labels must fit the rail and the tab bar.

**Files owned:**
- `web/src/components/AppShell/` (every file, incl. new `locales/tnShell.en-US.json`, `locales/tnShell.zh-CN.json`, `__tests__/locales.test.ts`)
- `web/src/components/Sidebar/` (every file)
- `web/src/components/BottomTabBar/` (every file)
- `web/src/components/NetworkBanner/` (every file)
- `web/src/components/PageLoading/` (every file)
- `web/src/components/brand/` (every file)
- `web/src/components/Main/` (every file, incl. `Main.tsx`)
- `web/src/components/nav/` (every file)
- `web/src/components/__tests__/` (every file)
- `web/src/components/HostedSignInGate.tsx`
- `web/src/components/LanguageToggle.tsx`
- `web/src/components/StaleBuildBoundary.tsx`
- `web/src/components/StaleBuildToastCopy.tsx`
- `web/src/App.css`
- `web/src/styles/base.css`
- `web/src/styles/chrome.css`
- `web/src/styles/dotGrid.css`
- `web/src/styles/animations.css`
- `web/e2e/terminal-native/shell.spec.js` (new)

**Tests:**
```bash
cd web && pnpm typecheck && pnpm test && pnpm lint
cd web && pnpm exec playwright test e2e/terminal-native/shell.spec.js
```

**Acceptance:**
- Every nav destination has a distinct icon (AUDIT §6 #13); a unit test in `components/nav/` asserts no icon repeats.
- The palette trigger opens S03's palette and Esc returns focus to the trigger.
- `shell.spec.js` runs the matrix on `/inbox` (rail at 1440, tab bar at 390); long zh-CN and en labels do not clip; Tab order reaches every nav item.
- No route added, removed or renamed in `Main.tsx` (`git diff` of its `<Route` lines is empty).

---

## S13 · Web UI primitives

**Round:** F3 · **Depends on:** S01

**Goal:** Restyle web/src/components/ui/ and ConfirmDialog to the token geometry: every floating surface (dialog, sheet, popover, select, dropdown, tooltip, toast) uses one fill — surface elevation 1 with the token shadow — radii capped at radius.overlay (no rounded-t-3xl or rounded-lg on dialog and ModalShell), the 2px ink.focus ring on every control, mono for ids and kbd, and reduced-motion entrances. No command search here (that is S03). Component APIs and test ids stay so pages compile unchanged; en and zh-CN labels must fit the buttons.

**Files owned:**
- `web/src/components/ui/` (every file, incl. `__tests__/`)
- `web/src/components/ConfirmDialog.tsx`
- `web/e2e/terminal-native/ui.spec.js` (new)

**Tests:**
```bash
cd web && pnpm typecheck && pnpm test && pnpm lint
cd web && pnpm exec playwright test e2e/terminal-native/ui.spec.js
```

**Acceptance:**
- `grep -n "rounded-t-3xl\|bg-background" web/src/components/ui/dialog.tsx web/src/components/ui/ModalShell.tsx` prints nothing (AUDIT §6 #4, #10).
- `ui.spec.js` runs the matrix on a Settings confirm dialog and on a toast; focus is trapped in the dialog and returns to the opener on Esc.
- Long zh-CN / en button labels wrap inside the dialog footer without overflow at 390.

---

## S14 · Web page: Inbox (Home)

**Round:** F4 · **Depends on:** S03, S12, S13

**Goal:** Rebuild /inbox as web/home.html: the needs-you inbox first (with the reply trap in front of the composer), then running work, each row with state glyph, word and token colour, square geometry and mono ids. The composer sends on Enter and inserts a newline on Shift+Enter or Ctrl+J; its mode label follows S03's composerMode (Alt+M). Fix .sa-error / .sa-status--bad using the danger wash as text colour. superagent.css is owned here; other superagent pages add their own CSS. New copy in tnInbox en-US/zh-CN files; en and zh-CN must both fit. Enter while an input method is composing (isComposing, keyCode 229) never runs, sends or inserts; test it.

**Files owned:**
- `web/src/features/superagent/InboxPage.tsx`
- `web/src/features/superagent/superagent.css`
- `web/src/features/superagent/shared.tsx`
- `web/src/features/superagent/__tests__/taskFirst.test.tsx`
- `web/src/features/superagent/__tests__/inboxLocales.test.ts` (new)
- `web/src/features/superagent/locales/tnInbox.en-US.json` (new)
- `web/src/features/superagent/locales/tnInbox.zh-CN.json` (new)
- `web/e2e/terminal-native/inbox.spec.js` (new)

**Tests:**
```bash
cd web && pnpm typecheck && pnpm test && pnpm lint
cd web && pnpm exec playwright test e2e/terminal-native/inbox.spec.js
```

**Acceptance:**
- No `border-radius` above `radius.card` and no `999px` pill except semantic state tags in `superagent.css` (AUDIT §6 #5); failed text uses the danger ink, not `--color-danger-soft` (AUDIT §6 #6).
- `taskFirst.test.tsx` covers Enter sends, Shift+Enter and Ctrl+J insert a newline, and the needs-you trap order (reply → composer).
- `inbox.spec.js` runs the matrix with one needs-you, two running, one failed Ticket and a zh-CN title of 60 CJK characters.

---

## S15 · Web page: Tickets list

**Round:** F5 · **Depends on:** S03, S12, S13, S14

**Goal:** Rebuild /tasks as web/tickets.html: a keyboard filter bar (state, worker, text) and one dense row per Ticket covering all seven states with glyph, word and token colour (the light-selection ink rule included), j/k row motion when the list has focus, Enter opens the Ticket. Styles go in a new tasks.css (superagent.css belongs to S14). New copy in tnTickets en-US/zh-CN files; long en and zh-CN titles truncate with a tooltip, never pushing the state column out.

**Files owned:**
- `web/src/features/superagent/TasksPage.tsx`
- `web/src/features/superagent/tasks.css` (new)
- `web/src/features/superagent/__tests__/TasksPage.test.tsx` (new)
- `web/src/features/superagent/locales/tnTickets.en-US.json` (new)
- `web/src/features/superagent/locales/tnTickets.zh-CN.json` (new)
- `web/e2e/terminal-native/tickets.spec.js` (new)

**Tests:**
```bash
cd web && pnpm typecheck && pnpm test && pnpm lint
cd web && pnpm exec playwright test e2e/terminal-native/tickets.spec.js
```

**Acceptance:**
- `TasksPage.test.tsx` renders all seven states (glyph + word), filters by state, and moves with j/k only while the list has focus; it asserts en/zh-CN parity of `tnTickets`.
- `tickets.spec.js` runs the matrix with every state present.

---

## S16 · Web page: Ticket detail

**Round:** F5 · **Depends on:** S03, S12, S13, S14

**Goal:** Rebuild /tasks/:ticketId as web/ticket.html: the conversation, worker progress with state glyphs, the worker report, and the read-only git delivery check; the clarification panel as a needs-you trap (reply → composer) with Shift+Enter / Ctrl+J newlines. Styles go in a new ticket.css. New copy in tnTicket en-US/zh-CN files; ids and branch names in mono never overflow at 390 in either language. Enter while an input method is composing (isComposing, keyCode 229) never runs, sends or inserts; test it.

**Files owned:**
- `web/src/features/superagent/TaskPage.tsx`
- `web/src/features/superagent/ClarificationPanel.tsx`
- `web/src/features/superagent/ticket.css` (new)
- `web/src/features/superagent/__tests__/ticketDetailIds.test.tsx`
- `web/src/features/superagent/__tests__/ticketProgress.test.tsx`
- `web/src/features/superagent/__tests__/clarificationGate.test.tsx`
- `web/src/features/superagent/locales/tnTicket.en-US.json` (new)
- `web/src/features/superagent/locales/tnTicket.zh-CN.json` (new)
- `web/e2e/terminal-native/ticket.spec.js` (new)

**Tests:**
```bash
cd web && pnpm typecheck && pnpm test && pnpm lint
cd web && pnpm exec playwright test e2e/terminal-native/ticket.spec.js
```

**Acceptance:**
- The three owned tests pass; `clarificationGate.test.tsx` covers the trap order and the newline keys.
- `ticket.spec.js` runs the matrix for a running and a completed Ticket.

---

## S17 · Web page: Dispatch (list + detail)

**Round:** F4 · **Depends on:** S03, S12, S13

**Goal:** Rebuild /dispatch and /dispatch/:id as web/dispatch.html: lines L1–L4 with provider and model, review verdicts with glyph and word, and the awaiting_human approve/reject decision behind the existing confirm dialog. The detail page honours ?confirm=approve|reject from S03's palette by opening that dialog for the id, never approving without it. dispatch.css stays owned here. New copy in tnDispatch en-US/zh-CN files; long en and zh-CN notes and model names fit at 390.

**Files owned:**
- `web/src/features/dispatch/` (every file: `DispatchListPage.tsx`, `DispatchDetailPage.tsx`, `model.ts`, `dispatch.css`, `__tests__/`)
- `web/src/features/dispatch/locales/tnDispatch.en-US.json` (new)
- `web/src/features/dispatch/locales/tnDispatch.zh-CN.json` (new)
- `web/e2e/terminal-native/dispatch.spec.js` (new)

**Tests:**
```bash
cd web && pnpm typecheck && pnpm test && pnpm lint
cd web && pnpm exec playwright test e2e/terminal-native/dispatch.spec.js
```

**Acceptance:**
- `DispatchPages.test.tsx` covers `?confirm=approve` opening the dialog (no API call until confirmed) and `?confirm=approve` on a non-awaiting dispatch showing nothing actionable.
- `dispatch.spec.js` runs the matrix for the list and for an awaiting_human detail.

---

## S18 · Web page: Agents

**Round:** F5 · **Depends on:** S03, S12, S13

**Goal:** Restyle /agents (AgentsPage and FleetSkillsPanel) to the terminal-native system: dense mono rows for the main agent, daemons and native runtimes, state glyph and word for online, busy and offline, square token geometry, keyboard reachable actions. Styles go in a new agents.css; new copy in tnAgents en-US/zh-CN files. Behaviour and data loading stay as they are; en and zh-CN must both fit at 1440 and 390.

**Files owned:**
- `web/src/features/superagent/AgentsPage.tsx`
- `web/src/features/superagent/FleetSkillsPanel.tsx`
- `web/src/features/superagent/agents.css` (new)
- `web/src/features/superagent/__tests__/AgentsPage.fleetSkillsChunkError.test.tsx`
- `web/src/features/superagent/__tests__/AgentsPage.mainAgent.test.tsx`
- `web/src/features/superagent/__tests__/FleetSkillsPanel.test.tsx`
- `web/src/features/superagent/__tests__/daemonRouting.test.tsx`
- `web/src/features/superagent/locales/tnAgents.en-US.json` (new)
- `web/src/features/superagent/locales/tnAgents.zh-CN.json` (new)
- `web/e2e/terminal-native/agents.spec.js` (new)

**Tests:**
```bash
cd web && pnpm typecheck && pnpm test && pnpm lint
cd web && pnpm exec playwright test e2e/terminal-native/agents.spec.js
```

**Acceptance:**
- Owned tests pass; one asserts `tnAgents` parity.
- `agents.spec.js` runs the matrix on `/agents`; no hex outside the 25 in owned files.

---

## S19 · Web page: Lines

**Round:** F5 · **Depends on:** S03, S12, S13

**Goal:** Restyle /lines to the terminal-native system: one dense row per line with its worker, branch in mono and state glyph and word, square token geometry, j/k row motion when the list has focus and keyboard reachable actions. Styles go in a new lines.css; new copy in tnLines en-US/zh-CN files. Data and behaviour are unchanged; long en and zh-CN line names truncate without hiding the state.

**Files owned:**
- `web/src/features/superagent/LinesPage.tsx`
- `web/src/features/superagent/lines.css` (new)
- `web/src/features/superagent/__tests__/LinesPage.test.tsx`
- `web/src/features/superagent/locales/tnLines.en-US.json` (new)
- `web/src/features/superagent/locales/tnLines.zh-CN.json` (new)
- `web/e2e/terminal-native/lines.spec.js` (new)

**Tests:**
```bash
cd web && pnpm typecheck && pnpm test && pnpm lint
cd web && pnpm exec playwright test e2e/terminal-native/lines.spec.js
```

**Acceptance:**
- `LinesPage.test.tsx` passes and asserts `tnLines` parity.
- `lines.spec.js` runs the matrix on `/lines`.

---

## S20 · Web page: Runs

**Round:** F5 · **Depends on:** S03, S12, S13

**Goal:** Restyle /runs to the terminal-native system: dense mono run rows with state glyph and word, right-aligned tabular durations, square token geometry and keyboard reachable rows. Styles go in a new runs.css; new copy in tnRuns en-US/zh-CN files. Data and behaviour are unchanged; en and zh-CN must fit at 1440 and 390.

**Files owned:**
- `web/src/features/superagent/RunsPage.tsx`
- `web/src/features/superagent/runs.css` (new)
- `web/src/features/superagent/__tests__/RunsPage.test.tsx` (new)
- `web/src/features/superagent/locales/tnRuns.en-US.json` (new)
- `web/src/features/superagent/locales/tnRuns.zh-CN.json` (new)
- `web/e2e/terminal-native/runs.spec.js` (new)

**Tests:**
```bash
cd web && pnpm typecheck && pnpm test && pnpm lint
cd web && pnpm exec playwright test e2e/terminal-native/runs.spec.js
```

**Acceptance:**
- `RunsPage.test.tsx` renders every state and asserts `tnRuns` parity.
- `runs.spec.js` runs the matrix on `/runs`.

---

## S21 · Web page: Sources

**Round:** F5 · **Depends on:** S03, S12, S13

**Goal:** Restyle /sources to the terminal-native system: dense rows per source with job counts right-aligned and tabular, health as glyph and word, square token geometry and keyboard reachable actions. Styles go in a new sources.css; new copy in tnSources en-US/zh-CN files. Data and behaviour are unchanged; en and zh-CN must fit at 1440 and 390.

**Files owned:**
- `web/src/features/superagent/SourcesPage.tsx`
- `web/src/features/superagent/sources.css` (new)
- `web/src/features/superagent/__tests__/SourcesPage.test.tsx` (new)
- `web/src/features/superagent/locales/tnSources.en-US.json` (new)
- `web/src/features/superagent/locales/tnSources.zh-CN.json` (new)
- `web/e2e/terminal-native/sources.spec.js` (new)

**Tests:**
```bash
cd web && pnpm typecheck && pnpm test && pnpm lint
cd web && pnpm exec playwright test e2e/terminal-native/sources.spec.js
```

**Acceptance:**
- `SourcesPage.test.tsx` passes and asserts `tnSources` parity.
- `sources.spec.js` runs the matrix on `/sources`.

---

## S22 · Web page: Agent fleet

**Round:** F5 · **Depends on:** S03, S12, S13

**Goal:** Restyle /agent-fleet to the terminal-native system: the provider and model roster (codex, kimi, cursor, grok, minimax, deepseek-dsh, zcode, stepfun as data) in dense mono rows with health glyph and word, square token geometry and keyboard reachable actions. Everything stays inside features/agentfleet/; new copy in tnAgentFleet en-US/zh-CN files. Data and behaviour are unchanged; en and zh-CN must fit at 1440 and 390.

**Files owned:**
- `web/src/features/agentfleet/` (every file, incl. new `locales/tnAgentFleet.en-US.json`, `locales/tnAgentFleet.zh-CN.json`)
- `web/e2e/terminal-native/agent-fleet.spec.js` (new)

**Tests:**
```bash
cd web && pnpm typecheck && pnpm test && pnpm lint
cd web && pnpm exec playwright test e2e/terminal-native/agent-fleet.spec.js
```

**Acceptance:**
- Owned tests pass and assert `tnAgentFleet` parity.
- `agent-fleet.spec.js` runs the matrix on `/agent-fleet`.

---

## S23 · Web page: Skills / MCP

**Round:** F5 · **Depends on:** S03, S12, S13

**Goal:** Restyle /skills-mcp to the terminal-native system: dense rows for local skills and MCP servers with status glyph and word, mono names, square token geometry and keyboard reachable actions. Everything stays inside features/skills-mcp/; new copy in tnSkillsMcp en-US/zh-CN files. Data and behaviour are unchanged; en and zh-CN must fit at 1440 and 390.

**Files owned:**
- `web/src/features/skills-mcp/` (every file, incl. new `locales/tnSkillsMcp.en-US.json`, `locales/tnSkillsMcp.zh-CN.json`)
- `web/e2e/terminal-native/skills-mcp.spec.js` (new)

**Tests:**
```bash
cd web && pnpm typecheck && pnpm test && pnpm lint
cd web && pnpm exec playwright test e2e/terminal-native/skills-mcp.spec.js
```

**Acceptance:**
- Owned tests pass and assert `tnSkillsMcp` parity.
- `skills-mcp.spec.js` runs the matrix on `/skills-mcp`.

---

## S24 · Web page: Plugins (+ MCP components)

**Round:** F5 · **Depends on:** S03, S12, S13

**Goal:** Restyle /plugins (catalog, detail, Discover, connectors, secrets, subagents, install wizard) and the shared MCP components it embeds to the terminal-native system: dense mono rows, state glyph and word, square token geometry, one floating fill, no hex outside the 25 (surface.css today), keyboard reachable actions. The largest slice (221 + 27 files): dispatch it with sub-agents split by sub-directory inside this one line. New copy in tnPlugins en-US/zh-CN files; en and zh-CN must fit at 1440 and 390.

**Files owned:**
- `web/src/pages/Plugins/` (every file, incl. new `locales/tnPlugins.en-US.json`, `locales/tnPlugins.zh-CN.json`)
- `web/src/components/mcp/` (every file)
- `web/e2e/terminal-native/plugins.spec.js` (new)

**Tests:**
```bash
cd web && pnpm typecheck && pnpm test && pnpm lint
cd web && pnpm exec playwright test e2e/terminal-native/plugins.spec.js
```

**Acceptance:**
- Owned tests pass and assert `tnPlugins` parity.
- `plugins.spec.js` runs the matrix on `/plugins`, a plugin detail, and the Discover tab.
- `grep -rE "#[0-9A-Fa-f]{6}" web/src/pages/Plugins web/src/components/mcp` finds only token hex values.

---

## S25 · Web page: Settings

**Round:** F5 · **Depends on:** S03, S12, S13

**Goal:** Restyle /settings to the terminal-native system: section labels in mono, dense field rows, token type sizes instead of the ad-hoc text-[0.625rem] / text-[0.6875rem] in ConnectedAccounts.tsx and its neighbours, square token geometry and keyboard reachable controls. Everything stays inside pages/Settings/; new copy in tnSettings en-US/zh-CN files. Behaviour is unchanged; en and zh-CN must fit at 1440 and 390.

**Files owned:**
- `web/src/pages/Settings/` (every file, incl. new `locales/tnSettings.en-US.json`, `locales/tnSettings.zh-CN.json`)
- `web/e2e/terminal-native/settings.spec.js` (new)

**Tests:**
```bash
cd web && pnpm typecheck && pnpm test && pnpm lint
cd web && pnpm exec playwright test e2e/terminal-native/settings.spec.js
```

**Acceptance:**
- `grep -c "text-\[0\.6" web/src/pages/Settings/panels/ConnectedAccounts.tsx` prints 0 (AUDIT §6 #9).
- Owned tests pass and assert `tnSettings` parity; `settings.spec.js` runs the matrix on `/settings`.
- Remove the temporary Settings 390px override in `web/src/styles/tokens.css` (the "Narrow settings" `@media (max-width: 900px)` block, lines 311-328 at F1, added by S01 because `Settings.css` collapses `.settings-console` to `1fr`): fix the track in `Settings.css` (`minmax(0, 1fr)`, `min-width: 0` on the nav), delete the block, and invert the S01 test `still carries the temporary Settings 390px override (S25 removes it)` in `web/src/styles/__tests__/terminalNativeTokens.test.ts` to assert the block is gone. This one block and that one test are the planner-approved exception to S01's ownership; `settings.spec.js` must still pass at 390 in en-US and zh-CN without them.

---

## S26 · Web page: Setup wizard (+ model pickers)

**Round:** F5 · **Depends on:** S03, S12, S13

**Goal:** Restyle the /setup wizard (provider, connect, models, defaults, ready) and the model pickers it shares with Settings to the terminal-native system: step list in mono, dense provider rows, token type and geometry, no hex outside the 25 (ProviderStep and connectStep today), every step completable by keyboard. New copy in tnSetup en-US/zh-CN files; behaviour is unchanged; en and zh-CN must fit at 1440 and 390.

**Files owned:**
- `web/src/pages/Setup/` (every file, incl. new `locales/tnSetup.en-US.json`, `locales/tnSetup.zh-CN.json`)
- `web/src/components/model/` (every file)
- `web/e2e/terminal-native/setup.spec.js` (new)

**Tests:**
```bash
cd web && pnpm typecheck && pnpm test && pnpm lint
cd web && pnpm exec playwright test e2e/terminal-native/setup.spec.js
```

**Acceptance:**
- Owned tests pass and assert `tnSetup` parity.
- `setup.spec.js` runs the matrix on `/setup/provider` and `/setup/connect`; every step is reachable with Tab and Enter.

---

## S27 · Web page: Login

**Round:** F5 · **Depends on:** S01, S13

**Goal:** Restyle the sign-in entry to the terminal-native system on its own full-viewport surface (it mounts outside AuthenticatedShell, so its 100vh is correct): mono heading, token inputs and buttons, the 2px focus ring, no hex outside the 25 (AgentNetwork.css today) and reduced-motion for the background animation. Everything stays inside pages/Login/; new copy in tnLogin en-US/zh-CN files; en and zh-CN must fit at 1440 and 390.

**Files owned:**
- `web/src/pages/Login/` (every file, incl. new `locales/tnLogin.en-US.json`, `locales/tnLogin.zh-CN.json`)
- `web/e2e/terminal-native/login.spec.js` (new)

**Tests:**
```bash
cd web && pnpm typecheck && pnpm test && pnpm lint
cd web && pnpm exec playwright test e2e/terminal-native/login.spec.js
```

**Acceptance:**
- Owned tests pass and assert `tnLogin` parity.
- `login.spec.js` runs the matrix on the entry page; with reduced motion the background is static.

---

## S28 · Web page: Legal

**Round:** F5 · **Depends on:** S01

**Goal:** Restyle /legal to the terminal-native system: the proportional prose face from the token type set for body text, mono headings, token surfaces and links with the 2px focus ring, readable line length at 1440 and full width at 390. Everything stays inside pages/Legal/; new copy in tnLegal en-US/zh-CN files if any string changes; en and zh-CN both render.

**Files owned:**
- `web/src/pages/Legal/` (every file, incl. new `locales/tnLegal.en-US.json`, `locales/tnLegal.zh-CN.json`)
- `web/e2e/terminal-native/legal.spec.js` (new)

**Tests:**
```bash
cd web && pnpm typecheck && pnpm test && pnpm lint
cd web && pnpm exec playwright test e2e/terminal-native/legal.spec.js
```

**Acceptance:**
- `legal.spec.js` runs the matrix on `/legal`; links are reachable with Tab and show the focus ring.

---

## S29 · Web page: OAuth callbacks

**Round:** F5 · **Depends on:** S01

**Goal:** Restyle the DeepSeek and Zhipu OAuth callback pages to the terminal-native system: a mono status line with glyph and word for working, done and failed, token surfaces, a keyboard reachable retry or return action, reduced-motion for the spinner. Everything stays inside pages/OAuth/; new copy in tnOAuth en-US/zh-CN files; en and zh-CN must fit at 1440 and 390.

**Files owned:**
- `web/src/pages/OAuth/` (every file, incl. new `locales/tnOAuth.en-US.json`, `locales/tnOAuth.zh-CN.json`)
- `web/e2e/terminal-native/oauth.spec.js` (new)

**Tests:**
```bash
cd web && pnpm typecheck && pnpm test && pnpm lint
cd web && pnpm exec playwright test e2e/terminal-native/oauth.spec.js
```

**Acceptance:**
- `oauth.spec.js` runs the matrix on `/oauth/deepseek` and `/oauth/zhipu` (mocked) in the working and failed states.

---

## S30 · Main locale catalogs sweep

**Round:** F6 · **Depends on:** S03, S12–S29

**Goal:** Be the only writer of web/src/locales/en-US.json and zh-CN.json in this plan. Delete every key no longer referenced after S03–S29 moved their copy into tn* feature files (including the unused commandPalette.* block), keep en-US/zh-CN key parity, and extend locales/__tests__/keys.test.ts so its sweep also loads every */locales/tn*.{en-US,zh-CN}.json, checks their parity, and fails when a tn* namespace collides with a main-catalog one. No UI change; en and zh-CN render exactly as before.

**Files owned:**
- `web/src/locales/en-US.json`
- `web/src/locales/zh-CN.json`
- `web/src/locales/__tests__/keys.test.ts`

**Tests:**
```bash
cd web && pnpm typecheck && pnpm test && pnpm lint
cd web && pnpm exec playwright test e2e/terminal-native/   # every slice spec, unchanged screenshots
```

**Acceptance:**
- `keys.test.ts` passes with the feature-file sweep; `grep -c '"commandPalette"' web/src/locales/en-US.json` prints 0.
- Every `e2e/terminal-native/*.spec.js` still passes in en-US and zh-CN.

---

## Ownership matrix

One row per owned path (a trailing `/` means every file under it). No path is
listed twice, and no listed file sits inside another slice's directory.

| Path | Slice |
|---|---|
| `web/src/styles/terminal-native.css` | S01 |
| `web/src/styles/tokens.css` | S01 |
| `web/src/index.css` | S01 |
| `web/src/styles/conductor.css` | S01 |
| `web/src/main.tsx` | S01 |
| `web/src/styles/__tests__/tokenRefs.test.ts` | S01 |
| `web/src/styles/__tests__/terminalNativeTokens.test.ts` | S01 |
| `web/e2e/terminal-native/matrix.js` | S01 |
| `web/e2e/terminal-native/tokens.spec.js` | S01 |
| `libs/ptc-cli/ptc_cli/superagent/tui_theme.py` | S02 |
| `libs/ptc-cli/tests/unit_tests/test_tui_theme.py` | S02 |
| `libs/ptc-cli/ptc_cli/superagent/tui_i18n.py` | S02 |
| `libs/ptc-cli/tests/unit_tests/test_tui_i18n.py` | S02 |
| `libs/ptc-cli/tests/unit_tests/tui_layout_contract.py` | S02 |
| `web/src/App.tsx` | S03 |
| `web/src/features/palette/` | S03 |
| `web/src/lib/featureLocales.ts` | S03 |
| `web/src/lib/__tests__/featureLocales.test.ts` | S03 |
| `web/e2e/terminal-native/palette.spec.js` | S03 |
| `libs/ptc-cli/ptc_cli/superagent/tui_palette.py` | S04 |
| `libs/ptc-cli/ptc_cli/superagent/tui_completion.py` | S04 |
| `libs/ptc-cli/tests/unit_tests/test_tui_palette.py` | S04 |
| `libs/ptc-cli/ptc_cli/superagent/tui_home.py` | S05 |
| `libs/ptc-cli/tests/unit_tests/test_tui_home.py` | S05 |
| `libs/ptc-cli/ptc_cli/superagent/tui_jobs.py` | S06 |
| `libs/ptc-cli/tests/unit_tests/test_tui_jobs.py` | S06 |
| `libs/ptc-cli/ptc_cli/superagent/tui_review.py` | S07 |
| `libs/ptc-cli/tests/unit_tests/test_tui_review.py` | S07 |
| `libs/ptc-cli/ptc_cli/superagent/dispatch_view.py` | S08 |
| `libs/ptc-cli/tests/unit_tests/test_superagent_dispatch_view.py` | S08 |
| `libs/ptc-cli/ptc_cli/superagent/tui_permissions.py` | S09 |
| `libs/ptc-cli/tests/unit_tests/test_tui_permissions.py` | S09 |
| `libs/ptc-cli/ptc_cli/superagent/tui.py` | S10 |
| `libs/ptc-cli/tests/unit_tests/test_tui_keymap.py` | S10 |
| `libs/ptc-cli/tests/unit_tests/test_superagent_tui_app.py` | S10 |
| `libs/ptc-cli/tests/unit_tests/test_superagent_tui_units.py` | S10 |
| `libs/ptc-cli/tests/unit_tests/test_tui_review_blockers.py` | S10 |
| `libs/ptc-cli/tests/unit_tests/test_tui_snapshots.py` | S10 |
| `libs/ptc-cli/tests/unit_tests/tui_snapshots/` | S10 |
| `libs/ptc-cli/tests/unit_tests/test_tui_redteam.py` | S10 |
| `libs/ptc-cli/tests/unit_tests/test_tui_motion_cache.py` | S10 |
| `libs/ptc-cli/tests/unit_tests/test_tui_motion_redteam.py` | S10 |
| `libs/ptc-cli/ptc_cli/superagent/shell.py` | S11 |
| `libs/ptc-cli/tests/unit_tests/test_superagent_shell.py` | S11 |
| `web/src/components/AppShell/` | S12 |
| `web/src/components/Sidebar/` | S12 |
| `web/src/components/BottomTabBar/` | S12 |
| `web/src/components/NetworkBanner/` | S12 |
| `web/src/components/PageLoading/` | S12 |
| `web/src/components/brand/` | S12 |
| `web/src/components/Main/` | S12 |
| `web/src/components/nav/` | S12 |
| `web/src/components/__tests__/` | S12 |
| `web/src/components/HostedSignInGate.tsx` | S12 |
| `web/src/components/LanguageToggle.tsx` | S12 |
| `web/src/components/StaleBuildBoundary.tsx` | S12 |
| `web/src/components/StaleBuildToastCopy.tsx` | S12 |
| `web/src/App.css` | S12 |
| `web/src/styles/base.css` | S12 |
| `web/src/styles/chrome.css` | S12 |
| `web/src/styles/dotGrid.css` | S12 |
| `web/src/styles/animations.css` | S12 |
| `web/e2e/terminal-native/shell.spec.js` | S12 |
| `web/src/components/ui/` | S13 |
| `web/src/components/ConfirmDialog.tsx` | S13 |
| `web/e2e/terminal-native/ui.spec.js` | S13 |
| `web/src/features/superagent/InboxPage.tsx` | S14 |
| `web/src/features/superagent/superagent.css` | S14 |
| `web/src/features/superagent/shared.tsx` | S14 |
| `web/src/features/superagent/__tests__/taskFirst.test.tsx` | S14 |
| `web/src/features/superagent/__tests__/inboxLocales.test.ts` | S14 |
| `web/src/features/superagent/locales/tnInbox.en-US.json` | S14 |
| `web/src/features/superagent/locales/tnInbox.zh-CN.json` | S14 |
| `web/e2e/terminal-native/inbox.spec.js` | S14 |
| `web/src/features/superagent/TasksPage.tsx` | S15 |
| `web/src/features/superagent/tasks.css` | S15 |
| `web/src/features/superagent/__tests__/TasksPage.test.tsx` | S15 |
| `web/src/features/superagent/locales/tnTickets.en-US.json` | S15 |
| `web/src/features/superagent/locales/tnTickets.zh-CN.json` | S15 |
| `web/e2e/terminal-native/tickets.spec.js` | S15 |
| `web/src/features/superagent/TaskPage.tsx` | S16 |
| `web/src/features/superagent/ClarificationPanel.tsx` | S16 |
| `web/src/features/superagent/ticket.css` | S16 |
| `web/src/features/superagent/__tests__/ticketDetailIds.test.tsx` | S16 |
| `web/src/features/superagent/__tests__/ticketProgress.test.tsx` | S16 |
| `web/src/features/superagent/__tests__/clarificationGate.test.tsx` | S16 |
| `web/src/features/superagent/locales/tnTicket.en-US.json` | S16 |
| `web/src/features/superagent/locales/tnTicket.zh-CN.json` | S16 |
| `web/e2e/terminal-native/ticket.spec.js` | S16 |
| `web/src/features/dispatch/` | S17 |
| `web/e2e/terminal-native/dispatch.spec.js` | S17 |
| `web/src/features/superagent/AgentsPage.tsx` | S18 |
| `web/src/features/superagent/FleetSkillsPanel.tsx` | S18 |
| `web/src/features/superagent/agents.css` | S18 |
| `web/src/features/superagent/__tests__/AgentsPage.fleetSkillsChunkError.test.tsx` | S18 |
| `web/src/features/superagent/__tests__/AgentsPage.mainAgent.test.tsx` | S18 |
| `web/src/features/superagent/__tests__/FleetSkillsPanel.test.tsx` | S18 |
| `web/src/features/superagent/__tests__/daemonRouting.test.tsx` | S18 |
| `web/src/features/superagent/locales/tnAgents.en-US.json` | S18 |
| `web/src/features/superagent/locales/tnAgents.zh-CN.json` | S18 |
| `web/e2e/terminal-native/agents.spec.js` | S18 |
| `web/src/features/superagent/LinesPage.tsx` | S19 |
| `web/src/features/superagent/lines.css` | S19 |
| `web/src/features/superagent/__tests__/LinesPage.test.tsx` | S19 |
| `web/src/features/superagent/locales/tnLines.en-US.json` | S19 |
| `web/src/features/superagent/locales/tnLines.zh-CN.json` | S19 |
| `web/e2e/terminal-native/lines.spec.js` | S19 |
| `web/src/features/superagent/RunsPage.tsx` | S20 |
| `web/src/features/superagent/runs.css` | S20 |
| `web/src/features/superagent/__tests__/RunsPage.test.tsx` | S20 |
| `web/src/features/superagent/locales/tnRuns.en-US.json` | S20 |
| `web/src/features/superagent/locales/tnRuns.zh-CN.json` | S20 |
| `web/e2e/terminal-native/runs.spec.js` | S20 |
| `web/src/features/superagent/SourcesPage.tsx` | S21 |
| `web/src/features/superagent/sources.css` | S21 |
| `web/src/features/superagent/__tests__/SourcesPage.test.tsx` | S21 |
| `web/src/features/superagent/locales/tnSources.en-US.json` | S21 |
| `web/src/features/superagent/locales/tnSources.zh-CN.json` | S21 |
| `web/e2e/terminal-native/sources.spec.js` | S21 |
| `web/src/features/agentfleet/` | S22 |
| `web/e2e/terminal-native/agent-fleet.spec.js` | S22 |
| `web/src/features/skills-mcp/` | S23 |
| `web/e2e/terminal-native/skills-mcp.spec.js` | S23 |
| `web/src/pages/Plugins/` | S24 |
| `web/src/components/mcp/` | S24 |
| `web/e2e/terminal-native/plugins.spec.js` | S24 |
| `web/src/pages/Settings/` | S25 |
| `web/e2e/terminal-native/settings.spec.js` | S25 |
| `web/src/pages/Setup/` | S26 |
| `web/src/components/model/` | S26 |
| `web/e2e/terminal-native/setup.spec.js` | S26 |
| `web/src/pages/Login/` | S27 |
| `web/e2e/terminal-native/login.spec.js` | S27 |
| `web/src/pages/Legal/` | S28 |
| `web/e2e/terminal-native/legal.spec.js` | S28 |
| `web/src/pages/OAuth/` | S29 |
| `web/e2e/terminal-native/oauth.spec.js` | S29 |
| `web/src/locales/en-US.json` | S30 |
| `web/src/locales/zh-CN.json` | S30 |
| `web/src/locales/__tests__/keys.test.ts` | S30 |

Not owned by any slice (read-only for the whole plan): `web/src/i18n.ts`,
`web/src/test/setup.ts`, `web/scripts/`, `web/e2e/fixtures.js`,
`web/e2e/helpers/`, `web/src/styles/tailwind-v3-theme.css`, `web/src/api/`,
`web/src/hooks/`, `web/src/lib/` (except S03's two new files),
`libs/ptc-cli/ptc_cli/superagent/{runner,client,text,tui_events,tui_blocks,tui_render,tui_rich,tui_local,tui_sessions}.py`,
and every other test module under `libs/ptc-cli/tests/unit_tests/`.

## Checks

Run from the worktree root (POSIX `sh` + BSD/GNU `awk`, no Python). Every
list after a heading line must be empty.

```bash
P=docs/design/terminal-native/plan/PLAN.md
# 1. Goal length: one line per slice, then the maximum. BSD awk counts bytes,
#    an upper bound for characters (the ○ ● … glyphs are 3 bytes each).
awk '/^## S[0-9][0-9] /{id=$2} /^\*\*Goal:\*\* /{n=length(substr($0,11)); print id, n; if(n>m)m=n; if(n>800)bad++} END{print "max", m, "over-800", bad+0}' "$P"

# 2. Ownership.
M=$(mktemp); B=$(mktemp)
awk -F'|' '/^## Ownership matrix/{f=1;next} /^## /{f=0} f && /^\| `/{p=$2; gsub(/[ `]/,"",p); s=$3; gsub(/ /,"",s); print p, s}' "$P" | sort > "$M"
awk '/^## S[0-9][0-9] /{id=$2} /^\*\*Files owned:\*\*/{f=1;next} /^\*\*Tests:\*\*/{f=0} f && /^- `/{match($0,/`[^`]+`/); print substr($0,RSTART+1,RLENGTH-2), id}' "$P" | sort > "$B"
echo "duplicate matrix paths:"; cut -d' ' -f1 "$M" | uniq -d
echo "paths inside another slice's directory:"
while read p s; do awk -v p="$p" -v s="$s" '$1!=p && $1 ~ /\/$/ && index(p,$1)==1 && $2!=s {print "  ", p, s, "inside", $1, $2}' "$M"; done < "$M"
echo "Files-owned bullets not covered by a matrix row of the same slice:"
while read p s; do awk -v p="$p" -v s="$s" 'BEGIN{ok=0} $2==s && ($1==p || ($1 ~ /\/$/ && index(p,$1)==1)) {ok=1} END{if(!ok) print "  ", p, s}' "$M"; done < "$B"

# 3. Existence: an existing path must exist, a (new) path must not exist yet.
awk '/^## S[0-9][0-9] /{id=$2} /^\*\*Files owned:\*\*/{f=1;next} /^\*\*Tests:\*\*/{f=0} f && /^- `/{match($0,/`[^`]+`/); n=($0 ~ /\(new/)?"new":"old"; print substr($0,RSTART+1,RLENGTH-2), id, n}' "$P" |
  while read p s n; do q=${p%/}; if [ "$n" = new ]; then [ -e "$q" ] && echo "new but exists: $p $s"; else [ -e "$q" ] || echo "missing: $p $s"; fi; done
rm -f "$M" "$B"
```

Last run (2026-11-10, worktree `fx-d1` at `63731a5f0`): 30 slices, Goal
maximum **753** (S04), none over 800; 140 matrix rows / 155 Files-owned
bullets; no duplicate, no nesting across slices, every bullet covered; no
missing existing path and no `(new)` path that already exists.

## Open questions

- **40-column TUI.** The renderers of S04–S09 hold at 40/80/120, but
  `MIN_TUI_COLUMNS = 80` (`tui.py:106`) still sends narrower terminals to the
  line shell, which S11 makes use the same renderers. Lowering the floor to
  40 needs the status bar and composer to hold there too; S10 tests them at
  40 but keeps the floor. Owner decision before a later `tui.py` line.
- **Not sliced:** `tui_blocks.py` (running Ticket stream, `../tui/SPEC.md`
  §7.5), `tui_render.py`, `tui_rich.py` get the new colours through S02 only.
  A layout restyle of the running stream needs a follow-up line that comes
  after S10, because the snapshots it would change belong to S10.
- **Web composer mode.** Alt+M cycles S03's `composerMode`; it has a visible
  effect only once S14's Inbox composer reads it.
- **Screenshots are review artefacts**, not pixel baselines: they land in the
  gitignored `web/test-results/`; adding `toHaveScreenshot` baselines would
  need an owner for the snapshot directory and is not in this plan.
- **S24 size.** Plugins is one slice because its tests cross sub-directories;
  the dispatcher may fan it out to sub-agents inside that one line.

## Out of scope

Backend or autonomy behaviour, the Electron shell beyond `chrome.css`,
reintroducing retired routes (`/chat`, `/memory`, …), new dependencies, and
any change to the 25 token colours.
