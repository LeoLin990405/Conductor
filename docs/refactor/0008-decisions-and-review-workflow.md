# 0008 — Resolved decisions + review-workflow specification

- Status: **decided** (autonomous resolution of the charter's open decisions, with
  the review workflow specified before code, per gate 1)
- Supersedes/feeds: `0001-decisions` open list, `0007-acceptance.md` §4 residuals
- Reference inputs: reference-product study in `0002-reference-matrix.md`, current
  inventory in `0003-current-state.md`, schema in `0004-data-model.md`

The charter left five decisions open. This record resolves them, then specifies
the last unbuilt Phase-3 surface (review) so the next implementation slices start
from a contract instead of a guess.

## Decisions

### D1 — One live goal per thread

- **Decision:** a thread has at most one goal in `active`/`paused`; terminal goals
  (`completed`/`cancelled`) stay as history and a new goal can be created
  afterwards. This is already the schema (partial unique index from 086).
- **Why:** a thread is one conversation with one intent; parallel goals would fork
  the plan, budget and verifier the schema attaches to a goal. Reference products
  present one plan per task.
- **Consequences:** create-while-live is a 409 (never a silent replace);
  superseding starts with `transition_goal` to a terminal state; readers always
  return the live goal + latest plan.

### D2 — Goals are user-set for now; agent-initiated creation is deferred

- **Decision:** the shell exposes goal creation to the **thread owner**; the main
  agent does **not** create goals in this phase. Implemented backend now:
  `POST /api/v1/threads/{id}/goal` (owner → goals flag → contract validation →
  one-live-goal 409). The shell affordance follows.
- **Why:** an agent-facing creation tool needs an approval/ownership story and
  prompt/skill budget for a capability nobody has exercised yet; a user-set goal
  gives the goal/plan entities real data and keeps the agent surface unchanged.
  The agent can still *execute* a goal and propose plans.
- **Deferred:** agent tool `create_goal` plus a confirmation affordance; it must
  not bypass the one-live-goal invariant.
- **Consequences:** plan creation also stays service/route-level until the agent
  contract work lands; product can revisit D2 without schema change.

### D3 — Permission modes: `auto` (standing approvals) and `manual` (strict)

- **Inventory first.** Every interrupt in the runtime needs a human: `AskUserQuestion`
  asks a question; the credit gate and the sandbox/tool policy gates require a
  decision only the user can make; secretary commands mutate the user's
  workspaces/threads. This build has no external transaction approval path. The one
  approval path with an existing safe auto-answer is **standing approvals**
  (`StandingApprovalMiddleware`): the user opted in to hand-offs/workspace creation
  in preferences, and the agent-side middleware records which proposed calls that
  setting permits.
- **Decision:** two enforceable modes, per thread:
  - `auto` (default, and absence of a row): honour the user's standing approvals —
    exactly today's behaviour;
  - `manual`: ignore standing approvals; every approval card is shown.
  `skip`/blanket auto-approval is **rejected**: it would need an approval-capable
  read-only gate that does not exist, and suppressing the order/credit/secretary
  cards would be a security regression. `auto` is not "approve everything": it
  only honours the user's own pre-granted settings.
- **Why:** this is the one behavior change that is strictly safer-or-equal, needs no
  new security model, and unifies the policy over a check that already exists —
  the charter's goal for permissions.
- **Implementation (landed):** migration 093 stores the mode per thread (CHECK
  manual/auto); `permissions` flag (default off); `GET/PUT /threads/{id}/permission`
  (owner first, flag-shaped, `permission.mode_changed` audit); the turn build reads
  the mode once and freezes it into `StandingApprovalMiddleware`; the finalize
  contract records the real mode instead of a hard-coded `manual`.
- **Consequences:** the header `PermissionModeControl` exposes the toggle (Auto /
  Always ask) and writes through the route; the mode is read once per turn build,
  so a change applies to the next turn. Once a read-only approval gate exists, an
  `auto` extension can add it to the allowlist without changing the vocabulary.
  `manual` threads stay strict even if the user later grants standing approvals,
  until the thread says `auto`.

### D4 — Review is read-only change review, no accept/reject

- **Decision:** the review surface aggregates **what the agent changed**, per turn
  (and per task), with inline diffs and a link into the file panel. It never
  writes, reverts or "accepts" a change. Accept/reject and rollback are out of
  scope until sandbox snapshot/restore semantics are specified.
