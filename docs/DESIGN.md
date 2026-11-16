# Conductor visual language

Conductor is not a re-skin of the upstream web app. Phase 1 replaces the shared
chrome and reading surfaces with a deliberate **中文研究台** language:

- **Ground**: 墨黑 (dark) / 宣纸 (light), warm neutrals rather than blue-slate.
- **Accent**: 朱砂 vermilion for brand/active/selected states; jade for
  down/positive-secondary. No cyan terminal accent.
- **Type**: Noto Serif SC for headings and long-form research prose; Inter for
  chrome and controls; JetBrains Mono for code/data.
- **Geometry**: 2px control/card radius, hairline borders, flat fills, no glass
  or glow. A single paper-grain gradient keeps large surfaces from looking flat.
- **Reading**: chat transcript is a manuscript column (serif, 1.9 line-height)
  rather than a message-bubble app; code/tables stay in UI/mono faces.
- **Semantic convention**: vermilion and jade stay out of the error palette, so
  error text never turns green.

Implementation: `web/src/styles/conductor.css`, loaded after Tailwind from
`web/src/main.tsx`. It overrides the semantic tokens in `tokens.css` for both
`[data-theme="light"]` and the default ink theme, so components that already
read semantic tokens inherit the new language without per-component rewrites.

## Coverage

The token layer is global, and these surfaces were audited in both themes:

- dashboard (KPI strip, summary bars, stat cards, brief cards)
- workspace and chart panels
- settings (config rail, rows, segmented controls)
- plugins (section rail, cards, add/search actions)
- chat shell, transcript, composer and orchestration panel

Full frontend suite after the redesign: 771 files / 7,824 tests passed;
production build 378.3 kB gz.

The brand mark and favicons now use the same language: a rice-paper/ink tile,
a square A, and one vermilion bar. The expanded sidebar is referred to as the
research-desk panel rather than an upstream product's panel.


## Final consistency pass

The remaining hard-coded cyan glows/borders in PageLoading, Memory, Chat, the
dashboard cards and Automations were replaced with the vermilion accent family.
The whole web client now reads from the Conductor token layer; no page keeps an
upstream cyan surface.
