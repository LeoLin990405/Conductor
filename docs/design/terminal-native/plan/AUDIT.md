# Terminal-native surface audit

Snapshot of the web app under `web/src` and the Super-agent TUI under
`libs/ptc-cli/ptc_cli/superagent/`, taken for the terminal-native redesign.
Counts are `*.tsx` + `*.ts` + `*.css` unless noted. Source of truth for the
shipped web language is repo-root `DESIGN.md` ("Operations Terminal"); TUI
colour tokens live in `tui_theme.py`. The redesign's target is
[`../system/tokens.json`](../system/tokens.json) and
[`../system/SPEC.md`](../system/SPEC.md); this file records only what ships today.

## 1. Web routes and pages

### Top-level routes (`web/src/App.tsx`)

| Path | Surface | Implementation |
|---|---|---|
| `APP_ENTRY_PATH` (`/` OSS, `/app` platform) | Sign-in / entry | `pages/Login/LoginPage.tsx` |
| `/app` | Legacy entry redirect (platform only) | `LegacyAppPathRedirect` |
| `/callback` | Auth callback | `CallbackRoute` |
| `/oauth/callback`, `/oauth/deepseek` | DeepSeek OAuth | `pages/OAuth/DeepSeekCallback.tsx` |
| `/oauth/zhipu` | Zhipu OAuth | `pages/OAuth/ZhipuCallback.tsx` |
| `/auth/confirm`, `/auth/confirm/:shell` | Email-link confirm | `AuthConfirm` |
| `/reset-password`, `/reset-password/:shell` | Password reset | `ResetPassword` |
| `/privacy` | External policy redirect | `ExternalRedirect` (when `PRIVACY_URL` set) |
| `/legal` | Legal copy | `pages/Legal/Legal.tsx` |
| `/setup/*` | Onboarding wizard | `pages/Setup/SetupWizard.tsx` |
| `/*` (authenticated) | App shell + inner routes | `AuthenticatedShell` → `components/Main/Main.tsx` |

### Setup wizard sub-routes (`pages/Setup/SetupWizard.tsx`)

`provider` · `connect` · `models` · `defaults` · `ready` (plus redirects from
`method` and `*`).

### Authenticated app routes (`components/Main/Main.tsx`)

| Path | Page module | Feature / page group |
|---|---|---|
| `/` → `/inbox` | redirect | — |
| `/inbox` | `features/superagent/InboxPage.tsx` | superagent |
| `/tasks` | `features/superagent/TasksPage.tsx` | superagent |
| `/tasks/:ticketId` | `features/superagent/TaskPage.tsx` | superagent |
| `/agents` | `features/superagent/AgentsPage.tsx` | superagent |
| `/lines` | `features/superagent/LinesPage.tsx` | superagent |
| `/dispatch` | `features/dispatch/DispatchListPage.tsx` | dispatch |
| `/dispatch/:dispatchId` | `features/dispatch/DispatchDetailPage.tsx` | dispatch |
| `/agent-fleet` | `features/agentfleet/AgentFleetPage.tsx` | agentfleet |
| `/runs` | `features/superagent/RunsPage.tsx` | superagent |
| `/sources` | `features/superagent/SourcesPage.tsx` | superagent |
| `/plugins` | `pages/Plugins/Plugins.tsx` | Plugins |
| `/skills-mcp` | `features/skills-mcp/SkillsMcpPage.tsx` | skills-mcp |
| `/connectors` | redirect → `/plugins` | legacy alias |
| `/settings` | `pages/Settings/Settings.tsx` | Settings |
| `/superagent`, `/chat/*`, `/projects/*`, `/memory`, `/automations`, `*` | redirect → `/inbox` | retired |

Nav keys that surface in the shell chrome:
`web/src/components/nav/navItems.ts` (`/inbox` … `/skills-mcp` + Settings).

### Page / feature groups with file counts

