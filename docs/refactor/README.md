# Deep refactor — Phase 0 evidence pack

This directory is the evidence pack the deep refactor requires **before** any
production code changes. The objective is depth, not breadth: reference patterns
are adopted only where they replace or extend an existing Conductor code path and
can be verified independently.

Status: **Phase 0 evidence pack complete; Phases 1–3 implemented behind default-off flags; Phase 4 capture/ops tooling ready; nothing deployed (see the internal document `0007-acceptance` (not published)).**
Branch: `refactor/phase0-evidence`. No production code is touched in Phase 0.

## Documents

| File | Purpose |
| --- | --- |
| [`0001-charter.md`](0001-charter.md) | Refactor charter: invariants, compatibility windows, rollback, rewrite/refactor/keep decision table, non-goals, gates. |
| [`0003-current-state.md`](0003-current-state.md) | Inventory of Conductor's agent, frontend and memory/data layers with concrete paths, tables, endpoints and gaps. |
| [`0004-data-model.md`](0004-data-model.md) | Unified entity model and the additive migration plan (up/down, backfill, flags). |
| [`0005-memory-eval.md`](0005-memory-eval.md) | Memory evaluation set, metrics, seed cases, thresholds and reporting. |
| [`0006-differential-harness.md`](0006-differential-harness.md) | Old/new behavior capture and comparison design (flows, fixtures, SSE, DB/checkpoint snapshots, gates). |
| `adoption-matrix.yaml` | Machine-readable adopt/adapt/reject/defer decisions with sources, paths, acceptance and risk. Checked by the validator. |
| [`0008-decisions-and-review-workflow.md`](0008-decisions-and-review-workflow.md) | Resolved charter decisions (goal cardinality/creation, permission modes, review scope, memory proposals) and the review-workflow specification built on them. |

## Phase 0 checklist (acceptance for this pack)

- [x] Reference products studied from primary sources (installed bundles + official docs), not from summaries.
- [x] Every adopt/adapt decision names the Conductor path it replaces/extends, an acceptance check and a risk.
- [x] Rejected/deferred patterns recorded with reasons (scope discipline).
- [x] Current-state inventory cites real paths, tables, endpoints and components.
- [x] Unified data model documented against the existing migrations head (085) and existing run/turn tables.
- [x] Memory evaluation design and differential-harness design documented, including how each will be run headlessly.
- [x] Charter includes invariants, compatibility, rollback and the rewrite/refactor/keep decision table.
- [x] Validator passes (`uv run --frozen python docs/refactor/tools/validate_phase0.py` → `checked sources=9 entries=26 (adapt=5 adopt=18 defer=2 reject=1)`).
- [ ] Reviewed by the user; Phase 1 (contracts + migrations) may then start.

## Verification

```bash
# from the repository root
uv run --frozen python docs/refactor/tools/validate_phase0.py
```

The validator checks that every matrix entry has a source, a decision in the
allowed set, existing Conductor paths for adopt/adapt decisions, acceptance rows,
and that local links in this README resolve.

## Non-goals

See [`0001-charter.md`](0001-charter.md) §Non-goals. In short: no computer use,
voice, video, cloud sync, IM/bot channels, new pages for their own sake, new
providers/dependencies, or unbounded schema churn. M2 app version remains the
user-acceptance baseline; Homelab is the regression target.

## Implementation progress

