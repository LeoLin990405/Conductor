# Conductor Web

React 19 + Vite + TypeScript console for **Conductor — the multi-agent
collaboration and aggregation operating platform**. The backend lives in the
repository root; this package owns the browser UI, the Electron renderer
surface, and the frontend test suite.

## Feature areas

- **Chat (`/chat`)** — streaming agent conversation with reasoning blocks, tool
  calls, subagent cards, file panel, artifacts, per-thread drafts, HITL
  approvals and native-CLI turns.
- **Projects (`/projects`)** — project goal, task dependencies, concurrency,
  pause/cancel/recovery, and the per-task worker threads.
- **Memory (`/memory`)** — workspace and cross-thread memory surfaces.
- **Automations (`/automations`)** — scheduled and one-shot runs, delivery,
  run history, pause/resume and duplicate.
- **Plugins (`/plugins`)** — installed plugins, MCP servers, skills, subagents
  and the secrets vault, plus the Discover catalog.
- **Settings (`/settings`)** — account, model connections and routing,
  connected accounts, data/search preferences, and advanced agent settings.
- **Shared chat** — public `/s/:shareToken` and `/a/:shareToken` views.

## Tech stack

| Category | Libraries |
|---|---|
| Framework | React 19, Vite 8 |
| Routing | React Router 8 |
| Server state | `@tanstack/react-query` 5 |
| UI | shadcn/ui primitives (Radix + Tailwind CSS 4), lucide-react |
| Editors / documents | Monaco, `react-pdf`, ExcelJS, KaTeX/rehype |
| i18n | `i18next`, `react-i18next` (zh-CN default, en-US) |
| Testing | Vitest, Testing Library, jsdom + Playwright |

## Commands

```bash
pnpm install
pnpm dev
pnpm typecheck
pnpm test
pnpm build
pnpm test:e2e
```
