# Frontend ↔ backend alignment refactor

Objective: rebuild the web surface around the current Conductor backend design
(dual runtime, native CLI inventory, SSE, CN market, automations, memory, MCP,
sharing), not around the legacy US-market UI.

## Ground rules

- The backend is the source of truth for provider ids, live proof and
  capabilities. The frontend never derives a capability from a provider name.
- One shared label/capability vocabulary; no duplicated provider maps.
- Typed API/SSE contracts; no `as any` growth.
- Feature folders own route pages; cross-page logic goes to `lib/`/`hooks/`.
- Every phase must keep `typecheck`, unit tests and `build` green, then add
  targeted browser coverage.

## Phases

| Phase | Scope | Status |
|---|---|---|
| 0 | Baseline: typecheck/unit/build; record E2E debt | ✅ |
| 1 | Backend vocabulary: shared native labels, native runtime inventory, thread native binding badge | ✅ |
| 2 | SSE/domain contracts: canonical event union, native_agent/native-runtime events, remove loose local shapes | ✅ |
| 3 | Split giant ChatAgent modules (`useChatMessages`, `ChatView`, stream router) | ✅ |
| 4 | CN dashboard/market legacy cleanup and stale Playwright specs | ✅ |
| 5 | Visual/mobile redesign over semantic tokens; final build + E2E acceptance | ✅ |

## Phase 0/1 evidence

- `web/src/lib/nativeRuntimeLabels.ts` is the single provider label map.
- `NativeRuntimeInventory` reads `GET /api/v1/native-agents` and renders live
  status + backend capabilities; no hardcoded matrix.
- Chat header reads `thread.metadata.native_runtime` and shows the immutable
  provider binding.
- Typecheck passes; unit 7,820 passed; Vite build and critical-path check pass.
- Live browser: Settings → Experiments shows the real eight-provider inventory
  (Codex/DSH/Grok/Kimi/MiMo/MiniMax/StepFun/ZCode), zero page errors.

### Phase 2 progress

- Added `session/stream/wireEvents.ts`: runtime narrowing from the loose stream
  event to canonical `NativeAgentProgressEvent` and `InterruptEvent`.
- `processStreamEvent` now validates `native_agent` and `interrupt` frames
  before projecting them, so malformed frames are ignored instead of producing
  undefined notification text or a broken card.
- Added guard unit tests (valid/invalid native phases and action_requests).

### Phase 4 progress

- Dashboard spec rewritten for the CN desk (5 passed); removed US indices/news,
  suggestion-chip and welcome-banner specs for UI that no longer exists.
- Deleted `market-view.spec.js` (/market redirects) and removed /market from the
  window-chrome route sweep; fixed SharedChat brand copy.
- Focus-ring/model-menu spec now targets a mocked thread for the composer,
  opens Create Workspace through its real header action, and removes the
  dashboard symbol-search rows.
- Root cause of the remaining focus failures was an unlayered base.css
  `:focus-visible` rule overriding the layered pointer-focus gate in
  tokens.css; removing the duplicate restores the transparent ring under a
  pointer.
- Full Playwright: **143 passed / 7 skipped / 0 failed**.

### Phase 5 acceptance smoke

`web/scripts/live-page-smoke.mjs` drives the real frontend/backend at desktop
(1440×900) and mobile (390×844) for `/dashboard`, `/chat`, `/chat/<workspace>`,
`/memory`, `/automations`, `/plugins`, `/settings` and `/settings?tab=experiments`.
It fails on page errors, blank routes, loading shells or horizontal overflow.

Run 2026-10-29: **16/16 route × viewport checks passed**, no page errors or
horizontal overflow. Full page-level visual redesign is still pending.

### Dead API cleanup

Removed an unused client call that pointed at a nonexistent endpoint (not in the
backend OpenAPI). The remaining calls were checked against the OpenAPI paths.

### Phase 3 progress

- Extracted the HITL resume handler into `chatMessages/resumeHitl.ts`
  (`ResumeHitlDeps`); wiring moved below `streamRouterDeps`/`settleRetry`.
- Extracted `streamEdit` + edit/regenerate handlers into
  `chatMessages/editRegenerate.ts` (`EditRegenerateDeps`), leaving the hook
  with composition and the remaining retry path.
- Extracted the slash-command action handler (/compact, /offload) out of
  `ChatView.tsx` into `chatView/chatAction.ts` (`ChatActionDeps`).
- `useChatMessages.ts`: 2,744 → 2,374 lines (−370); modules are 269 and 252
  lines and own one flow each.
- `ChatView.tsx`: 1,906 → 1,796 lines; new action module is 150 lines.
- Removed imports left unused by the extraction; eslint on the hook: 0 errors,
  warnings 47 → 28.
- Verified after both splits: typecheck/build, unit 7,823 passed, full
  Playwright 143 passed / 7 skipped / 0 failed, live page smoke 16/16.

## Completion record (2026-10-29)

All phases verified against the live workspace:

| Check | Result |
|---|---|
| `npm run typecheck` | pass |
| `npx vitest run` | 771 files / 7,823 passed |
| `npm run build` | pass, critical path 374.5 kB gz |
| `npx playwright test` | 143 passed / 7 skipped / 0 failed |
| live page smoke (desktop+mobile) | 16/16 routes, no page errors/overflow |
| backend health | 200 |

Residual, accepted technical debt (does not block the refactor):
`streamFromCheckpoint` remains in `useChatMessages`; the loose processor
`SSEEvent` remains the internal stream shape with canonical guards at the
native_agent/interrupt boundaries; full page-by-page visual redesign is
incremental work after this baseline.

## Known debt

- Phase 2/3 remaining debt: split `useChatMessages`/`ChatView` and continue
  canonicalizing the loose processor SSE shape.
- `useChatMessages.ts` (~2.7k lines), `ChatView.tsx` (~1.9k) and the loose
  `session/types.ts` SSE shape remain Phase 2/3 targets.