| Group | Path | Files |
|---|---|---|
| Plugins | `web/src/pages/Plugins/` | **221** |
| Setup | `web/src/pages/Setup/` | **33** |
| Login | `web/src/pages/Login/` | **27** |
| Settings | `web/src/pages/Settings/` | **22** |
| Super-agent product pages | `web/src/features/superagent/` | **20** |
| Dispatch | `web/src/features/dispatch/` | **6** |
| Agent fleet | `web/src/features/agentfleet/` | **5** |
| Skills / MCP page | `web/src/features/skills-mcp/` | **4** |
| OAuth callbacks | `web/src/pages/OAuth/` | **3** |
| Legal | `web/src/pages/Legal/` | **1** |
| **Pages + features total** | | **342** |

`web/src` overall: **651** TS/TSX/CSS files (**302** `.tsx`, **30** `.css`).

## 2. Component groups under `web/src/components`

| Group | Path | Files | Role |
|---|---|---|---|
| UI primitives | `components/ui/` | **30** | Radix + CVA buttons, dialog, toast, input, wizard, … |
| MCP | `components/mcp/` | **27** | Server modal, import, vault picker, primitives |
| Model | `components/model/` | **17** | Model selector / pickers |
| Nav | `components/nav/` | **4** | `navItems`, active-match helpers |
| App shell pieces | `AppShell`, `Sidebar`, `Main`, `BottomTabBar`, `NetworkBanner`, `PageLoading`, `brand` | **2** each (brand **1**) | Chrome |
| Root helpers | `ConfirmDialog`, `HostedSignInGate`, `LanguageToggle`, `StaleBuildBoundary`, `StaleBuildToastCopy` | **5** | Cross-cutting |
| Tests | `components/__tests__/` | **2** | — |
| **Components total** | | **98** | |

Other `web/src` buckets (not page UI, but part of the surface budget):
`hooks/` 68 · `lib/` 76 · `api/` 33 · `styles/` 8 · `contexts/` 3 · `config/` 3 ·
`locales/` 2 catalogs (`en-US.json`, `zh-CN.json`, 4980 lines each) · `test/` 6.

## 3. Super-agent TUI (`libs/ptc-cli/ptc_cli/superagent/`)

16 modules, **10 675** lines total. Primary interactive surface is the
full-screen TUI (`tui.py`); the Rich line shell (`shell.py`) is the fallback
when the terminal is not a TTY or is narrower than `MIN_TUI_COLUMNS`.

### Modes / screens

| Mode / view | Where | Notes |
|---|---|---|
| Full-screen TUI | `tui.py` `choose_mode` → `"tui"` | Requires TTY and ≥ 80 columns (`MIN_TUI_COLUMNS = 80`, `tui.py:106`) |
| Line shell | `shell.py` | Fallback when `< 80` cols, pipe, or `CONDUCTOR_NO_TUI` |
| Print (one-shot) | `choose_mode` → `"print"` | Non-interactive prompt |
| Interaction modes (composer) | `_MODES = ("auto", "dispatch", "read-only")` `tui.py:222` | Shift+Tab cycles today (binding `tui.py:4156`, footer hint `tui.py:2129`, help `tui.py:201`); decision D3 moves it to Alt+M (PLAN S10) |
| Supervisor view (default) | conversation blocks; `/details` toggles Worker detail | Product default documented in `tui.py` |
| Permission overlay | `tui_permissions.py` + permission rows in `tui.py` | Keys `a` / `s` / `d` |
| Dispatch roster view | `dispatch_view.py` | `/dispatch` |
| Sessions | `tui_sessions.py` | `/resume`, `/new` |
| Welcome / help / model panels | `tui_rich.py` | Rich panels; `DEFAULT_WIDTH = 100` |

### Slash commands

**TUI** (`tui_completion.py` `SLASH_COMMANDS`, shared with `/help` completions):

`/worker` · `/dispatch` · `/jobs` · `/attach` · `/workspace` · `/reply` ·
`/retry` · `/cancel` · `/resume` · `/status` · `/help` · `/quit` · `/skills` ·
`/mcp` · `/memory` · `/agents` · `/review` · `/merge` · `/stop` · `/signals` ·
`/clear` · `/details` · `/model` · `/compact` · `/expand` · `/collapse` ·
`/new` · `/exit`

Also: `!<shell>` local shell via `tui_local.py`.

