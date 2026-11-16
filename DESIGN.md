# Conductor Web Design System — "Operations Terminal"

The visual language of the web app (`web/`): a dense, near-black operations
terminal for multi-agent work, where **work is the interface** and color is
reserved for meaning. Tokens are the single source of truth — every value below
lives in `web/src/styles/tokens.css`, and every `var(--color-*)` referenced
anywhere must be declared there (enforced by
`web/src/styles/__tests__/tokenRefs.test.ts`).

The 2026-10 terminal pass replaced the earlier "Quiet Workspace" card language:
square 2–6px geometry, mono metadata, hairline rails, cyan selection, and
tabular figures. The reading surfaces (chat prose, documents) keep generous
line-height; the operational surfaces (dashboard, settings, plugins, lists)
are built for scanning.

## Identity

- **Temperature**: near-black steel. The app ground is `#060A0F`, cards step up
  through `#080E15` / `#0E1924`; a faint 48px grid belongs to the page ground,
  never to a card.
- **Accent**: terminal cyan — `#5BC8FF` (dark) / `#0D6F91` (light). It marks
  selection, active navigation, primary action, and live state; red/green stay
  reserved for state and direction; amber is warning/attention only.
- **Radius**: square by default. 2px controls and rows · 3px compact cards ·
  6px (`--radius-card`) is the maximum for a working surface. Pills survive only
  for semantic tags (state, ownership, direction).
- **Depth**: hairlines first. No card shadows in operational surfaces; one
  elevation shadow exists only for floating menus and dialogs.
- **Density**: operational rows are 28–42px high; section labels are mono
  uppercase; numbers are right-aligned and tabular.

## Palette (both themes live in tokens.css)

| Role | Dark | Light |
|---|---|---|
| Page ground | `#060A0F` | white |
| Canvas under card grids | = page | `#F5F4F1` warm paper |
| Cards | `#080E15` + steel hairline | white + `#E8E8E6` hairline |
| Elevated (menus, tooltips) | `#0E1924` | `#F7F7F6` |
| Primary text | `#D8E4F0` | `#1F1F1E` |
| Focus ring | accent edge | accent edge |
| Accent | `#5BC8FF` | `#0D6F91` |
| Primary button | `#5BC8FF` bg / `#041018` text | `#1F1D1A` bg / `#FAF9F7` text |
| Profit / loss | `#2FBF71` / `#F05252` | `#1A7F37` / `#CF222E` |
| Market up / down | `#F2555A` / `#2FBF71` | `#CF222E` / `#1A7F37` |
| Warning | `#D8B04C` | `#B45309` |

Background roles (`bg-page` → `bg-canvas` → `bg-card`/`bg-tool-card` →
`bg-elevated`, `bg-input`, `bg-popover`) are documented in the comment block
above the background group in `tokens.css` — pick by surface role, never by
matching a hex. Floating surfaces deliberately do not share one fill yet; don't
converge one of them in isolation.

## Typography

| Role | Face | How |
|---|---|---|
| UI + headings | **Geist** (+ Noto Sans SC for CJK) | `--font-ui`; headings read as compact UI labels, not display marketing type |
| Reading | **Geist** | `--font-content`, line-height 1.6–1.75 |
| Data, numbers, kickers, section rails | **JetBrains Mono** | `--font-mono`; `.terminal-kicker`, `.terminal-meta`, `.terminal-value`, `.ui-num` |

Tabular numerals (`font-variant-numeric: tabular-nums`) wherever digits align.

## Accent discipline (the rule that makes the system)

Cyan is the terminal's interaction signal. It may fill the **primary action**
and mark **selection** (active nav edge, active tab edge, selected row, live
dot). Amber is reserved for warnings and attention states. Never use it as a decorative wash behind prose, as a card background, or
as a one-sided bar on a content card — liveness and state are carried by the
glyph and the label. Red/green belong to market direction and to success/
failure semantics; never use them for selection.

The rule governs the product's own surfaces. Illustrations that *depict* the
product are not one of them: the onboarding intro panels draw a miniature of the
app rather than being part of it, and they carry the accent structurally, in
washes, glows and fills. A picture of a branded thing reads as brand, not as
state. The alternative is a picture whose color has to be kept in step with the
brand by hand, which is how that panel spent a rebrand still showing the old one.

**The sign-in page is the one product surface that is exempt**, and the reason
is that the rule has nothing to bite on there. It carries no chrome, no
navigation and no data, so there is nothing for the accent to annotate. The accent
is its whole interaction vocabulary instead: the submit button has always taken the
fill on hover, and keyboard focus takes the same fill rather than inventing a
second language for the same "about to act" state. Focus is not left to hue
alone, since hue is what a color deficiency flattens: an underline by default, a
doubled edge on the boxed controls, an outline under forced colors. The exemption
is the `.login-page` surface itself, not the route it mounts on: the same build
serves the app from that route once someone is signed in.

Semantic color (profit/loss/warning/danger) is separate from the accent and
never substitutes for it.

## Status & liveness vocabulary

- **Live work** renders the shared ascii `Loader` glyph (cyan) + text — never
  a spinning lucide icon.
- **Completed** on dense surfaces (nav subagent rows) renders **no glyph**:
  absence of the liveness glyph is the done state. Roomier surfaces (status
  bar) may keep a celebratory check.
- Exceptional terminal states keep informative glyphs (error = alert,
  cancelled = stop, spawned-idle = hollow circle).
- Status/icon mappings live in one table per vocabulary
  (`web/src/pages/ChatAgent/components/taskStatusUi.tsx`); surfaces declare
  overrides there instead of restating ladders.

## Motion

Entrance animations are shared utilities (`web/src/styles/animations.css`):
staggered fade-up with `cubic-bezier(0.16, 1, 0.3, 1)`, ≤ 500ms. No ambient or
looping decoration; motion signals state change only. Respect
`prefers-reduced-motion`.

Streamed chat text fades in as the typewriter reveals it, on its own gentler
curve (`EASE_REVEAL`, `DURATION.reveal` in `web/src/lib/motion.ts`): the
entrance curve is at half opacity a tenth of the way in, which on a word reads
as a pop.

## Empty states

Dot-grid texture is reserved for empty states only — never behind content.

## Working rules

1. New colors enter through `tokens.css` (both themes, plus the role comment)
   — never a hex literal in a component. Canvas painters that can't read CSS
   variables go through `web/src/lib/themeTokens.ts`.
2. Style both themes in the same change; the light theme is a first-class
   surface, not an inversion.
3. Buttons are quiet: `--color-btn-primary-*` for primary, neutral borders for
   secondary. `--color-text-on-accent` exists solely for text sitting on an
   cyan accent fill (the primary action).
4. Liveness = glyph + text, per the vocabulary above.
5. When in doubt, remove decoration rather than add it.
