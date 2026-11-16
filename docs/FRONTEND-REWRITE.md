# Conductor frontend rewrite

This is an application-level rewrite of the web client, not another theme pass.
The target is a research desk: fewer destinations, one command surface, a
research-first default route, and interaction state that belongs to Conductor
rather than the upstream Conductor shell.

## Target information architecture

| Destination | Route | Job |
|---|---|---|
| 研究台 / Research | `/research` (default) | start a question, resume recent research, see live workers |
| 自动化 / Automations | `/automations` | scheduled and triggered research |
| 资产 / Assets | `/plugins` | MCP, skills, subagents, connectors, installed plugins |
| 设置 / Settings | `/settings` | models, data sources, appearance, experiments |

Thread pages stay under `/chat/:workspaceId` and `/chat/t/:threadId`.

## Interaction model

1. **Command surface first.** `Cmd/Ctrl+K` opens one palette for navigation,
   starting a research thread, switching theme, and page actions. Search is
   fuzzy; Enter runs; arrows move; Escape closes.
2. **Research home replaces dashboard-as-landing.** `/` resolves to
   `/research`; the dashboard is a deliberate destination, not the default.
3. **One worker timeline.** Native, subagent, ManusResearch and direct tool
   workers share one timeline; no separate mode banners per provider.
4. **Manuscript reading.** Assistant prose is serif and citation-first; tool
   events and worker state are compact marginalia, not chat bubbles.
5. **Progressive disclosure.** Settings and assets use a single rail + one
   content column. No nested tab stacks that mirror upstream.

## Migration slices

1. Shell: command palette, nav trigger, shared layer (done).
2. Research home: prompt-first landing, recent research, live worker summary,
   and make `/` resolve to `/research` (done).
3. Chat: one worker timeline and margin-note offers (done); the duplicate
   open-thread tab strip and its persisted store are removed. A few upstream
   status components still need to fold into marginalia.
4. The desk: the floating composer is gone and a sticky desk nav now moves
   between the overview and the research view. Extracting the remaining
   charts/cards into the desk layout is next.
5. Automations, assets, settings: one content-column pattern each. Settings,
   Automations and Assets now share the new eyebrow + serif content header;
   their inner cards still carry the older terminal vocabulary.
6. Shell simplification: the collapsed sidebar state machine, desktop history
   arrows, `terminal-kicker` slash vocabulary and the manual route-preload
   machinery are removed. Routes load when mounted; the shell no longer warms
   chunks ahead of the router.

## Rules

- Backend APIs and SSE contracts are unchanged.
- Every slice keeps existing tests green and adds a focused test for the new
  interaction.
- The old shell stays mounted until its replacement reaches parity; slices must
  not leave two competing navigation states on screen.

## Research object: the brief view

`/chat/t/:threadId/brief` is the first product view that is not a chat or a
dashboard: it reads the thread's provenance API and groups the external records
the agent actually used by turn, with per-source type/agent/timestamp and a
print/export action. The chat header's document icon opens it. The heading now also carries the
thread goal/plan step list and the PTC worker trace beside the evidence, so the
page reads as question -> plan -> workers -> sources. This is the anchor for the
"research project / evidence wall / deliverable" model. The brief now also
downloads itself as a self-contained Markdown deliverable (question, plan,
workers, per-turn sources, timestamps). Evidence freshness is surfaced in the
header (data as of / sources older than 24h) and carried into the download; a
saved project object and richer PDF/report template are next. Saved projects
now exist as a local-first shelf at `/research/projects`: saving a brief stores
its title/objective/template/counts/data-as-of in localStorage, and the shelf
opens the source brief or removes the bookmark. Printing now produces a
report cover (title, objective, data-as-of, counts, no-advice disclaimer) with
the app chrome hidden and A4 page margins. Three Markdown
templates now ship: full brief, project memo (newest sources only) and source
audit.


## Conductor-managed browser for OpenCLI

`/browser` (and the command palette) controls a Chromium window launched by the
backend with its own `~/.conductor/browser-profile` and a loopback CDP port
(9223). OpenCLI subprocesses receive `OPENCLI_CDP_ENDPOINT` automatically, so
research data sources drive this browser instead of the user's Chrome profile.
The desktop shell does not dock a browser: it used to render a `<webview>` here and
expose a loopback CDP port (9224) with `~/.conductor/desktop-browser.json`, and both
were removed because nothing in the tree consumed them and the port gave any local
process the signed-in window. The page keeps the app-managed window.