| Slice | Status | Evidence |
| --- | --- | --- |
| Contracts vocabulary + state machines (`src/server/contracts/refactor.py`) | done | `tests/unit/server/contracts/test_refactor_contracts.py` (42 tests) |
| Deployment flags (`settings.get_refactor_flag`, `services/refactor_flags.py`) | done | default-off + truthiness + typo tests (10 tests) |
| Migrations 086–090 with downgrade (goals/plans, run contracts, events, memory projection, task tree) | done | offline `alembic --sql`; single head 090; real-Postgres up/down on an M2 scratch clone: 085→090 applies all five; unique-active goal, goal-status, permission-mode, memory tier/status and task status/source CHECKs reject drift; generated `search` tsvector returns the memory hit; 090→085 restores 085 with tables NULL and contract columns 0 |
| Contract↔schema binding test | done | `tests/unit/migrations/test_refactor_migrations.py` (12 tests); integration test `tests/integration/test_090_refactor_migrations.py` for CI |
| SQL layer (`src/server/database/refactor/`: goals, events, memory, tasks) | done | CAS updates, tenant-scoped reads, FTS search; statement shape pinned by `tests/unit/server/database/refactor/` |
| Service layer (`src/server/services/refactor/`: flags, validation, event redaction) | done | `tests/unit/server/services/refactor/`; flag-off is a strict no-op |
| Reader parity views (`readers.py`: goal+latest plan, event page, task tree, memory index; `None` = legacy hand-off, `[]` = new path empty) | done | `tests/unit/server/services/refactor/test_refactor_readers*.py`; task-tree orphan/cycle promotion and JSON-safe field whitelists pinned |
| Memory projection parity check (`compare_memory_projection`) | done | pure store-vs-projection checker (missing/extra/changed) for the backfill and memory eval |
| Real-Postgres smoke of the new SQL | done (host-level DB) | M2 scratch clone at 090: goal insert/CAS, plan review, event append+auto-seq, memory projection+FTS hit, task insert/status/tree — all returned rows. The smoke exposed a real bug: `COALESCE(%s, verifier_verdict)` bound a `json` value into a `jsonb` column; fixed to `%s::jsonb` and pinned by a unit assertion. |
| First dual-write call site: turn-finalize projection (`record_turn_finalized`) behind the `events` flag | done | wired into `RunCoordinator.finalize_run` after the terminal CAS (won only); best-effort, never raises; flag-off regression = 356 run-lifecycle unit tests; 8 unit + 3 CI integration tests for the projection path |
| Permission-decision event call site (`record_permission_decision`) behind the `events` flag | done | wired in the HITL resume branch of `ptc_run`; payload counts approve/reject/feedback/order verdicts only — no feedback text or action args; best-effort, resume proceeds on failure; flag-off regression = 576 handler/import-layering tests |
| TodoWrite task-tree call site (snapshot reconciliation) behind the `task_tree` flag | done | migration 091 adds title-per-thread partial unique index; `record_todo_snapshot` syncs completed snapshots from the SSE producer (owner resolved SQL-side); 4 DB-shape unit tests + 4 service tests + 3 CI integration cases; best-effort, live SSE frame unaffected |
| Live HTTP capture: F1 against the M2 app (`capture.py` + `cli capture`) | done | committed `fixtures/F1/login-live-20261025.json` (`synthetic:false`, app 12d17232); two live captures compare PASS 0/0; proxy-bypass fixed after a 502 from the host proxy; redaction gate passes |
| Playwright/SSE capture + live F2/F3 turns | next | blocked on a valid model credential in the M2 stack (provider 401s observed); synthetic F2/F3 remain the comparison baselines |
| Phase 2: budgeted memory-context selection (`memory_context.py`: tier quota → leftover fill, dedupe, CJK/latin token estimate, index block) | done | deterministic policy; `tests/unit/server/services/refactor/test_memory_context*.py`; DB-backed selection in `tests/integration/test_refactor_memory_context.py` |
| Phase 2: memory eval harness (`scripts/memory_eval/`, 10 cases) | done | baseline recall 1.00 / precision 0.95 / leakage 0 / stale 0 / budget 0/10; `make memory-eval` exit code gates thresholds |
| Phase 2: shadow memory assembly at turn start + injection audit on the run contract | done | flag `memory_index`; selection runs at turn start, writes `agent_run_contracts.injected_memory`/`context_budget` + a `memory.injected` event (ids/tokens only); finalize preserves the audit via preserve-on-empty upsert; prompt still renders the legacy baseline (Phase 3 flips it) |
| Phase 2 render switch: selected index block replaces the per-tier store indices behind the same flag | done | `build_turn_memory_block` at turn start → `<memory-context>` harness block; `with_memory_context_block` clears legacy memory sources and merges the block (inputs not mutated); selection failure falls back to the legacy baseline; template registered in prompts.yaml + envelope test cases |
| Phase 3 shell slice 1: goal/plan/task read view + composer goal strip | done | `GET /api/v1/threads/{id}/goal` is flag-shaped (200 + `enabled:false` hides the surface; ownership checked before any read) and returns the JSON-safe goal/plan/task-tree views; web hook + read-only `<GoalStatusBar>` in the composer stack, i18n en/zh. 5 route tests, 11 web tests, 2011 backend-app + 463 chat-view/hook regressions, tsc/eslint clean |
| Phase 3 shell slice 2: plan decision endpoint + review buttons | done | `POST /threads/{id}/goal/plan/{plan_id}/decision` guards ownership → flag → plan-belongs-to-thread → legal transition (404/409 mapped); returns the JSON-safe plan and appends a `plan.decided` event with counts only (feedback text stays on the plan row); composer strip shows Approve/Reject only for `awaiting_approval`, disables in flight, refetches the goal read on settle. 9 route tests, 14 web tests |
| Phase 3 shell slice 3: goal detail sheet + panel-module registry | done | `GoalPanel` dialog (goal/plan steps/task tree, defensive step shapes, flag/empty/loading states) opened from the strip's Details button; `panelModules` is now a typed registry (`PanelType` = file/detail/preview/memory, one lazy loader + title key each) consumed by header/right-panel/nav hook; 25 web tests across panel/registry/strip |
| Phase 3 shell slice 4: session tabs | done | per-workspace persisted store (`sessionTabsStore`, MRU + dedupe + cap 8, corrupt storage degrades to empty); strip above the transcript shows threads with titles read from the thread-detail cache, hides below two tabs and on mobile; tab click/close go through the route leave guard, closing the active tab moves to a neighbour; 14 web tests (store + strip) |
| Live Postgres retrieval probe (`scripts/memory_eval/live_pg.py`, 10 retrieval cases) | done | production predicate on M2 Postgres (temp-table, BEGIN/ROLLBACK, zero residue): en 5/6 fully retrieved; zh 0/4 — CJK cannot be retrieved under `simple` tokenization, and `simple` has no stemming. Findings + remediation plan (`pg_trgm` CJK path) in 0005; 24 probe tests |
| Memory retrieval remediation: CJK path (migration 092 `pg_trgm` + word-similarity branch) | done | triggered by the live probe, verified by the same probe: zh 0/4 → **4/4** fully retrieved, en unchanged 5/6 (non-stemming miss documented); latin FTS path untouched; `SET LOCAL` threshold keeps the GUC off the pooled connection |
| Deployment acceptance: 086–092 on a cloned app schema + migrated-DB probe + EXPLAIN | done | clone at 085 → `alembic upgrade head` → 092, pg_trgm + trigram index created; migrated-DB probe en 5/6 · zh 4/4; `EXPLAIN` confirmed Bitmap Index Scan on `ix_memory_entries_trgm` for the word-similarity path and ≥3-char CJK contains (2-char terms filter-scan, correctness fine); clone dropped, copies removed, app DB untouched at 085 |
| Migration down/up drill: 094 → 093 → head on an M2 clone | done | 085 → head 094 with the proposal queue + unique (run,type,hash) idempotency + CHECKs (duplicate inserted 0 rows, invalid type rejected), `downgrade 093` dropped only that table (permission table intact), re-upgrade recreated it empty; clone dropped, copies removed, app DB untouched at 085 |
| Migration down/up drill: 093 → 092 → head on an M2 clone | done | 085 → head 093 with `agent_thread_permissions` + CHECK (seeded `manual` row accepted, `skip` rejected), `downgrade 092` dropped only that table (memory projection intact), re-upgrade recreated it empty; clone dropped, copies removed, app DB untouched at 085 |
| Migration down/up drill: 092 → 091 → 085 → head on an M2 clone | done | 092 down drops only the trigram index (extension + rows kept, retrieval still answers through a scan), 085 down removes the new tables/columns with the base schema intact, re-upgrade recreates everything empty at 092; clone dropped, copies removed, app DB untouched at 085. Closes the gate-2 residual in 0007 |
| Memory projection backfill job (`scripts/refactor_memory_backfill.py`) + runbook | done | one row per detail entry (pointer line from `memory.md`, fallback description/first line), user+workspace tiers, dry-run default, `--apply` idempotent, parity report; 15 unit tests + 3 DB integration cases (CI) incl. CJK-searchable rows and double-apply; live run not performed because the M2 store table is empty (0 rows), documented in the runbook |
| Memory store-write dual write (`memory_dual_write.py` facade behind `memory_index`) | done | memory `aput`/`adelete` mirrored after the store write: detail upsert uses pointer text, index edit refreshes rows in one UPDATE, delete supersedes; non-memory namespaces delegate untouched; projection failure logged and dropped (store write never fails); mapping shared with backfill; non-UUID dev labels skipped. 13 unit + 6 DB integration cases; flag-off regressions unchanged (928 handler/run + 279 targeted) |
| Memory-proposal decision controls (inspector approve/reject) | done | each proposal row gains Approve/Reject (only when the row carries a proposal id), disabled while the mutation runs; approval writes the store first server-side; on settle every memory read is invalidated; 409 (already decided), 503 (store write failed) and generic failures are stated; 5 decision-UI tests. D5 is now end-to-end. |
| Memory-proposal decision route (D5): approve writes the store, reject is status-only | done | `POST /memory/proposals/{id}/decision`: owner + flag + status guards; approve writes the detail file + `memory.md` pointer, projects both via the dual-write mapping, then CASes the status; retry is pointer-idempotent; reject only moves status; store failure leaves the proposal `proposed` (503); `memory.approved`/`memory.rejected` events carry ids only. 4 approval-service + 8 route/service tests. Decision buttons in the inspector are next. |
| Memory-proposal queue (migration 094) | done | extracted proposals now land in `agent_memory_proposals` (identity per task run + type + text hash, `proposed`→`approved`/`rejected` lifecycle, owner/status index); extraction is idempotent and best-effort after the contract write; the read route switched to the queue; a decision CAS service exists (store-writing approval route is the next slice). 60 tests across migration/DB/service/dual-write |
| Memory proposal review surface (read-only, in the memory inspector) | done | the inspector's new "Proposed memories" section lists the owner's proposals from the D5 API (type chip + text + empty/flag states), fetched with the panel; approval is deliberately absent (must write the store). 13 new web tests; full 7819-test web suite passes |
| Memory proposal channel (D5): subagent `ProposeMemory` tool + extraction + read API | done | when `memory_index` is on, compiled subagents get a no-op `ProposeMemory` tool (gated at build, vocabulary pinned to the server contract); the server extracts valid calls from the task transcript into the subagent contract's `proposed_memories`; `GET /api/v1/memory/proposals` lists them owner-scoped from the contract jsonb (thread → workspace join), flag-shaped and read-only. Approval still deferred to the store-writing slice. 14+ extraction/contract/read tests |
| Structured subagent result projection (`subagent_result.py`) | done | one history read derives `artifact_ids` (successful Write/Edit files), `findings` (summary bullets outside an open-questions heading) and `open_questions` (that section's bullets); `proposed_memories`/`evidence` stay empty with reasons. Fills the contract from durable records with no agent-tool or prompt change; 10 builder/resolver tests + contract/event count tests |
| Subagent contract write path (`subagent_contract` flag) | done | project a run's structured result after its winning finalize CAS: resolved summary for completed (bounded; unavailable → record nothing, never fabricate), status+error summary otherwise; UPDATE-only ledger write via `record_subagent_contract`; `subagent.result` event with ids/status/lengths only. 21 dual-write tests + 4 call-site tests + 2 DB tests; flag-off regression unchanged. |
| Phase 3 shell: goal creation form (set-goal chip + goal-sheet form) | done | with no live goal the composer strip shows a low-key "Set goal" chip that opens the goal sheet's create mode; title/objective/stop-conditions (one per line) are contract-validated client-side before submit, 409 duplicate-live-goal and generic failures are stated, success invalidates the goal read so the sheet flips to the goal view; 30 goal strip/panel tests |
| Goal creation route (`POST /threads/{id}/goal`) + decision record 0008 | done | owner → goals flag → contract validation → one-live-goal 409 (ContractError/GoalTransitionConflictError mapped before ValueError; workspace read from the thread row); `goal.created` event with ids/counts only; 9 route tests. 0008 also resolves the charter's open decisions (one live goal, user-set creation, permission-mode policy, read-only review, propose-only memory) and specifies the review workflow before code |
| Permission modes (D3): per-thread `auto`/`manual`, enforced at build, recorded on the run contract, header control | done | flag `permissions` (default off); migration 093 `agent_thread_permissions` (mode CHECK manual/auto; `skip` rejected); `GET/PUT /threads/{id}/permission` owner-first + flag-shaped, `permission.mode_changed` audit; turn build freezes the mode into `StandingApprovalMiddleware` (`manual` = no standing approvals), finalize contract records the real mode; 4 DB + 8 service + 7 route + 4 middleware tests. Header `PermissionModeControl` (Auto / Always ask) reads the flag-shaped mode and toggles through the PUT route, updating the cache on success and refetching on a rejected write; hidden when disabled or on the new-chat placeholder; 9 web tests. |
| Phase 3 shell: review workflow (`review` panel module) | done | read-only per-turn change review per 0008 D4: aggregation (`reviewChanges`) takes the successful Write/Edit calls after the last user message, newest wire per path, Edit old/new or Write content, secret-classified paths listed with body withheld; `review` panel module (6th lazy module) opened from a desktop header toggle, expandable rows (aria-expanded), empty/no-diff/withheld states, open-in-file-panel; 16 web tests (helper + panel + registry) and the full 7804-test web suite pass |
| Phase 3 shell: memory inspector (`GET /api/v1/memory/index` + right-panel module) | done | flag-shaped read of the signed-in user's active projection (owner from the credential; list or ranked search, tier filter, limit bounds, ContractError→422); new `memory` panel module in the registry opened from a header toggle, read-only rows with tier/key/index line and unpointed marker, disabled/empty/loading states; 8 route tests + 14 web tests |
| Phase 3 shell: thread audit trail (`GET /threads/{id}/events` + goal-sheet section) | done | flag-shaped page (`enabled:false` hides it), ownership before read, paging by `after_event_id`; the goal sheet shows the latest 20 projected events with count-only summaries and fetches only while open. 7 route tests + 6 web tests; 2132 backend-app/refactor + 2682 web regressions pass |
| Canary headless-UI acceptance on M2 (goal strip, permission toggle, memory inspector, in-UI proposal approval) | done | Playwright/Chrome against the live canary: no console/page errors or 4xx; goal strip visible; permission PUT 200 `自动 → 总是询问`; memory inspector listed entries + empty proposals; a test proposal approved **through the UI** (POST 200, queue emptied, DB approved). Found and fixed three shell defects (missing `loadFilePanel` export, proposal type fields, optional callback) plus the proper `tsc -b` check. Electron window itself not clicked through. |
| Canary stages 1–3 on M2, all six flags on, live turns verified | done | model chain fixed to valid `deepseek-flash`/`mimo-v2.6-flash`; live turns wrote run contracts + events, mirrored a TodoWrite snapshot (`agent_tasks`), created/read a goal, persisted a per-thread permission mode, backfilled a seeded memory and injected it (`injected_memory` + `memory.injected`), produced a subagent contract with a proposed memory, and approved that proposal into the store (detail + `memory.md` pointer + projection + `memory.approved`) with a later turn injecting it. Six deployment findings fixed (OSS owner mapping, events INSERT list, status/backfill dict rows, todo/subagent owner, approval namespace). Electron/UI acceptance still open. |
| Phase 4: canary/rollback runbook + `scripts/refactor_status.py` | ready | staged order (ship dark → cheap dual writes → goal reads → permissions → memory last), per-stage verification commands, abort thresholds, rollback matrix per flag, schema-gate check (repo head 092 vs database revision) and projection row counts; 8 tests. Deployment itself remains a user action. |
| Live flag-on turn eval on M2 | blocked | no valid model credential (provider 401s); audit path is wired and waiting |


## Current phase status

1. **Phase 1** — done: contracts, additive migrations 086–092 (up/down exercised,
   down/up drill recorded above), SQL/service readers, dual writes for turns,
   permissions, task tree, memory store and subagent results. Flags default off.
2. **Phase 2** — done: five-tier budgeted selection, eval harness (baseline
   recall 1.00 / precision 0.95), render switch, injection audit, backfill job,
   CJK retrieval path verified on real Postgres (zh 0/4 → 4/4).
3. **Phase 3** — partially done: goal/plan/task views, plan decision, goal detail
   sheet, panel registry, session tabs, thread audit trail. Remaining: review
   workflow (diff review) and a memory inspector surface; goal creation and
   permission-mode policy are open product decisions.
4. **Phase 4** — tooling ready, execution pending: differential harness (F1–F3,
   one live capture), live retrieval probe, canary/rollback runbook + status tool,
   migration drill done. Deployment, M2 acceptance, live flag-on eval and
   Playwright F2/F3 capture remain (see the internal document `0007-acceptance` (not published) §4).