**Line shell** help list is shorter (`shell.py` `HELP_COMMANDS`): `/help`
`/status` `/jobs` `/worker` `/dispatch` `/reply` `/retry` `/cancel` `/resume`
`/workspace` `/skills` `/mcp` `/memory` `/quit` — while the handler still
accepts extras (`/agents`, `/review`, `/merge`, `/signals`, `/new`, …).

### Supporting modules

| File | Purpose |
|---|---|
| `tui_theme.py` | Palette, glyphs, contrast helpers |
| `tui_blocks.py` | Conversation block model + render |
| `tui_render.py` | Markdown / wrap / fold at column width |
| `tui_events.py` | SSE / activity event mapping |
| `tui_completion.py` | Slash + `@` completion |
| `client.py` | HTTP + SSE client |
| `runner.py` | Entry; documents ≥ 80 columns for TUI |
| `text.py` | Control-sequence sanitizer re-exports |

## 4. Where styles live

### Web

| Location | Role |
|---|---|
| `web/src/styles/tokens.css` | Semantic colour / radius / font tokens; Tailwind HSL block; imported first from `index.css` |
| `web/src/index.css` | Tailwind 4 entry: `@import` tokens → animations → chrome → dotGrid → theme → `tailwind-v3-theme.css` → preflight → base → utilities; `@theme inline` maps shadcn colours |
| `web/src/styles/tailwind-v3-theme.css` | v3 default theme values Tailwind 4 changed |
| `web/src/styles/base.css` | Global base after preflight |
| `web/src/styles/chrome.css` | Desktop window chrome (`html.desktop-mac`) |
| `web/src/styles/animations.css` | Shared motion utilities |
| `web/src/styles/dotGrid.css` | Page-ground grid |
| `web/src/styles/conductor.css` | **Loaded after `index.css` in `main.tsx:10`** — alternate vermilion / rice-paper brand layer that reassigns the same `--color-*` names |
| `web/src/App.css` | `.app-layout` viewport shell |
| Feature / page CSS | `features/*/*.css`, `pages/Plugins/**/*.css`, `pages/Login/*.css`, `pages/Settings/*.css`, component CSS under `components/` |
| Tailwind utilities in TSX | Primary styling path for `components/ui` and many Settings/Plugins screens |
| `DESIGN.md` | Authoritative "Operations Terminal" language (cyan accent, 2–6px radius) |

### TUI

| Location | Role |
|---|---|
| `libs/ptc-cli/ptc_cli/superagent/tui_theme.py` | `_DARK` / `_LIGHT` / `ansi16` / `none`; `resolve_theme`, `style_for`, contrast |
| `tui.py` `_FALLBACK_COLORS` | Duplicate dark palette when theme import is unavailable (`tui.py:230–243`) |
| prompt_toolkit style classes | Applied via `style_for` / class names like `tui-warn`, `permission` |

Web i18n: `web/src/locales/{en-US,zh-CN}.json` — one JSON file per locale,
lazy-loaded as one chunk each by `web/src/i18n.ts:22–25` and handed to unit
tests by `web/src/test/setup.ts:4–12` (keyed parity enforced by
`locales/__tests__/keys.test.ts`). The catalogs already carry a
`commandPalette` namespace (title, placeholder, research/markets entries) that
no `t('commandPalette.…')` call in `web/src` uses — a leftover, not the new
palette. TUI copy is mostly English literals with at
least one zh-CN-only hardcode (`_UNSUPPORTED`).

## 5. Ad-hoc colours and sizes

Measured excluding `tokens.css`, `tailwind-v3-theme.css`, `__tests__/`, and
`locales/`.

| Metric | Count |
|---|---|
| Hex occurrences outside the token/theme files | **109** |
| Unique hex values in that set | **72** |
| Hex in `conductor.css` alone | **83** of those occurrences |
| `rgb()`/`rgba()`/`hsl()`/`hsla()` outside tokens | **142** |
| Arbitrary Tailwind value class occurrences in `.tsx` | **203** |
| Unique arbitrary Tailwind classes | **46** |
| `Npx` literals in non-token `.css` | **1470** |
| `rounded-full` in `.tsx` | **26** |
| TUI hex literals outside `tui_theme.py` | **13** (all in `tui.py` fallback map) |

