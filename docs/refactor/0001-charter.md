# 0001 — Deep refactor charter

- Status: proposed (Phase 0 evidence pack)
- Branch: `refactor/phase0-evidence`
- Complements: [ADR 0001](../adr/0001-subscription-connection-framework.md) (connection framework)
- Authoritative machine-readable scope: `adoption-matrix.yaml`

## Purpose

Rebuild three layers of Conductor around one coherent model instead of adding
features: **agent orchestration** (main ↔ subagent contract, goals/plans,
verification, permissions, context), **frontend workspace shell** (panel modules,
sessions, review, memory/agent visibility) and **memory/data** (five-tier memory,
retrieval, event log, auditable context reconstruction). Reference products are
used as pattern sources, never as a feature checklist.

## Invariants (must not change without an explicit migration + approval)

1. **Credentials and secrets**: BYOK/OAuth encryption, token storage, log
   redaction, and the no-secret-in-URL rule stay as they are. The refactor adds
   no new credential store.
2. **Deployment topology and security boundaries**: Homelab rootless Docker,
   UDS-only backend, sandbox isolation and egress policy are unchanged. M2 app
   stack remains the user-acceptance environment.
3. **Public behavior that users can observe**: SSE event contract, thread/workspace
   ownership, file/artifact semantics, model routing and readiness, i18n and
   `DESIGN.md` accessibility rules. Changes require a compatibility note and a
   differential check.
4. **Database and API compatibility**: additive migrations only in Phases 1–2;
   destructive changes require a documented backfill, a rollback path and an
   explicit approval. Down-migrations must be exercised.
5. **Multi-worker truth**: Postgres remains the source of truth; Redis is
   coordination; no new module-level state that a request path consults.
6. **No feature-for-feature porting**: a pattern enters scope only through an
   entry in `adoption-matrix.yaml` with path, acceptance and risk filled.

## Rewrite / refactor / keep decision table

Evidence for each row: [`0003-current-state.md`](0003-current-state.md).

| Area | Current state (evidence) | Decision | Why |
| --- | --- | --- | --- |
| Agent runtime (`src/ptc_agent/agent/`, LangGraph `create_agent` + middleware) | Mature middleware stack, roles, subagents, compaction, runtime context | **Refactor + extend** | Keep the runtime; add explicit contracts/entities, don't replace LangGraph. |
| Subagent orchestration (registry/assembly/compiler/merge, background orchestrator, `subagent_tasks`/`subagent_runs`) | Exists with lifecycle tables and merge logic; contract implicit in prompts | **Refactor** | Make the implicit contract explicit and verifiable; extend tables, keep owners. |
| Goals/plans | Not first-class | **Extend** | New entities layered on threads, not a parallel runtime. |
| Permissions | AskUser HITL + sandbox/credit/order policies, no unified mode | **Refactor** | One policy layer over existing checks; no new security model. |
| Memory (LangGraph store user/workspace tiers, baseline index, subagent read-only + proposals, memo store) | Exists but two tiers, no eval, read-only API, no injection audit | **Refactor + extend** | Preserve store as content backend; add tier/index/audit/eval around it. |
| Transcripts / turn slices / replay facts | Implemented (migration 056/064/065) | **Keep** | Already the historical substrate; reuse for harness and timeline. |
| Frontend chat workspace | Large mature component set (122 components) with a 3-surface right panel | **Refactor** | Extract panel-module contract and shell; do not rewrite components wholesale. |
| Design system (`DESIGN.md`, tokens) | Enforced tokens, tests | **Keep + extend** | Extend tokens only through the documented process. |
| Desktop shell (`desktop/`) | Remote-URL Electron wrapper with URL classification tests | **Keep** | No bundle change needed; acceptance uses it as-is. |
| Deployment (the maintainer's homelab deployment (not published), compose) | Documented, rooted in backups/rollback | **Keep** | No topology change; only revision updates. |
| Data model | Most entities exist (threads, responses, runs, slices, provenance, transcripts) but relations/lifecycle undocumented | **Refactor + extend** | Document + add event/memory projections; no table rewrites. |

## Compatibility windows

- **Phase 1**: additive only. Old read paths keep working; new columns nullable;
  event/memory projections dual-written behind `REFACTOR_*` flags (names in
  [`0004-data-model.md`](0004-data-model.md)).
- **Phase 2**: new memory retrieval can be disabled per deployment; old baseline
  index path stays until eval thresholds are met.
- **Phase 3**: new shell behind a frontend flag; old chat layout remains the
  fallback for one release.
- **Phase 4**: remove old paths only after M2 acceptance + Homelab regression +
  one rollback drill.

## Rollback

- Migrations must provide `downgrade()`; Phase 1 exercise it on a staging copy
  before release.
- Frontend/backend flags default off in Homelab until accepted on M2.
- Homelab update follows the maintainer's homelab deployment (not published) (backup, image revision
  labels, restart, rollback). No deploy without explicit approval.
- Persistent data added by the refactor is additive; nothing existing is
  rewritten in place.

## Gates (definition of done per slice)

1. Contract + schema written and reviewed (no code-first).
2. Additive migration with up/down exercised; backfill idempotent.
3. Old/new differential check for touched behavior (see
   [`0006-differential-harness.md`](0006-differential-harness.md)).
4. Memory eval delta reported (Phase 2+; see [`0005-memory-eval.md`](0005-memory-eval.md)).
5. Security checks: secret scan, permission negative tests, sandbox/egress
   regression where touched.
6. M2 app user-acceptance steps recorded (for user-visible slices) before
   Homelab deploy.
7. Evidence table row appended; docs updated in the same PR.

## Non-goals

Computer use, voice input, video/image generation, cloud-synced agent sessions,
IM/bot channels, native mobile apps, worktree-isolated parallel coding, infinite
canvas/floating windows/screenshot annotation, new model providers or
aggregators, and any new external service dependency. Rejected/deferred entries
carry the rationale in the adoption matrix.

## Open decisions (resolved — see `0008-decisions-and-review-workflow.md`)

1. Goal entity: per-thread single goal vs multiple goals per thread (default:
   one active goal per thread, history retained).
2. Memory tiers: project tier source of truth — workspace metadata vs a new
   explicit "project" object (default: extend workspace; avoid a parallel object).
3. Event log retention: cap by count vs age (default: 90 days or 200k events per
   deployment, whichever first; configurable).
4. Verifier cost: model choice and token budget per goal (default: reuse the
   thread's background model, hard budget recorded in the goal row).
5. Frontend flag rollout order: shell before review workflow or review before
   shell (default: shell skeleton → memory inspector → review).