- **Why:** the agent already wrote through the sandbox; a UI toggle cannot undo a
  write without a restore contract, and a fake "reject" is worse than none. What
  users need today is a trustworthy account of the changes.
- **Data:** existing provenance/write-log records for a run (`file_operation`
  artifacts, write paths); before/after content from the persisted file tool
  results where captured. No new table.
- **Spec:** see the next section.

### D5 — Memory proposals: propose-only; approval waits for the emitter

- **Decision:** proposals are a **write-free channel**: when the memory flag is
  on, subagents get a `ProposeMemory` tool that records nothing; the server
  extracts valid calls from the task transcript after the run, puts them in the
  subagent contract (`proposed_memories`) and exposes them through a read-only
  route. Approve/reject is deferred: approval must write the **store** (source of
  truth) and let the projection follow via dual write, not flip a projection
  status alone.
- **Why:** approving a projection row without a store write would create a
  memory the agent never sees (or the reverse). Half-writing is a correctness bug;
  the tool needs no write access to make proposals reviewable.
- **Consequences:** extracted proposals land in the `agent_memory_proposals`
  queue (094; identity per run+type+hash, proposed/approved/rejected lifecycle)
  and `GET /api/v1/memory/proposals` lists the owner's `proposed` rows; the
  memory inspector shows them read-only. The decision route is implemented: approval
  writes the store (detail + index pointer) and projects it before the queue
  status moves; rejection is status-only. The inspector's
  approve/reject controls are in place (approval writes store + projection first;
  rejection is status-only), so D5 is end to end. `evidence` stays empty in the contract for
  the same no-invention reason.

## Review-workflow specification (D4)

### Scope

Per-turn (not whole-thread) change review for the current thread, read-only.

### Backend

- No new endpoint initially: the turn's write provenance already reaches the
  client (`provenance` events / write-log on the response, consumed by
  `useRightPanel`'s `collectWriteLog`). If aggregation is too slow client-side,
  add `GET /api/v1/threads/{id}/turns/{response_id}/changes` later with the same
  view shape (path, operation, size, before/after availability) — additive.
- Any new route must be flag-shaped (`enabled:false` hides the surface), owner
  checked before reads, and JSON-safe like the goal/audit routes.
- Redaction: never include file bodies for paths classified as secrets; a path
  may be listed while its content is withheld, and the view says so.

### UI

- A **`review` panel module** in the existing registry (desktop right column,
  same open/close pattern as `memory`), opened per turn from the settled turn's
  file row / "Review changes" affordance.
- Content: turn header (time, status), list of changed files grouped by operation
  (created/modified/deleted), each row expandable to the diff (reuse
  `EditDiff`/file preview primitives); deleted files show the removed lines when
  available; missing before/after is stated, never guessed.
- States: loading, no changes (states it), flag off (states it), per-file
  content-unavailable (reason), and a link that opens the file in the file panel.
- Accessibility: rows are buttons with `aria-expanded`; diff text keeps the
  existing color-independent markers from `EditDiff`; panel traps no focus and
  closes with Escape like other modules.

### Acceptance

- Unit: aggregation from provenance (order, dedupe, deleted/modified detection,
  redacted path withheld content).
- Component: render grouped rows, expand/collapse (aria-expanded), empty/flag-off
  states, link opens file panel; a11y test with the existing patterns.
- Differential: a turn that writes/edits/deletes files produces the same change
  set before and after the refactor (fixture F2 extension once a live capture is
  possible).
- No write path in tests: assert no mutation endpoint is called.

### Non-goals

Accept/reject, revert, stage-commit, PR creation, review comments/threads, and
multi-turn aggregated diffs. Those need the restore contract from D4's rationale.

## Implementation order after this record

1. ~~Shell goal-creation affordance for D2~~ — done: set-goal chip + goal-sheet create form (contract-validated, 409 stated).
2. ~~Review workflow per the spec above~~ — done: `reviewChanges` aggregation + `review` panel module (read-only, secret-withheld, no write path); see README row.
3. ~~Permission-mode persistence + enforcement for D3~~ — done (migration 093, API, build-time freeze, contract recording); shell selector next.
4. Structured subagent contract emission (unblocks D5 approval semantics).
5. Deployment/canary per `runbook-canary-rollback.md` when approved.