Heaviest arbitrary-size call sites (occurrences):
`ConnectedAccounts.tsx` (14), `PluginReportStep.tsx` (9), MCP modals /
`PreferencesTab` (8 each). Example ad-hoc type scale:
`text-[0.625rem]` / `text-[0.6875rem]` in
`pages/Settings/panels/ConnectedAccounts.tsx:367` and neighbours.

## 6. Top 15 visual and usability problems

Evidence is `path:line`. Severity is relative to `DESIGN.md` + a shared
web/TUI terminal language.

1. **`conductor.css` overrides the Operations Terminal tokens at runtime.**
   `main.tsx:10` imports it after `index.css`, and
   `styles/conductor.css:1–12,35–75` redefines fonts (Inter / Noto Serif SC)
   and the full `--color-*` palette to vermilion ink on warm paper
   (`#C0523B`, `#0C0B09`), fighting `DESIGN.md:18–46` and `tokens.css`.

2. **Web accent (cyan) and TUI baton (indigo) disagree.**
   Web: `#5BC8FF` dark (`DESIGN.md:21`, `tokens.css:140,162,167`) /
   `#0D6F91` light (`tokens.css:328,351,354`).
   TUI: `baton="#8B9CFF"` / `"#3F4FC8"` (`tui_theme.py:107,119`). Same product,
   two brand colours.

3. **Duplicate TUI palette in `tui.py` can drift from `tui_theme.py`.**
   `_FALLBACK_COLORS` (`tui.py:230–243`) copies dark hexes; `faint` is
   `#3A4150` there vs `#2A2F3A` in `tui_theme.py:106`.

4. **Floating surfaces still use three fills.**
   Documented debt at `tokens.css:71–75`: popover/select → `--popover`,
   tooltips/menus → `bg-elevated`, dialogs still on page/background
   (`dialog.tsx:40,47` use `bg-background`; `wizard.tsx:58` uses
   `bg-(--color-bg-page)`).

5. **Super-agent web CSS is soft-card, not terminal geometry.**
   `features/superagent/superagent.css:6–12,27` uses `border-radius: 8–12px`
   and status pills at `border-radius: 999px`, against `DESIGN.md:24–26`
   (2px / 3px / max 6px; pills only for semantic tags).

6. **Error text uses a soft wash token as foreground.**
   `.sa-error` and `.sa-status--bad` set
   `color: var(--color-danger-soft)` (`superagent.css:6,29`) — that token is
   defined as a translucent background (`tokens.css:177–179`), so failed
   state text washes out.

7. **Stale font commentary vs live stacks.**
   `tokens.css:32–34` still says "Sora is the app's voice" while
   `--font-ui` / `--font-content` are Geist (`tokens.css:41–42`);
   `conductor.css:9–11` then swaps Inter + serif display on top.

8. **Danger buttons keep Tailwind default reds instead of semantic loss red.**
   `tokens.css:173–175` (`#dc2626`, `#991b1b`, `#f87171`) sit beside
   `--color-loss: #F05252` (`tokens.css:155`), so destructive controls do not
   match P&L / failure vocabulary.

9. **Ad-hoc type sizes in Settings Connected Accounts.**
   Repeated `text-[0.625rem]` / `text-[0.6875rem]`
   (`ConnectedAccounts.tsx:367,598,611,648,…`) bypass the token type scale
   and densify inconsistently with mono kickers in `DESIGN.md:55–60`.

10. **Dialog / sheet radii exceed the terminal max.**
    Mobile sheet `rounded-t-3xl` (`dialog.tsx:40`, `ModalShell.tsx:212`);
    desktop dialog `rounded-lg` (`dialog.tsx:47`) vs `--radius-card: 0.25rem`
    (`tokens.css:242–244`).

11. **Slash command palette stops at the verb — no argument completion.**
    `TuiCompleter.get_completions` only runs `SlashCompleter` while the
    composer text has no space (`tui_completion.py:197–199`); after
    `/jobs␠` or `/merge␠` it falls through to `@path` completion. The verb
    match itself is a case-sensitive prefix, `name[1:].startswith(fragment)`
    (`tui_completion.py:101`), so `/DSP` or `/dsp` finds nothing — current
    shipped behaviour that the TUI palette slice (PLAN S04) replaces with the
    D1 fuzzy matcher. Operators
    get no search/completion for documented args (`/jobs watch`,
    `/merge <line> --into`, `/review run …`), and shell vs TUI never share
    one argument grammar.

12. **TUI refuses the full UI below 80 columns; no 40-col contract.**
    `MIN_TUI_COLUMNS = 80` (`tui.py:106`); `choose_mode` (`tui.py:734–757`) drops
    to the line shell when `columns < MIN_TUI_COLUMNS` (`tui.py:754`). View width floor is 40 only
    *inside* an already-open TUI (`tui.py:1294`). Ticket target of layout
    contracts at 40 / 80 / 120 is unmet.

13. **Nav icons collide across distinct destinations.**
    `navItems.ts:20–22` reuse `GitBranch` for `/lines`, `/dispatch`, and
    `/agent-fleet`; `navItems.ts:25–26` reuse `Blocks` for `/plugins` and
    `/skills-mcp`. The rail cannot be scanned by glyph alone.

14. **TUI / shell command surfaces are uneven for operators.**
    Line-shell `HELP_COMMANDS` (`shell.py:47–62`) omits many TUI commands
    that `SLASH_COMMANDS` lists (`tui_completion.py:46–76`: `/attach`,
    `/review`, `/merge`, `/details`, …), so `/help` teaches different
    products in the two modes.

15. **Locale: web is bilingual; TUI hardcodes zh-CN for a shared notice.**
    `_UNSUPPORTED = "服务端暂不支持"` (`tui.py:227`, `shell.py:98`) with no
    English twin, while web catalogs keep `en-US` / `zh-CN` in lockstep.
    Phase blurbs (`tui.py:115–124`) are English-only. zh-CN and en both need
    to keep working across the redesign.

Note: Login, Legal, and OAuth mount outside `AuthenticatedShell`
(`App.tsx:338–414`), so their `100vh` / `h-screen` sizing is their own
full-viewport chrome — not a violation of the in-shell
`.app-layout`-owns-`100vh` rule in `web/AGENTS.md`. Live `/model` rendering
passes `width=self._view_width()` into `model_table` (`tui.py:3446–3448`);
`tui_rich.DEFAULT_WIDTH` is only a helper default for callers that omit
`width`, not evidence of wrong on-screen width.

---

Keyboard conflict carried into the plan: the shipped TUI binds Shift+Tab to
the mode cycle (`tui.py:4156`) and advertises `shift+tab mode`
(`tui.py:2129`, `tui.py:201`); browsers need Shift+Tab for reverse tab order,
so decision D3 keeps Shift+Tab as previous focus on both surfaces and moves the
mode cycle to Alt+M.

## 7. Verification log

Re-checked against the worktree at `63731a5f0` on 2026-11-10 (read-only `grep`
/ `sed -n` / `find`):

- File counts in §1–§2 (Plugins 221, Setup 33, Login 27, Settings 22,
  superagent 20, dispatch 6, agentfleet 5, skills-mcp 4, OAuth 3, Legal 1;
  components 98, ui 30, mcp 27, model 17, nav 4; `web/src` 651 / 302 `.tsx` /
  30 `.css`; hooks 68, lib 76, api 33, styles 8; catalogs 4980 lines each) —
  all hold.
- §5: 109 hex occurrences / 72 unique outside the token files, 83 in
  `conductor.css`, 26 `rounded-full`, 13 hex in `tui.py` — hold.
- §6 claims #1, #4, #5, #6, #7, #8, #9, #10, #13, #15 and the note's
  `App.tsx:338–414` / `tui.py:3446–3448` — hold as written.
- Corrected: #2 (`tokens.css` lines for the dark and light accent), #3 and §4
  (`_FALLBACK_COLORS` starts at `tui.py:230`), #12 (`choose_mode` range and the
  `tui.py:754` check), #14 (`HELP_COMMANDS` closes at `shell.py:62`); #11 gained
  the case-sensitive prefix evidence (`tui_completion.py:101`).

Related: `PLAN.md` in this directory orders the remediation slices.
