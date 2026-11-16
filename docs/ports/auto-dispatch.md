# Auto-Dispatch: Conductor Superagent Orchestrator

## Overview

Auto-dispatch decomposes a high-level goal into work lines, assigns models via quota-aware selection with role constraints, monitors execution with log stall detection, triggers cross-review, and gates merge approval to humans before any push. v1 runs the lines of one dispatch serially, in topological order, one line at a time (the internal document `RELEASE-READINESS` (not published), Known v1 limits). Interfaces are protocols (not hardwired), enabling dev to proceed with fakes until impact-map/skills-hub/memory-p0 land.

Sources: `Hivemind/discussion*.py` (deliberation/rounds), `model-farm-handoff/codexbar_usage.py` + `dynamic_routing.py` (model selection), `mmteam-cc/build_headless_cmd` (CLI adapters), `memfw/approval` (human gate), `opentars/structured_cancel.go` (cancel propagation).

## Interfaces (Protocols)

### Line-worktree cleanup

Each dispatch line and author epoch uses a registered Git worktree rooted outside the repository. Terminal cleanup removes every owned line and integration worktree but keeps the branch refs. Dirty tracked changes and Git-reported, non-ignored untracked regular files are saved to a local recovery artifact before removal; final symlinks are recorded as links and are never followed. Git-ignored files and FIFO-only content do not trigger an archive and may be removed during terminal cleanup. If an archive cannot be completed, cleanup keeps the worktree and records a cleanup error.

**Planner** → `plan(goal: str) -> [Line(id, name, task, assigned_model: str | None)]`  
Decomposes the goal via Claude into lines. v1 runs those lines serially. Mocks for dev: fixed 3-line plans. A line's task may be at most 8,000 UTF-8 bytes and its name 256 (`MAX_LINE_TASK_BYTES`, `MAX_LINE_NAME_BYTES` in `planning.py`), and neither may hold text that is not valid UTF-8 (a lone surrogate) or a NUL byte (the launcher checks its own arguments again before it spawns, by the operating system's argument encoding): a plan with a longer or unencodable one is a plan failure ("refine the goal"), like malformed JSON, not a cut task, because a task is one argv string of the launch script (Linux does not start a process with a single argument of 131,072 bytes or more) and cutting it would change what the work lane is asked. The reviewer's prompt cuts a task at the same 8,000 bytes and a name at 256, so a line the planner accepts reaches every later stage whole.

**ModelSelector** → `select(line: Line, used_models: [str], role) -> str`  
Live choice comes from `RolePolicy` (`src/server/services/autonomy/roles.py`), or from `CONDUCTOR_ROLE_POLICY_FILE` when that YAML/JSON file is set. An unreadable or invalid file raises and does not fall back. Work order is `kimi`, `glm`, `grok`, `cursor`, `stepfun`, `deepseek`, then `minimax`, `doubao`. `claude` (planner) and `codex` (primary reviewer) are reserved and are not chosen for work unless `allow_reserved_code` is set, in which case the line stores `assignment_audit`. Aliases such as `claudedev`, `codexrev` and `step` are canonicalized before that check and before author exclusion. Review tries an available `codex` first, then a read-only secondary (`stepfun`). `grok` and `cursor` are not reviewers in v1. When none remain, selection returns None and `review_refusal` is `no_secondary_reviewer`. `kimi`, `deepseek`, `glm` and `minimax` stay ineligible to review; `REVIEW_INELIGIBLE` records why. `agy` is never eligible. `manus` is research only. Headroom is `default_headroom()` in `services/dispatch.py` unless a port is injected: the more exhausted of `CodexBarHeadroom` (the CodexBar snapshot file) and the native-runtime usage snapshot read by `load_usage_headroom`, which is `UnlimitedHeadroom` when it has no rows. The planner's DAG assignment and the selector share that port. Provider spellings (`zcode`, `zhipu`, `zai`, `dsh`, `claudedev`, `arkcli`, ...) are folded by `roles.canonical_provider` before any headroom read. A `large` line (`estimated_size`, or `risk_bearing`) ranks by headroom tier before the least-assigned spread. Success rate is `MemoryP0OutcomeStore.success_rate`, read back from the existing `dispatches.outcomes` column (no new table, newest 500 rows; a rollup past that is v2). A model with a recorded rate ranks ahead of a model with none. `DEV_MODEL_POOL` stays `[codex, claude, kimi, deepseek, glm, minimax, stepfun, doubao]` for the dev round-robin and the read-only adapter tests. `MODEL_POOL` is a deprecated alias of that tuple, not the live roster. Live work and review come from `RolePolicy`. `cursor` and `grok` are work-eligible through the policy and are not reviewers, because they have no read-only mode. In live mode a CLI the operator lists in `native_runtime_routing.disabled_providers` is not chosen to review. Dispatch names that are not native-runtime ids go through `PROVIDER_ALIASES` in `roles.py`: `claude` to the claude runtime id, `glm` to `zhipu`, `deepseek` to `deepseek-dsh`. `doubao` is the only name left on `DISPATCH_ONLY_REASONS` (a CLI adapter with no native-runtime provider). Manual `DispatchService.reassign` applies the same reserved-role rule as live work selection: `claude` and `codex` are refused unless `allow_reserved_code` is set, and that override stores `assignment_audit`. Mocks: round-robin over `DEV_MODEL_POOL`; the mock applies no role rules, and its reviewer runs no CLI.

**Launcher** → `launch(line: Line, model: str, skills: [str]) -> RunID`  
Invokes `~/conductor-wt/launch.sh model line.task --skills=skills_hub --impact-map=impact_map_path`. Returns run ID for monitoring. Before it starts anything it checks the argv (`_require_startable`): an argument with a NUL byte, text the operating system's argument encoding cannot encode (`os.fsencode`; a lone surrogate in the range U+DC80 to U+DCFF is encoded by it and passes, which is why the planner checks UTF-8 itself) or 131,072 bytes or more (Linux's `MAX_ARG_STRLEN`, 131,072 with the terminating NUL, so 131,071 bytes are accepted) is a `LaunchError` like a non-zero exit, so the machine retries and then reassigns the line, instead of an `OSError` or `ValueError` leaving the machine's launch loop from the spawn (the line has no run, and the dispatch stays in LAUNCHING); the message names the line and the argument's index, never the argument's text or the model (either can be the bad argument, and the message is logged and stored). The check is before the spawn on purpose: an error from the spawn itself cannot say whether the child ran (Python can create the process and then fail to register it), and the machine retries a launch failure, so converting one would run a line that already ran a second time (nine executions in a reviewer's reproduction of an earlier version that did). The limits this does not check stay what they were: the kernel's total argv-and-environment bound (the planner's 8,000-byte task keeps a planned line far below it) and a script that is not executable, which raise from the spawn as before. A failure after the child has run (writing its exit marker) propagates as the error it is. Checked at the byte boundary (131,071 accepted, 131,072 and four-byte characters over it refused), with a lone surrogate and a NUL byte (the runner counts its calls to show no spawn is attempted), with a 3 MiB task through `ScriptLauncher` and the real runner (nothing is created), and with a failing exit-marker write (propagates, one execution). Note that macOS accepts a single argument above this size, so the limit is a portable ceiling, not a statement about every kernel.

**Monitor** → `poll(run_id: RunID) -> Status(output: str, complete: bool, stalled: bool)`  
Watches log growth over 10s windows. Stall: no new bytes for 60s (timeout: 600s). Emits `(line_id, model, attempt, is_stalled, duration, token_count)`.

**Reviewer** → `review_line(line_id: str, model: str) -> Verdict(pass: bool, blockers: [str])`  
Cross-review via non-author model. No model reviews its own branch. Live: one read-only native-CLI turn (`NativeReviewBackend`, which builds its adapter only through `get_read_only_adapter`, see [NATIVE-CLI-AGENTS.md](../NATIVE-CLI-AGENTS.md)). The prompt carries the goal, the line's id, name, task and branch, and what the branch adds to the repository's HEAD (`branch_evidence`: `git diff HEAD...<branch>`), because three of the reviewers cannot open the repository. It does not carry the worker's output, and it says so. The evidence builder (`line_evidence`) has a slot for an output tail, an `Output tail:` heading with the tail under it, but `DispatchService` passes it no outputs and nothing reads `Line.log_tail`. With no output supplied (none, an empty or a blank one) the slot is one line instead of a bare heading, because a heading with nothing under it reads as "the worker printed nothing" or "nothing failed", and neither is known: `Worker output and test results are NOT part of this evidence; their absence here is not success and not silence. Do not infer that the worker printed nothing or that nothing failed.` That line is shorter than the slot's 2,000-byte cap. The slot is left unfilled because the tail is the last 500 characters of the worker CLI's raw output and `LogMonitor` redacts none of it (a credential-shaped string written to a log came through unchanged, checked), so filling the slot would send it to a second vendor's CLI. Filling it needs a redaction decision first. Only the prompt text is pinned (the live reviewer's prompt in `test_dispatch_review_backend.py`, the builder in `test_execution.py`); how a real reviewer CLI reads the line was not observed. The whole prompt is held to 98,304 UTF-8 bytes (`REVIEW_PROMPT_BUDGET_BYTES`, 96 KiB; bytes, not characters, which are up to four each), because StepFun, Claude and Doubao take it as one argv string and Linux does not start a process with a string of 131,072 bytes or more. Observed on Ubuntu 24.04 (aarch64): a 138,717-byte prompt, the 4,000-character four-byte goal with a 30,000-character diff of four-byte characters that the earlier character cap let through, failed to start (`Argument list too long`) and the 97,851-byte prompt this budget produces from the same input started. The diff gets what the rest leaves, cut on a character boundary with `[diff truncated: N bytes in all]` as the last line. The rest is bounded too, so the diff has room whatever the planner wrote: goal 8,000 bytes (so a goal the API accepts, at most 8,000 UTF-8 bytes, is never cut), task 8,000, line id and name 256 each, branch 512, and the output-tail slot 2,000 (its end kept; its heading and a tail at the cap are the largest the slot can be, 2,013 bytes, and the no-output line above is smaller than that, so the evidence is never larger than it is with the slot full (the line is longer than a very short tail's slot, so it is not the smallest); the diff takes whatever the slot leaves, so the budget bounds the whole prompt with output and without), each cut marked `[<part> truncated: N bytes in all]`. With the goal, task, line name and output-tail slot over their caps, a 50,000-byte diff still reaches the reviewer whole, with output and with none as live (tested through the builder; the line id and branch, which add at most 1,280 bytes more, are short in that test). 32 KiB under the 128 KiB string limit is left for the CLI's own flags and the environment, because the kernel also bounds argv and environment together, and under a small stack limit that bound is about 128 KiB (observed at a 256 KiB stack; 2 MiB at the usual 8 MiB). `CrossReviewer` refuses to ask a reviewer for a prompt over the budget, and `branch_evidence` fails the review if the other parts leave the diff under 1 KiB. An adapter that cannot be built or run is a transient review failure: the stage stays, `review_attempts` increases, and the third consecutive failure parks `review_unavailable`. A `DispatchError`, including a diff that cannot be read, still ends the dispatch in ERROR. Mocks: always-pass.

**Merger** → `merge_line(line_id: str) -> merged_branch`  
Merges line branch into `integrate/auto-dispatch-{dispatch_id}` (new per dispatch). Rebases on main if needed. Revert on conflict.

**ApprovalGate** → `request_approval(dispatch_id: str, lines: [Line]) -> approval_token`  
Stores pending dispatch in `proposed/auto-dispatch/{dispatch_id}/` with human readable summary. Returns token; blocks merge until human approval or rejection.

**OutcomeStore** → `record(dispatch_id: str, outcomes: {line_id → {model, duration, passed: bool, reason}})`  
Writes outcomes to `memory-p0` store (interface TBD with WP12). Tracks per-model success rate for future selection.

## State Machine

```
SUBMITTED → PLANNING → ASSIGNED → LAUNCHING → MONITORING → REVIEW → APPROVAL → MERGE → COMPLETE
    ↓         ↓(raise)  ↓(reassign) ↓(W8b)      ↓(failure)   ↓(fail)   ↓(reject) ↓(conflict)
  ERROR     REVIEW     ASSIGNED   recovery    REASSIGN    REASSIGN   ABORT      ABORT
```

- **SUBMITTED**: goal received, no lines yet.
- **PLANNING**: decomposing the goal. A planner that raises parks the dispatch in REVIEW as `planner_failed` (a fixed reason code, no lines). On the first plan, only an empty plan is ERROR (`The goal produced no work lines; refine it and retry`).
- **ASSIGNED**: all lines have a model assigned. ModelSelector failure (no eligible model) → wait 30s, retry once; fail → ERROR.
- **LAUNCHING**: all lines launched. A classified nonzero Worker exit follows the W8b recovery table, not a fixed two retries of the same model. Fix-round ownership is in WORKER-HEALTH.md §7.

| Failure class | Recovery |
|---|---|
| `rate_limited`, `overloaded`, `network`, `timeout` | Retry the same session up to 3 times. A `Retry-After` is capped at 600 seconds; otherwise the delay is bounded exponential backoff. |
| `context_exhausted` | Rotate the session up to 3 times. |
| `crash`, `unknown` | One fresh-session retry, then reassign. A fix round parks `fix_launch_failed` instead. |
| `auth_failed` | Park the line. |
| `quota_exhausted` | Reassign to a fallback worker. A fix round parks `author_quota_exhausted`. |
| `rate_limited`, `overloaded`, `network`, `timeout`, or `context_exhausted` after 3 tries | Park `<class>_retries_exhausted`. |
- **MONITORING**: watching logs. Stall (no bytes 60s) or timeout (600s) → REASSIGN; completion → REVIEW.
- **REVIEW**: cross-review each line. Blocker → send to user for decision, wait for manual reassign; pass → APPROVAL. An adapter failure retries (gated: on the drive, 1/5/15 minutes, falling back to the next eligible reviewer) and parks `review_unavailable` when the budget is spent; a reply that carries no verdict object is corrected once on the same model, then reviewed once by the next eligible reviewer, and parks `review_unparseable` without ever starting an author fix round; a `DispatchError` → ERROR.
- **APPROVAL**: request human gate. Rejection → ABORT. Approval → MERGE.
- **MERGE**: merge into integrate branch. Conflict → ABORT + notify. Success → COMPLETE.
- **COMPLETE**: outcomes recorded, integrate branch ready for push (human-triggered outside dispatch).

Reassign counter per line, max 2 (3 attempts total). All lines must reach REVIEW; if any abort/error, whole dispatch fails.

A dispatch parked in REVIEW, LAUNCHING, MONITORING or ASSIGNED can also be ended by a human: `cancel`, or `reject` outside APPROVAL, releases the lines and goes to ABORT without touching any branch. See "Operator ways out of a parked dispatch".

## Data Model

```yaml
Dispatch:
  id: str # UUID
  goal: str
  user_id: str
  created_at: timestamp
  lines: [Line]
  integrate_branch: str
  state: State
  approval_token: str | null
  outcomes: {line_id → Outcome} # populated at MERGE

Line:
  id: str # "line-0", "line-1", ...
  name: str # e.g. "implement_feature"
  task: str # e.g. "Write async file handler in src/core/fs.py"
  assigned_model: str | null # e.g. "codex"
  branch: str # feature/auto-dispatch-{dispatch_id}/line-{id}
  attempts: int # reassignment counter
  status: Status
  current_run: RunID | null

Outcome:
  line_id: str
  model: str
  duration_secs: int
  tokens: int
  passed: bool
  failure_reason: str | null
  reviewer_verdict: str
```

## Failure Modes & Recovery

| Mode | Symptom | Recovery |
|---|---|---|
| Plan decomposition fails | Claude refuses or returns invalid JSON | Emit error, ask user to refine goal |
| No eligible model (all exhausted/excluded) | ModelSelector returns None | Wait 30s, retry; fail after 1 retry → ERROR |
| Launch subprocess fails (path not found) | Launcher returns error | Mark run as failed, reassign to new model |
| Log stall / timeout | Monitor detects no bytes for 60s or >600s elapsed | Cancel run, reassign to new model (max 2 reassigns) |
| Reviewer cannot be built or run, or its diff cannot be read (no read-only mode, CLI disabled by the operator, missing or signed out, timeout, a branch git cannot diff, a prompt over its byte budget) | The reviewer call or the evidence raises | Dispatch ends in ERROR with a fixed message, `The reviewer <model> could not review line <id>`; the exception type is logged and its text is not, because it can carry what the CLI echoed of the prompt. Start a new dispatch |
| Review blocker (code quality, test fail) | Reviewer rejects | Emit blockers to user with line output; wait for manual reassign via API or auto-skip |
| Merge conflict | Git merge fails | Abort dispatch, emit conflicting files to user |
| Human rejects dispatch | ApprovalGate receives rejection | Move integrate branch aside, emit summary to user |
| Gated run exits 0 with no new commit, or leaves a dirty tree | Line BLOCKED `no_new_commit` / `uncommitted_changes` | `worker_exited` checkpoint and Stage-Gate `block` are written; `conductor dispatch reassign` continues on the same branch. Other ready lines keep running |
| Line parked mid-implementation before that checkpoint existed | Reassign answers `reassignment_not_at_checkpoint` | `conductor dispatch unblock`, then `reassign` |
| Reviewer scope or security conflict | Line BLOCKED `reviewer_conflict_scope` / `reviewer_conflict_security`, stage `awaiting_adjudication` | `conductor dispatch adjudicate ... in-scope\|out-of-scope` |
| Dispatch parked short of APPROVAL and not worth continuing | No line can move | `conductor dispatch cancel` (or `reject`): lines released, ABORT, branches kept |

Reassign loop prevents thrashing: once a line has used up its reassignments (`max_reassigns`, 2) the line is FAILED and the dispatch is set to ABORT with the line's id and reason (`machine._reassign`).

## Test Plan

**Unit (no external calls):**
- Planner: verify JSON parse, line count, model field presence (mock input fixtures)
- ModelSelector: role rules (no self-review, manus research-only, agy excluded), quota exhaustion, round-robin fallback
- Monitor: stall detection (no bytes >60s), timeout (>600s), completion on output
- Reviewer: pass/fail verdict shapes
- OutcomeStore: writes to fake memory-p0 store

**Integration (fake launchers, real state machine):**
- Full dispatch flow: plan → assign → launch (mocked) → monitor (mocked) → review (mocked) → approval (mocked) → merge (real git, tmp repo)
- Reassign path: simulate launch failure, verify new model picked, run completes
- Stall recovery: mock Monitor to detect stall, verify reassign triggered
- Conflict recovery: create conflicting branch, verify merge fails, abort emitted

**E2E (with live conductors, no push):**
- Real Launcher invokes `~/conductor-wt/launch.sh` on tmp goal
- Monitor hits real log file
- Outcomes written to test memory-p0 instance

**Coverage target:** unit 85%+, integration 70%+, e2e spot-checks on main features.

## CLI & API

**CLI:**
```bash
conductor dispatch start "<goal>"         # Start dispatch, return dispatch_id (an unknown verb is refused, N85)
conductor dispatch status <dispatch_id>   # Poll state, emit line statuses + current model
conductor dispatch approve <dispatch_id> [--note T]            # Human approves; triggers merge
conductor dispatch reject <dispatch_id> [--note T] [--force]   # At APPROVAL: rejects; parked: cancels
conductor dispatch cancel <dispatch_id> [--note T] [--force]   # Ends a parked dispatch; branches kept
conductor dispatch reassign <dispatch_id> <line_id> <model>    # Override model for next attempt
conductor dispatch unblock <dispatch_id> <line_id> [--note T]  # Move an old wedged line to a checkpoint
conductor dispatch adjudicate <dispatch_id> <line_id> in-scope|out-of-scope [--note T]
```

`--note` goes with approve, reject, cancel, unblock and adjudicate; `--force` only with reject and cancel. `conductor dispatch status` prints the command that moves a line on: `adjudicate` for a `reviewer_conflict_*` line, and `reassign` (with `unblock` first for a line parked before N40) for a `no_new_commit` / `uncommitted_changes` line.

**API (read-only, no authentication required for internal Conductor calls):**
```
GET  /api/v1/dispatch/{id}                         → { state, goal, lines, integrate_branch, gates }
GET  /api/v1/dispatch/{id}/lines                   → [Line]
POST /api/v1/dispatch/{id}/approve                 → { status, approval_token }
POST /api/v1/dispatch/{id}/reject    {note, force} → summary
POST /api/v1/dispatch/{id}/cancel    {note, force} → summary
POST /api/v1/dispatch/{id}/lines/{line}/reassign   {model}        → summary
POST /api/v1/dispatch/{id}/lines/{line}/unblock    {note}         → summary
POST /api/v1/dispatch/{id}/lines/{line}/adjudicate {ruling, note} → summary
GET  /api/v1/dispatch/status/active                → [{ id, goal, state, progress_pct }]
```

Every refusal from these writes is a 409 whose detail is the reason.

A dispatch parked on a planner failure never reached the gate, so rejecting it
ends the run instead of moving an integrate branch aside: the same
`POST /api/v1/dispatch/{id}/reject` puts the row in `ABORT`, with `cancelled`
beside the reason it parked with. The goal is held to the planner's byte
ceiling (`MAX_GOAL_BYTES`, 8,000 UTF-8 bytes) by the request model, so the API
refuses a goal the planner would refuse, and says which field it refused.

## Dashboard & Monitoring

**Agent-Fleet Dashboard Panel:**
- Active dispatches: list with goal, state, line count, ETA (computed from prior model runtimes)
- Line detail: task, assigned model, current status, log tail (last 500 chars)
- Reassignment counter: visual indicator if line has been reassigned
- Approval gate: pending approvals with summary + diff link to integrate branch
- Outcomes (after COMPLETE): per-line model performance, pass rate, duration distribution

**Status Indicator:**
- 🟢 In progress (LAUNCHING/MONITORING)
- 🟡 Awaiting action (APPROVAL, waiting for manual reassign)
- 🔴 Failed (ERROR, ABORT)
- ✅ Complete (MERGE)

## Rollback & Legacy
Implementation touches only: new files under `src/tools/auto_dispatch/`, CLI entry in `src/llms/native_cli/dispatcher.py`, new API router `src/server/app/dispatch.py`, new DB table `dispatches` (Alembic migration, reversible). Legacy sources remain read-only. Feature is opt-in via CLI; no changes to main agent flow without user invocation.

To rollback: `git revert` implementation commits, drop `dispatches` table (Alembic down), remove CLI entry, restart backend.

## Success Criteria

- ✅ All protocols mocked/stubbed → dev can write tests without impact-map/skills-hub/memory-p0
- ✅ Full state machine coverage in integration tests
- ✅ No network calls to real models or CodexBar during dev (mocks only)
- ✅ CLI and API match spec
- ✅ Dashboard renders active + pending dispatches
- ✅ Approval gate blocks merge until human action
- ✅ Outcomes recorded to memory-p0 store (faked in tests, real in staging)
## Ported implementation

Shipped on branch `feat/auto-dispatch` in six commits (`d6dc97154`..`06885e63c`,
plus `06d328fa0` for the head pins). Legacy sources were read-only throughout;
nothing was written back to them, and no live provider, GitHub, backend on
:8000, database or migration was touched.

### What came from where

| Source (read-only) | What was taken | Where it landed |
|---|---|---|
| Hivemind `lib/gateway/discussion*.py` | Rounds, park-and-resume, continue/cancel | `src/tools/auto_dispatch/machine.py` — the dispatch parks rather than blocks, and picks up from the persisted row when a human answers |
| model-farm-handoff `farmctl/workflow/codexbar_usage.py`, `dynamic_routing.py` | Quota headroom before assignment, deterministic ordering | `src/tools/auto_dispatch/selection.py` (`HeadroomPort`, `QuotaAwareModelSelector`) |
| mmteam-cc `build_headless_cmd` | The headless CLI seam | `src/tools/auto_dispatch/execution.py` (`ScriptLauncher.argv`) |
| memfw `core/repository.py` approval model | Approval-first writes under `proposed/` | `src/tools/auto_dispatch/approval.py` (`FileApprovalGate`) |
| opentars `taskruntime/structured_cancel.go` | Cancel propagation and reassignment budget | `machine.py` stall/timeout → cancel → reassign, `MAX_REASSIGNS = 2` |

### Files

- `src/tools/auto_dispatch/` — `types`, `protocols`, `machine`, `planning`,
  `selection`, `pool`, `execution`, `approval`, `outcomes`, `dev` (the
  deterministic doubles the design names for the window before impact-map,
  skills-hub and memory-p0 land).
- `migrations/versions/121_dispatch.py` — the `dispatches` table, chained after
  119 (the head on `integrate/r2`), reversible.
- `src/server/database/dispatch.py`, `src/server/services/dispatch.py`,
  `src/server/app/dispatch.py` (+ one line in `app/setup.py`).
- `src/conductor_cli/dispatch.py` (+ two lines in `cli.py`, the deploy gate in
  `operations.py`).

### Deviations from the design

- The API takes `CurrentUserId` like every other `/api/v1` route rather than
  running unauthenticated; ownership is answered 404, never 403.
- `POST /api/v1/dispatch` was added (the design lists only reads plus the three
  decisions) because the CLI's first form starts a dispatch.
- The dashboard panel is not built: this port is backend + CLI only, and `web/`
  is untouched.
- A review runs the reviewer's CLI read-only, so the review role does not use every pool model (see ModelSelector above), and a reviewer failure of any kind ends the dispatch in ERROR rather than escaping the machine. One failed reviewer ends the dispatch: another eligible model is not tried, and the stored sentence does not say whether the cause was a timeout, a sign-out or a refusal (the log has the exception type only).
- Outcomes write through a `MemoryPort`; `InMemoryMemoryPort` stands in until
  memory-p0 lands, exactly as the design's OutcomeStore interface allows.

### Bugs the tests found in the first cut

The machine tests could not have caught these: `_monitor` never ended for a
line that reported completion, so the whole file hung. Fixing that exposed five
more, all fixed in `d6dc97154`:

1. A completed line stayed RUNNING and was polled forever.
2. A blocked review parked the dispatch and then asked the gate anyway,
   overwriting the park (and requesting approval twice on the happy path).
3. A line that burned its reassignments failed while the launch loop carried on
   and the dispatch advanced into monitoring, review and the gate.
4. A merge conflict ended the dispatch in ERROR; the design puts it in ABORT.
5. `dispatch_summary` called `progress_pct`, which was never imported.

Two smaller ones followed: the gate wrote `gate.json` into its own root when a
dispatch was resolved before it had asked for approval, and the deploy blocker
printed a literal `<id>` instead of the dispatch id.

### Validation

No network, no real provider, no database — every collaborator is a fake and
the socket tripwire in `tests/unit/conftest.py` stays on. The original port run
used the harness interpreter at
`/Users/you/Documents/deepseek-harness/default-workspace/AlphaCN/.venv/bin/python`
because the worktree had no virtualenv of its own; the worktree now has one
(`.venv`, Python 3.14) and the re-run after the merge fix used it:

```sh
python -m pytest -q -p no:cacheprovider \
  tests/unit/tools/auto_dispatch \
  tests/unit/server/app/test_dispatch_api.py \
  tests/unit/server/services/test_dispatch.py \
  tests/unit/server/database/test_dispatch.py \
  tests/unit/migrations \
  tests/unit/conductor_cli \
  tests/unit/test_conductor_cli.py \
  tests/unit/test_conductor_cli_operations.py \
  tests/unit/server/test_import_layering.py
```

**1362 passed** (0 failed, 0 skipped), including the 65 orchestrator tests, the
17 service tests, the 15 API tests, the 8 row-mapping tests and the 6 migration
tests. `tests/unit/server/app` was run in full because `app/setup.py` changed.
`ruff check` passes on every file added or changed. Frontend tests were not
run: `web/` is untouched.

After the merge fix the same command (plus `tests/unit/scripts`, which holds
the other head pins) passes **755 tests**, and the whole unit suite passes
**16494 tests / 28 skipped** on `.venv/bin/python` — the merge brings in
autonomy, daemon, sandbox and memory-federation code this line does not touch,
so the full run is the evidence the merge itself is clean. `ruff check` passes
on every file added or changed by the merge fix.

### Merge fix: `integrate/r2` renumbered the migration to 121

`git merge integrate/r2` brought in the sa-channels migration, which had taken
revision `119` while this branch was open, so the two migrations both claimed
119 and `alembic upgrade head` would have refused to start. `120` is reserved
for the sa-router-shard line, so the dispatch migration moved to `121`:

- `migrations/versions/119_dispatch.py` → `121_dispatch.py`, `revision = "121"`,
  `down_revision = "120"` (the Router snapshot migration, after the shared 119 head).
  Still reversible: the downgrade drops the two indexes then the table.
- `tests/unit/migrations/test_119_dispatch.py` → `test_121_dispatch.py`, now
  asserting `("121", "120")`.
- The head pins moved with it: `test_refactor_migrations.py`
  (`get_heads() == ["121"]`, `121 → 120 → 119`), `test_refactor_status.py`
  (`repo_schema_head() == "121"`, the `--json` `schema_head`), and
  `test_119_channel_reply_approval.py`'s graph test now expects `121` past
  `119`.
- `tests/unit/migrations/test_118_deliberation_groups_continue.py` was the one
  conflicting file. Both sides were kept: this branch asserted the graph has
  exactly one head (`heads == {"119"}`), `integrate/r2` asserted 118 is no
  longer terminal. The merged test asserts `heads == {"121"}` and, separately,
  `"118" not in heads`.

The migration graph is linear after the merge: `119 → 120 → 121`.

### Rollback

`git revert` the six implementation commits, `alembic downgrade 119` to drop
`dispatches`, and restart the backend. The CLI verbs and the deploy gate go
with the commits; nothing else reads the table.

## Gated mode (W3a)

Dev (`CONDUCTOR_DISPATCH_ADAPTERS=dev`, the default) does not construct a `GateRunner`. `Dispatch.gates` stays `{}`, lines still launch together, and merge still happens after a human approves. Live does the same when `CONDUCTOR_DISPATCH_GATES=off`.

When live gates are on, `DispatchService._machine` injects `GateRunner` (`src/tools/auto_dispatch/gates.py`). The machine then:

1. Rejects a plan whose `depends_on` is missing a line or contains a cycle with `DispatchError`: `The plan's line dependencies are not a DAG`.
2. Launches a line only after every line it depends on is `PASSED`. Roots launch first. Ties keep planner order. A line the planner already gave an `assigned_model` is marked `ASSIGNED` and joins that order. It is not left `PENDING`, which would skip launch.
3. After a line's run completes, and before review, runs G2 in a detached git worktree at `<runs_dir>/gates/<dispatch>/_wt/<line>`. The log stays at `<runs_dir>/gates/<dispatch>/<line>/g2-0-<index>.log`, outside that worktree, so removing the worktree does not delete it.
4. Reviews lines whose G2 result is `pass` or `skipped` for this attempt and this commit. A fixable G2 failure (nonzero exit or `g2_timeout`) starts a fix round on the same author: `attempts` stays put, `assigned_model` stays put, and the launch task is a copy. G2 runs again even when the attempt and the commit still match (`invalidate_g2(..., force=True)`). `CONDUCTOR_DISPATCH_MAX_G2_FIXES` (default 2) counts those launches on `G2.fix_launches`. The failure after the cap blocks the line with `focused_tests_exhausted` and leaves the dispatch in `REVIEW`. `g2_no_commands_risk_bearing` and `g2_revision_moved` block immediately and do not start a fix. A handoff that is missing branch, commit, files, tests, or results blocks with `handoff_incomplete` and does not start a fix. Other lines whose dependencies are already `PASSED` still launch. The dispatch parks in `REVIEW` only when nothing else can launch. The G2 object stores `attempt`, `revision`, and `fix_launches`. `revision` is the detached worktree's HEAD, read after that worktree is created. The machine saves that attempt and that SHA before the first command of the round, including the rerun after a branch move. A pass or a skip is reused only when both still match. A `pending` or `running` result is not a completed result and is run again. A new attempt or a new commit runs G2 again. If the branch tip moves while the commands run, that result is discarded and G2 runs once more on a new worktree. A second move records `g2_revision_moved` against the SHA that was tested, and `allows_review` stays false. A failure is cleared on the next launch of that line even when the attempt and the commit are unchanged.
5. When every line is `PASSED`, merges them in dependency order into `integrate/auto-dispatch-<id>` (a conflict still aborts), then runs G5 in a detached worktree of that branch. Before `git worktree add`, a leftover `<runs_dir>/gates/<id>/_g5` that this repo already lists is reused only when its HEAD is that branch's commit and `git status --porcelain` is empty. That status includes tracked changes, untracked files and conflicts. A dirty leftover at that commit is removed and added again, so the suite runs the committed tree. If that rebuild cannot clear the path, G5 fails closed with `g5_worktree_dirty` and does not run the suite. A leftover at a different commit is removed and added again. A path that is not this repo's worktree is left untouched and G5 fails closed.
6. G5 pass moves the dispatch to `APPROVAL` and asks the human. The lines are already merged, so approve records outcomes and completes without merging again. That skip reads the saved integration phase, so it still applies if this process has no `GateRunner`. Reject still moves the branch aside and aborts.
7. G5 fail sets the dispatch to `ERROR`. The message is exactly `G5 integration checks failed on integrate/auto-dispatch-<id>: <check>` where `<check>` is `full_suite` or `alembic_single_head`.

`GET /api/v1/dispatch/{id}`, approve, reject and reassign all return `gates` through `dispatch_summary()`. Dev returns `{}`. The same summary includes `spec_ref` (empty when the dispatch has none). The gated object follows `tests/fixtures/dispatch_gates_v1.json`, plus optional keys this round adds and the shared fixture does not yet list: top-level `scheduler`; `G2.fix_launches`; and, only after a fix handoff is recorded, `lines.<id>.gates.handoff` (branch, commit, files, test command, results, blocker count). `machine.handoff` is always null. `history` rows are `round`, `gate`, `result`, `reviewer`, `blocker_count`, `reason`, `at`. Events keep the last 50 items and only the `GateEvent` field names. Timestamps are UTC ISO-8601 from the runner's clock. The snapshot stores exit codes, durations and log paths. It does not store prompts, diffs, model text, command output or blocker text. The approval Gates table columns are Line, G2, Primary, Secondary, G5. A non-risk primary pass is Stage-Gate verdict `merge` and does not open a secondary review. A risk-bearing primary pass is `approve`, then a secondary reviewer (not the author, not the primary) must `merge` before G5. Primary and secondary BLOCK use the same fix-round count: the first BLOCK is `blocking_rounds` 0 and stage `fixing`; the next BLOCK from that same reviewer is rounds 1 and still `fixing`; rounds of 2 or more go to `replanning` with `two_failed_rounds`. The machine does not recompute that count. APPROVE or MERGE clears it. A different reviewer starts the count over and the line snapshot reason is `reviewer_changed`. Blocker text containing "security" or "scope" parks at `awaiting_adjudication` (`reviewer_conflict_security` / `reviewer_conflict_scope`); the reviewer's findings are kept on `line.blockers` and in the snapshot's `adjudication` record (conflict, reviewer, findings), and `conductor dispatch adjudicate` is the way out. Any other disagreement is a fix round. `fixing` displays as its `return_stage`. After the fix, a G2-return fix reruns G2 and only then sends `fix_resubmitted` when G2 passed. A review-return fix goes back to that review. One replan calls `Planner.plan` with a fixed template. New ids are `<oldid>-r<n>-<k>`, `depends_on` is copied, the child starts `ASSIGNED` on the same model, and the original line is `FAILED` with `failure_reason` `replanned`. `CONDUCTOR_DISPATCH_MAX_REPLANS` (default 1) is copied onto the child, so the child cannot replan again. An empty plan, a planner error, or the cap blocks with `replan_exhausted` and the message `Line <id> was blocked twice after fixes and could not be replanned; reassign it or reject the dispatch`. Both work while the dispatch waits in `REVIEW`: reject there cancels it (see "Operator ways out of a parked dispatch"). G5 still ignores lines whose `failure_reason` is `replanned`. G6 stays human. Approve sends Stage-Gate G6 pass and does not merge again. Reject sends G6 fail, sets the branch aside, and aborts. It does not auto-replan. Human reassign still sends Stage-Gate `reassign` when the handoff is complete. Wiring the planner to an archived SPEC and to HeadroomPort stays out of this round until W4 has merged.

A reviewer that cannot be chosen because no read-only secondary remains ends the dispatch in `ERROR`. The line's `failure_reason` and the message both carry `no_secondary_reviewer`.

G2 commands are the acceptance items that start with `cmd:`. The rest is prose. The reviewer prompt, in gated mode only, appends that prose and the G2 command index plus exit code. It still fits the existing byte budget. There is no shell: `shlex.split` produces the argv, and the runner uses `create_subprocess_exec`. The command leads its own session. A timeout or a cancelled task sends TERM to that group, waits a bounded interval, then KILL, and the reap (including the stdout pipe) is time-bounded. A leader that has already exited, including exit 0, is not done while a descendant still holds the stdout pipe or the process group. That path uses the same stop. A cancel during the pipe wait takes that stop and then raises `CancelledError`. A child that inherited the pipe dies with the group, so the read cannot wait on it. Cancelling a worker launch snapshots descendants with `ps` while the leader is alive, sends TERM to the group, waits, then KILL to that group and to any descendant that left it (`setsid`). `ScriptLauncher.cancel(run_id)` is that stop for a launch that is still inside `launch`: the live runner records the process under the run id before it waits. The launch script keeps the worker in the launcher's process group, so the group kill reaches it. A cancel for a run that has already left `launch` finds nothing to stop.

G5 reads `migrations/versions/*.py` as text and finds `revision` / `down_revision`, including a tuple of parents. The head is a revision nobody names as a parent. One head passes. Zero heads is `alembic_no_head`. More than one is `alembic_multiple_heads`. A file that cannot be read or parsed (bad UTF-8, a syntax error, a read error) fails the single-head check with `alembic_unparseable`. A missing versions directory is still `alembic_no_head`. The parser does not import the files and does not open a database.

A line is risk-bearing when `line.risk_bearing` is set, or when any `write_scope` path matches `CONDUCTOR_DISPATCH_RISK_GLOBS`.

### Environment

| Variable | Default | Effect |
|---|---|---|
| `CONDUCTOR_DISPATCH_GATES` | `on` | `off` disables the runner in live mode. Dev never injects it. |
| `CONDUCTOR_DISPATCH_REPO` | server process cwd | Repository the live Claude Code planner runs in (`DispatchSettings.repo_root`). |
| `CONDUCTOR_DISPATCH_G2_COMMAND` | unset | Fallback argv when a line has no `cmd:` acceptance. Still unset: a normal line is `skipped` / `g2_no_commands` (treated as a pass); a risk-bearing line fails with `g2_no_commands_risk_bearing`. |
| `CONDUCTOR_DISPATCH_G2_TIMEOUT_SECONDS` | `900` | Focused-test timeout. The snapshot reason is `g2_timeout`. |
| `CONDUCTOR_DISPATCH_G5_COMMAND` | unset | Full-suite argv. Unset fails G5 with reason `g5_not_configured` and check name `full_suite`. |
| `CONDUCTOR_DISPATCH_G5_TIMEOUT_SECONDS` | `3600` | Full-suite timeout (`g5_timeout`, check name `full_suite`). |
| `CONDUCTOR_DISPATCH_REVIEW_TIMEOUT_SECONDS` | `900` | Timeout of one native review turn (`NativeReviewBackend`), for the primary, secondary and conflict reviewer alike. A turn that runs over fails `review_unavailable` / timeout; after three tries the line parks (N74). |
| `CONDUCTOR_DISPATCH_RISK_GLOBS` | `migrations/**,src/server/database/**,**/auth*,**/credential*,**/secret*,src/server/app/**` | Extra risk paths, comma-separated. |
| `CONDUCTOR_DISPATCH_MAX_G2_FIXES` | `2` | Fix launches after a fixable G2 failure. `0` blocks on the first one. |
| `CONDUCTOR_DISPATCH_MAX_REPLANS` | `1` | Replans of one original line. Children inherit the count. |
| `CONDUCTOR_DISPATCH_MAX_PARALLEL_LINES` | `4` | Independent lines of one live dispatch that may run at once (N31). `1` keeps the v1 serial invariant. Dev drills always run one line. |
| `CONDUCTOR_DISPATCH_PROVIDER_CAPACITY` | unset (every provider 1) | Concurrent worker launches per provider in this backend process, e.g. `grok=1,kimi=2`. The shell's `CONDUCTOR_DISPATCH_SERIAL_PROVIDERS` lock (default `glm`) still applies on top. |
| `CONDUCTOR_DISPATCH_QUOTA_HANDOFF_MODE` | `shadow` | M2c quota handoff pack: `off`, `shadow` (record only) or `enforce` (also in the new worker's prompt). See Quota handoff pack (M2c). |
| `CONDUCTOR_MEMFW_BIN` | `memfw` on PATH | The memfw executable the quota handoff runs; empty means not installed. |

### Parallel lines (N31)

`_run_gated` starts every ready line it may, up to `max_parallel_lines`, each in its own line task; the drive keeps converging (monitor, G2, review) and starts more as lines finish. Two lines never run together when they share a provider beyond its capacity (the launcher's `capacity_for`, default 1) or when their write scopes overlap (an empty scope may write anywhere). A dependency still waits until its parent passed. Fix rounds and deferred relaunches stay on the drive itself.

Exactly one coroutine changes the row at a time: the drive and each line task hold a writer lock and hand it over at every external wait (worker run, G2, review, sleep). Each line reserves its own run under the epoch fence, and the launcher re-checks the epoch right before the spawn, so two lines or two drives never share a reserved run. A line's reserved run, RUNNING status and Stage-Gate stage are on the row, so a restart in the middle resumes each line once. `ScriptLauncher` additionally holds a per-provider slot from just before the spawn until the worker exits, process-wide, so lines of different dispatches respect the same capacity. `dispatch status` shows `parallel: N of LIMIT lines active`.

### Provider skips (N25a)

A worker failure classified `rate_limited` or `quota_exhausted` (including zcode's `zcode_plan_quota_exhausted`) records `gates.provider_skips[provider] = {reason, until, source, at}`: until its `retry_after`, or a backoff of 5 minutes (rate limit) / 30 minutes (quota) when the reset is unknown. Until then the provider is left out of every new selection (developer and reviewer), and a line that has not run yet, is not a fix and was not picked by a human moves to the next eligible developer before its launch (`provider_skipped` audit entry with the replacement; none eligible keeps the model and its own retry policy). The line that hit the limit moves too (N25(a)), unless it is in a fix round: a fix stays with its author, so a rate limit or spent quota during a fix round retries the same provider after its backoff (or parks `author_quota_exhausted`), as before: when another developer is eligible (not skipped, never a holder of the line), its worker exit is recorded as a `worker_exited` checkpoint and it is reassigned through the same Stage-Gate path as a human reassign. It keeps its branch and its work; a dirty worktree is kept and its diff goes into the new model's first prompt, and the move is audited (`provider_skipped` with `moved: true`). Only when nobody else is eligible does it retry the same provider after the backoff. The live `QuotaAwareModelSelector` reads `used_models` only for review, so every skip reaches it through the hard `exclude=` set. `dispatch status` lists the active skips and the recent moves.

### Quota handoff pack (M2c)

When the line that moves off a provider (N25(a) above) moved because its worker reported `quota_exhausted` (not a rate limit, not a fix round), `src/tools/auto_dispatch/handoff_pack.py` builds a handoff pack of what the previous worker was doing, after the move succeeded and without changing who got the line: the replacement is still `_select_model`'s (or the N73 Super-agent's) pick, made before the pack exists. The pack is built after the replacement is chosen and before the line is touched, so the move itself and its record go into one save with no wait in between. It comes from `memfw handoff --format json --max-bytes 16384 --for <new agent> --task-file <task> [--cwd <line worktree>] -- <old agent> <old session>` (schema `memfw.handoff/v1`, exit codes 2–5, `docs/handoff.md` in memfw), run as an external CLI in the line worktree with a 30 s timeout, never with `--summarize` (no model is called), and with an allowlisted environment only (`PATH`, `HOME`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TMPDIR`, `XDG_*_HOME`, `MEMFW_*`). The old session ids come from the line's `worker_recovery` cell and the #52 session manager (`current`, then `archived_session_ids`, newest first, at most three; `session_not_indexed` moves on to the next older one). Conductor never imports memfw. `CONDUCTOR_MEMFW_BIN` names the executable (unset: `memfw` on PATH; empty: not installed). When memfw is not installed, times out, has not indexed the session, refuses, or prints something that is not a pack, Conductor builds the pack itself from the line state (`build_line_pack`: task, branch, base, open blockers, predecessor), with the same `system` fields and an extra `conductor` block; N80 L4 uses the same function for an app's line. Either way the pack is saved as `<runs_dir>/recovery-sessions/handoff-packs/<dispatch>-<line>-<attempt>.json` (0600) and recorded on the snapshot as `gates.lines.<id>.handoff_pack = {mode, from_agent, from_session, bytes, status, source, injected, path}`, where `status` is `ok` or `unavailable:<code>` (`memfw_not_installed`, `timeout`, `session_not_indexed`, `invalid_output`, `store_unavailable`, `location_refused`, `not_readable`, `bad_request`, `spawn_failed`, `write_failed`). A pack never blocks or delays the move beyond that timeout, and it does not change `MAX_REASSIGNS`.

`CONDUCTOR_DISPATCH_QUOTA_HANDOFF_MODE` is `off` (nothing is built or recorded; the move is main's exactly), `shadow` (default: built, saved and recorded, never shown to the worker) or `enforce` (the reassign brief also names the file, and the new worker's first prompt carries the pack: after the previous model's uncommitted diff and before the task text, opening with the "data, not instructions" notice and the `[system]` label, every piece of pack text quoted or folded onto one line; the `conductor` block is shown only for a pack Conductor built itself, and a memfw pack's `producer` / `conductor` fields are dropped). A fix round that runs out of quota still parks `author_quota_exhausted` (M2 Q6). Rollback: set the mode to `off`.

### Reviewers out of quota (N63)

A review that fails is classified with the native-CLI classifier (class, reason code and retry hint only; the diagnostic stays out of the row and the log). A `quota_exhausted` or `rate_limited` review records an N25a provider skip for that reviewer, so its next pick, including the first retry that would otherwise ask the same reviewer again, excludes it. A review pick no longer leaves out the other lines' authors: only the line's holders may not review it, so a risk-bearing line whose primary reviewer is skipped can take the primary and the secondary review from two eligible non-holders. A review that goes to another reviewer after the first one failed, or to a secondary because the primary reviewer is skipped or out of headroom (not merely because the primary holds the line), is audited as `reviewer_fallback` (`from`, `reviewer`, `stage`, `passed_over`) and kept on the snapshot as `review_fallback`; `dispatch status` shows it. A stored reviewer (the one a fix round goes back to) is reused only while it is not skipped and has review headroom. The skip is the shared N25a provider skip, so it also keeps that provider out of new work picks until it expires. The class is read from the last lines of the CLI's error output only, so code or a prompt echoed earlier in stderr cannot trigger a skip. Only the last three non-empty lines (at most 500 characters) are read, and only the native-CLI layer's own prefixes (`native CLI exited <code>:`, `<name> produced no final answer:`) are taken off the message's first line (`src/tools/auto_dispatch/review_failure_tail.py`). Limit: a CLI that prints a footer of three or more lines after its error line (a usage hint, a log path, a banner) pushes the error line out of that window, so the failure stays unclassified and counts as a generic review failure, not a quota skip. When no reviewer may take the line because each one is skipped or has no headroom, and the earliest skip is more than 15 minutes away, the line parks `reviewers_quota_exhausted` with a blocker naming each provider, the reason and its reset (`reviewers_quota_exhausted` on the snapshot). The reset shown is the later of the provider skip's expiry and the CodexBar plan window's reset, so a spent weekly plan shows its weekly reset rather than a 30-minute backoff. Only reviewers that could take the line count (on the review roster, not a holder, not the primary on a secondary pick): a developer-only provider's short skip does not postpone the park. `dispatch unblock` resumes the review (its audit records the real park reason). A skip that ends sooner is waited out. The ungated path parks the same way instead of ending in ERROR.

Headroom for review reads the CodexBar plan status too (`load_usage_plan_status`): a model whose weekly or session plan window is full counts as having no headroom for review even with paid credits left, unless `CONDUCTOR_DISPATCH_ALLOW_PAID_CREDITS` is set (1/true/yes/on). Work picks keep running on credits as before.

### Reason codes

W3a emits the codes in the first list. The second list is the rest of the `dispatch_gates_v1` set; a later round emits those when review, fix rounds and replanning are wired. Shared spellings live on `GateRunner`.

Emitted now: `planner_output_valid`, `focused_tests_passed`, `focused_tests_failed`, `focused_tests_exhausted`, `g2_no_commands`, `g2_no_commands_risk_bearing`, `g2_timeout`, `g2_revision_moved`, `g5_passed`, `g5_not_configured`, `g5_full_suite_failed`, `g5_timeout`, `g5_worktree_dirty`, `alembic_single_head`, `alembic_multiple_heads`, `alembic_no_head`, `alembic_unparseable`, `merge_conflict`, `awaiting_human`, `human_approved`, `human_rejected`, `no_secondary_reviewer`, `review_merged`, `review_blocked`, `reviewer_changed`, `two_failed_rounds`, `replanned`, `replan_exhausted`, `handoff_incomplete`, `fix_launch_failed`, `secondary_required`, `reviewer_conflict_scope`, `reviewer_conflict_security`, `primary_merge_no_risk`, `watchdog_timeout`, `stale_event`, `stale_result`, `stale_drive`, `stale_fix_intent`, `resume_spec_missing`, `review_unavailable`, `review_unparseable`. Wakeup logs use `wakeup_lease_held`, `wakeup_recover_failed`, and `wakeup_consumer_failed`. A failed recovery stores `wakeup_retry` as `{attempts, next_at}` on `dispatch.gates` and does not mark the dispatch `ERROR`. The scan and the recover call both skip the row while `now < next_at`. A later tick runs once that stamp is due. `HeadroomPort` and SPEC wiring stay deferred.

Phase names are the Stage-Gate set: `planning`, `spec_review`, `dag_ready`, `assigned`, `implementing`, `focused_tests`, `primary_review`, `fixing`, `secondary_review`, `integration_checks`, `awaiting_human`, `awaiting_adjudication`, `merged`, `replanning`, `blocked`, `cancelled`. A line in `fixing` shows `return_stage` (`focused_tests`, `primary_review`, or `secondary_review`) as its phase. No new `DispatchState` or `LineStatus` values were added.

On resume, a dispatch left in `LAUNCHING` or `MONITORING` with gates on continues the gated loop on a new machine, including one restored from the serialized store. A G2 result of `pending` or `running` is run again. A `pass` or `skipped` result is not run again only when its stored attempt and revision still match. A failed G2 is cleared when that line is launched again. Ungated `MONITORING` is still an error (`Dispatch <id> is already MONITORING`). A G5 snapshot whose phase is `merging` or `running` is run again. Lines already listed in `merged_lines` are not merged a second time. Approve does not merge again when the saved integration phase is `passed`.

### Reliable wakeup

Every gated wait has a durable source, not only the polling loop. `dispatch.gates.scheduler` holds `heartbeat_at`, `seq`, `threshold_seconds` (30 seconds), `deadline_at`, `deadline_reason`, `pending`, `handled`, and, after a timeout, `audit`. A launch deferred for an active sibling appends `{line_id, reason: serial_wait}` to `pending_relaunch` and clears that entry when the launch proceeds. Every fix stores `pending_fix` on the gates root before the launch is reserved or deferred, including a direct fix with no running sibling. The save that reserves the run keeps that entry and records `reserved_run_id`. The entry is cleared only after LaunchDone confirms the run started, or when the line is parked. The machine writes a heartbeat on each gated turn. `dispatch_summary()` copies that object through `scheduler_view`, which sets `healthy` from the age of `heartbeat_at` against `threshold_seconds`. An old or missing heartbeat is unhealthy. `cond status` and `GET /api/v1/dispatch/{id}` both render this summary.

The monitor arms `deadline_at` once, at `watchdog_seconds` from the clock (default 900), and saves it before the wait. The wait races `poll()` with that deadline, so a stuck poll still times out. During the wait the machine persists `heartbeat_at` every `heartbeat_interval_seconds` (default 30) when it is due. After a poll returns, the next sleep is `min(poll_interval, deadline - now, heartbeat_interval)` and is never negative. A deadline that has already been reached ends the wait without sleeping the rest of the poll interval. A later `beat` writes `heartbeat_at` and `last_seen` and does not move the deadline. Expiry of an armed deadline is `now > deadline_at` (`GateRunner.watch_expired`): `now == deadline_at` is not expired. The post-poll path treats remaining `<= 0` (`now >= deadline_at`) as already due, so a poll that lands exactly on the deadline does not sleep the rest of the interval. A watchdog of zero fires at once and does not sleep. A parent cancel during poll cleanup is shielded until that cleanup finishes, then the cancel is raised again. The line becomes `BLOCKED` with `watchdog_timeout`, the dispatch stays `REVIEW`, and `scheduler.audit` gains `{kind, line_id, state, at}`. The deadline is cleared when the wait ends or the watchdog fires, so the next wait can arm once.

A completion that arrives while the monitor is down is `push_wakeup(dispatch, event_id, "run_complete", line_id, run_id=..., attempt=...)`. The same id a second time is a no-op, whether it is still pending or already handled. The machine writes `{line_id, run_id, attempt}` into `scheduler.settled_lines` and saves that fact before it moves the id to `handled`. A mismatched triple, or a missing `run_id` while `current_run` is set, is ignored: the only record is an audit `{kind: stale_event}`, and the id is still acked. A new machine treats a `RUNNING` line whose triple matches, or whose line id is still stored as a legacy string, as complete and does not poll the old monitor. The line snapshot itself does not gain a key; the shared fixture still matches. A `running` G2 result is not reusable and is run again. `pull_wakeups` still takes each pending id once (the handled list keeps the last 200).

`DispatchService.open` returns as soon as the consumer task is ready. Recovery then runs in that task, four dispatches at a time, with no outer timeout. G2 (default 900s), G5 (default 3600s) and the watchdog are the deadlines. A recovery error writes `wakeup_retry` and leaves the dispatch state untouched. Every tick reconciles active rows whose retry stamp is due, not only rows that still have a pending wakeup. The fair scan asks for 1000 rows and widens by 1000 until the page stops growing or the page reaches 100000. A full page at that ceiling is not widened again, so a row outside it is not recovered. `close` cancels the consumer and the in-flight recovery tasks and waits until those tasks, including the poll and deadline children, have finished. A drive's long work (launch, poll, G2, G5, review) runs outside the per-dispatch write lock. The save is `apply_mutation`: re-read the row, copy only the fields this drive changed since its paired baseline, and write that row. After that save, baseline and local both become the saved row. `wakeup_retry` stays the fresh value. `scheduler.pending` and `scheduler.handled` keep concurrent items; the drive can add or remove its own ids. The status of a line the drive did not change stays on the fresh row. `reassign`, `approve` and `reject` use the same helper, so a reassignment of another line is still there after the drive saves. v1 is a single instance: a database store holds `pg_try_advisory_lock(870413)` on its existing connection, and anything else uses an in-process lease. A process that does not get the lease records `wakeup_lease_held` and does not recover. The same process keeps one drive body per dispatch id, and a second drive waits until that body finishes so it does not launch the line twice. `apply_mutation` holds the write lock from the re-read through a shielded `store.save`. The save runs on its own task, kept on the service, and a loop of `asyncio.shield` waits for it. Cancelling the caller, including a second cancel, does not cancel that save; the lock stays until the save finishes. The caller gets the persisted row only after that save returns, and only then may it resume a drive. A failed save raises `persist_failed` and does not pretend the write landed. `reassign`, `approve`, `reject`, and a replan each increment `gates.epoch` in that same mutation. A drive remembers the epoch it started with. Every drive write goes through `drive_mutation`: under the same lock it re-reads the row, and when `gates.epoch` is not that epoch it records only `{kind: stale_drive}`, skips the drive's write, and stops. A replan checks that epoch and commits the DAG, the child lines and the epoch bump in that one mutation, then keeps only the row the mutation returns. It does not increment the epoch on the local object first. The next wakeup starts a drive from the persisted row. Merge is claimed in a mutation before the external call. One `DispatchCoordinator` per process holds the write locks, the body locks, the `active_drives` registry, and the `WakeupLease`. The app lifespan creates it, and the HTTP routes, the scheduled drive, and wakeup all use that instance. A drive stores an owner token in the registry only after it holds the body lock, and deletes that entry on the way out only when the entry is still its token. A task waiting on the lock does not register and cannot clear the owner. While the token is there, `approve` and a `reject` at APPROVAL return 409 `drive_active`, write nothing, and schedule nothing. `reassign`, `unblock` and `adjudicate` of a parked line are queued for the drive instead, and `cancel` (or `reject` short of APPROVAL) refuses `drive_active` unless `--force`; see "Operator ways out of a parked dispatch". The drive drops the token when the machine reaches a human wait (G6 approval, a parked review, or BLOCKED waiting for reassign) and drops itself from the map. The same API call then commits and wakes a new drive. Two overlapping calls leave one winner: the winner reserves the map entry before it releases the write lock. `reassign` does not land while launch, poll, G2, G5, or review is still running. A line that is already `RUNNING` with `current_run` set is not launched again. The app lifespan calls `start_wakeup` / `stop_wakeup`. Recovery does not restart the API, the database, or any other process. Events stay in the existing `gates.scheduler` object. There is no new table and no change to `list_dispatches`. A scheduled drive that reserves an owner token registers a done callback on that task. `DispatchCoordinator.release_if_owner` drops the reservation when the task finishes, fails, or is cancelled before its first step, and only when the registry still holds that same token. `stop_wakeup` clears the process service. The next `start_wakeup` in the same process builds a service on the coordinator that is bound then. v1 runs the lines of one dispatch serially, in topological order, one line at a time. In-dispatch concurrency is 1. A recovered line that is `RUNNING`, or whose stage is `implementing`, is monitored, passed through G2, and reviewed before the next ready line launches. `_launch_guarded` checks that first. If any other line is still active it does not launch: the line is marked `pending_relaunch` with reason `serial_wait` and the call returns. A line blocked `no_new_commit` or `uncommitted_changes` has left `implementing` for Stage-Gate `blocked` (N40), so it is not active: the other ready lines, and the `serial_wait` lines, keep launching, and the drive parks only once nothing else can move. A fix that cannot be launched on resume does not end the drive either. Every fix goes through `_begin_fix` before `_launch_guarded`, whether it is a review fix or a G2 fix and whether or not a sibling defers it. That call writes `gates.pending_fix[line_id]` first: `kind` (`review` or `g2`), `author`, `attempt`, `epoch`, `source_ref`, `policy` `fix`, and `fix_round`, with no task text. `epoch` is the dispatch epoch at that write. The defer branch and the direct branch both read that entry and do not build a second one. A deferred fix is stored in the same save as `pending_relaunch`. A direct fix is already stored when the reservation save adds `reserved_run_id`. A later drive resumes any line that still has `pending_fix`, even when `pending_relaunch` is empty, through `_launch_fix`. That rebuilds the task with `_fix_task` from the persisted handoff view for that `source_ref` and does not increment `fix_round`. Author and attempt stay. A `reserved_run_id` whose run is not confirmed started is launched again. A run LaunchDone already confirmed only clears the entry. The entry is cleared in that confirmation save, or in the save that parks the line. A resumed fix that fails stays in `REVIEW` with `fix_launch_failed` and is not reassigned. While `pending_fix` is still on a line, `DispatchService.reassign` returns 409 `fix_pending` and writes nothing: author, attempt, epoch, the intent, and the schedule stay as they were. The check re-reads the row inside the reassign mutation and runs before Stage-Gate `advance`. The caller waits until LaunchDone confirms that fix or the line is parked. Resume compares the intent's `author`, `attempt`, `epoch`, and `source_ref` with the current line. Any mismatch clears the intent in that same save, appends `{kind: stale_fix_intent}` on the scheduler audit, and leaves the current assignment in place. The line continues from that current state. A stale intent does not restore its author or attempt. Each drive turn, after active lines converge, launches those deferred lines in topological order before any new ready line. A snapshot that already has two or more `RUNNING` lines is converged, not failed. A poll worker that sees a stall or a timeout enqueues `ReassignNeeded` and does not launch; the drive reassigns and then launches through that same check. Another dispatch may run at the same time. DAG edges still decide which line is ready. Parallel lines inside one dispatch are v1.1. The drive coroutine is the only task that writes machine state: the dispatch row, its lines, nested `gates` (including `beat`, `arm_watch`, `clear_watch`, and the G2 and G5 snapshots), `message`, and `approval_token`. Poll, heartbeat, watchdog, selector, retry, review, G2, G5, and the launcher are workers. They enqueue an immutable event or return an immutable result, and the drive applies it. `PlanReady` and `Assigned` install the plan and the chosen models. `ReviewResult` is constructed from the reviewer's verdict and applied on that same drive task. `PollResult`, `Heartbeat`, `LaunchDone`, and `Deadline` carry `(dispatch_id, line_id, run_id, attempt)` plus a monotonic `seq` taken when the event is queued. The drive applies one queued event synchronously, saves through `drive_mutation`, and only then takes the next. There is no `_state_lock` and no second writer task. A heartbeat save still cannot adopt over a completion that was applied before that save: the completion stays applied, and a `Deadline` queued behind it is discarded. A `Deadline` is discarded only when a completion for the same run has a smaller `seq`, or that run has already completed. A completion queued later does not win: the line stays `BLOCKED` with `watchdog_timeout`, and the completion is recorded only as `{kind: stale_result}`. `close` joins the in-flight save before the drive owner is released. The run id is `{dispatch_id}:{line_id}:{attempt}:{12 hex chars}`. It is persisted after the epoch check and before `launcher.launch`, and a new machine does not reuse it. An event that does not match the current line is discarded as `stale_result`. Logs for a wakeup failure contain the exception type name and a stable reason code, not the message and not a traceback.

Every launch, including a fix launch after review or G2, goes through `_launch_guarded`. That method is the only call to `launcher.launch`. Its first step is the serial check above. When no other line is active, it saves the run id and the attempt through `drive_mutation`. The worker starts only after that save returns. `force_g2` skips a fresh G2 result, not this barrier. A stale epoch raises `StaleDrive` and does not start the worker. Before that guarded launch, a gated line in `dag_ready` or `assigned` advances through `assign` and `start_implementation` and that snapshot is saved. While the worker is still running the phase is `implementing`. When the worker finishes, the machine saves `submit_work`. A fix round is already in `fixing` and is not assigned again. A fix launch that fails `launch_retries + 1` times parks the line `BLOCKED` with `fix_launch_failed` and the dispatch in `REVIEW`. The author, `attempts`, the stored task and contributors stay put, and the author is not asked to review that submission. An ordinary launch that falls back to another worker rewrites `author_provider`, `developer_provider`, and `contributors` to that worker in the same save, before the new launch. The reviewer exclusion then uses that worker. Resume of `ASSIGNED` uses the same path as `LAUNCHING`. Resume of `PLANNING` calls `planner.plan(spec_ref)`. An empty `spec_ref`, an empty plan, or a planner failure parks with `resume_spec_missing` instead of `ERROR`.

A reviewer adapter exception (timeout, launch failure, non-zero exit, I/O) does not mark the dispatch `ERROR` on either the gated path or the nongated/dev path. `DispatchError` is still the path to `ERROR`.

- Gated (N42): the drive retries the review itself instead of waiting for a wakeup, which never came when nothing else in the dispatch was active (R8c line-0-r1-2 and F1 line-0 sat RUNNING with no worker for 90+ minutes after a 30-minute codex outage). A failure is not slept on inline: it is recorded on the line snapshot as `review_retry` (attempt, the reviewer, the reviewers that failed, `retry_at` in wall time, after 60, 300 and 900 seconds) and `review_attempts` increases. Until `retry_at` the line is skipped by review and does not hold the serial slot (its run has finished), so every other line keeps launching, running and being reviewed; only when nothing else can move does the drive sleep until the earliest `retry_at`. The record survives a restart, so a new drive waits only for what is left. Before a due retry is sent the drive re-checks that it still owns the row (epoch). The first retry asks the same reviewer; from the second failure on the next eligible reviewer is asked: never the author, a holder of the line, a reviewer that already failed this round, or (secondary review) the primary; when none is eligible the pick is asked again. A changed reviewer restarts the Stage-Gate round count (`reviewer_changed`). A fourth failure parks the line `BLOCKED` `review_unavailable` with a blocker naming the reviewers tried, Stage-Gate `block` from the review stage, and a `review_parked` record (run, attempt, author, stage). `conductor dispatch unblock <id> <line>` resumes it: Stage-Gate `resume` back to the review stage (only if Stage-Gate accepts it), the line RUNNING on its finished run with a fresh budget, and the drive retries the review. `dispatch reassign` is also accepted from that pause. A reviewer that is unavailable on the one extra review after an unparseable reply still waits for the next wakeup.
- Ungated/dev: the stage stays put, `review_attempts` increases by one, and the next wakeup retries. The third consecutive failure parks the line `BLOCKED` with `review_unavailable` and the dispatch in `REVIEW`.

A reply that carries no usable verdict object is the reviewer's format, not the author's work, so it is never a fix round. The verdict object must be the last non-whitespace content of the reply: prose before it is fine (review prose quotes code with braces), and anything after it — a sentence, a stray code fence — makes the whole reply unusable (one closing fence that pairs with a fence opened right before the object, as in ` ```json … ``` `, is allowed), as do conflicting top-level `passed` values, which an author could plant in the diff for the reviewer to quote. `CrossReviewer` corrects the same model once. A reply that is still unusable is then reviewed once by the next eligible reviewer, barred from the author and from the reviewer that just failed (a secondary review also bars the primary reviewer), and a usable verdict from that review is applied and credited to it. When that reviewer changes on a gated round the review round count starts again, as the Stage-Gate does for any new provider (the snapshot reason reads `reviewer_changed` only until the verdict is applied; the lasting record is the history round and which reviewer got the credit). A second unusable reply, or no other eligible reviewer at all, parks the line `BLOCKED` with `review_unparseable` (never `ERROR`), keeps the parse failure text in the line's blockers, and leaves the dispatch in `REVIEW` for a human to reassign. No `pending_fix`, no handoff, no author launch. The second review leaves the actor, so its result is committed or parked only while the line still matches the `(line_id, run_id, attempt, author)` taken before the first review; otherwise it is `stale_result`.

A replan writes the new lines on the row `drive_mutation` returns and, in that same save, replaces the parent id in every other line's `depends_on` with all of the child ids and increments `gates.epoch`. `DispatchService.reassign` raises `replan_exhausted` before it changes the line, saves, or schedules a launch. A line that still has `pending_fix` is refused with `fix_pending` before Stage-Gate `advance`, and that refusal writes nothing. Every other gated reassign calls Stage-Gate `advance` before any field write and returns that transition's reason unchanged. `implementing` refuses with `reassignment_not_at_checkpoint`, unless a `worker_exited` checkpoint stands for the current author and attempt (Stage-Gate section 7). A legal checkpoint with missing handoff fields refuses with `handoff_incomplete`. A refusal does not save and does not start a drive. An acceptance commits the Stage-Gate transition, the line fields, and `epoch += 1` in one mutation, and only then resumes a drive. An async result (poll completion, review, G2, or G5) is committed only when its `(line_id, run_id, attempt, author)` still matches the current line. A mismatch discards the result, records only `{kind: stale_result}` on the scheduler audit, and does not credit an outcome to the old author. A later save that only updates that line's gate snapshot is the same mismatch: the snapshot stays as it was on the current line. `HeadroomPort` and SPEC wiring stay deferred.

## Operator ways out of a parked dispatch

When the drive cannot move a line on its own, the line parks and a human command moves it on. The decisions live in `src/tools/auto_dispatch/operator.py` as pure row writes; `DispatchService` applies them to a parked row, and a running drive applies the same write from its queue. A refusal is a 409 whose detail is the reason, and it writes nothing.

### A human reassign sticks (N86)

A reassign the service accepts writes `gates.human_pins[line_id] = {model, at, queued, attempts_at_request}` (`src/tools/auto_dispatch/human_pin.py`; a service-owned gates key, so a drive save or a Stage-Gate root reset keeps it). A reassign queued for a running drive is `queued` and counts only once the drive's audit shows it applied. For a pinned line the work selector returns the pinned model, or nobody when that model is excluded (a skipped provider, the model a move is leaving), so the move off a limited provider (`_move_off_limited_provider`, the path that put N80 line-0 back on deepseek after `reassign line-0 kimi`), a liveness reassign and the generic reselect cannot seat another worker: the same-provider retry policy runs instead. The reassign itself is refused with `worker_skipped` when this dispatch skips the chosen provider and its reset is not reached, and with `worker_no_headroom` when the quota reader reads nothing left. `pinned_worker_unusable` (HUMAN) is the park for a pinned worker that cannot run. `CONDUCTOR_DISPATCH_HUMAN_PIN_MODE` is `off`, `shadow` (default: the pin is recorded, refusals and kept picks are only logged and audited as `human_pin_shadow_refusal`) or `enforce`.

### Re-review a branch the operator fixed (N87)

`conductor dispatch recover <id> <line> --rereview` (API mode `rereview`) is for a line parked `replan_exhausted` (or another replan park) after the operator fixed its branch by hand; `unblock` answers `line_not_wedged` for it. `operator.apply_rereview` walks Stage-Gate to G2 through legal events only (`resume` from a pause; `replan_decision replace_developer` with provider `operator` and a complete handoff, only when that option was offered; `start_implementation`; `submit_work`), resets the line's G2 cell to `pending` with a fresh fix budget, and leaves the line `RUNNING` on a settled synthetic run (`<id>:<line>:<attempt>:rereview-<hex>`). The drive adopts that run as finished, runs G2 on the branch head and then the review; no Worker is launched. A fix round after a BLOCK goes to the line's author as usual. Refusals: `not_gated`, `already_split`, `line_not_parked`, `run_out`, `fix_pending`, `recover_pending`, `no_branch`, `rereview_not_offered` (the replan offered only a re-split) and `rereview_not_reachable:<stage>`. The episode's liveness record is closed as recovered, and `operator_rereview` is audited.

### Reassign after the worker exited (N40)

A gated run that ends with exit 0 and no new commit (`no_new_commit`), or with a dirty tree (`uncommitted_changes`), means the worker has exited. When no run of the line is out, `record_worker_exited` writes the same `worker_exited` checkpoint worker recovery writes (N38b): author, attempt, run id, failure class and exit code, plus a matching last `worker_recovery` event. It then sends Stage-Gate `block` (`implementing` -> `blocked`, `blocked_from=implementing`).

`conductor dispatch reassign <id> <line> <model>` then passes `option="worker_exited"` and is accepted. The new model continues on the same branch, and its first prompt carries the previous blockers. For `uncommitted_changes` the worktree is never reset: the reassign sets `allow_dirty_worktree` for the relaunch, and `ScriptLauncher.uncommitted_diff(line)` adds the dirty tree to the relaunch prompt as context. That read is bounded (4,000 bytes in the prompt) and read-only git: porcelain status, the tracked diff against `HEAD`, and the names of untracked files, never their contents. The text is passed through `redact_credentials` as a whole before it is cut, and a binary file keeps only its `diff --git` header. Git output cannot be capped while it is read, so the diff is read only when its size is bounded first: for each changed path its HEAD blob size (`ls-tree -r -l -z HEAD`, added to both read-only git allowlists) plus its size on disk, doubled for base85, must stay within 256 KiB; otherwise, and for renames or quoted paths, only the names are given. A tree it cannot read is left out and the relaunch goes on.

The line has left `implementing`, so it no longer holds the serial slot. Other independent ready lines, and lines in `serial_wait`, keep running. The drive parks only once nothing else can move.

### Unblock an old wedged line

Legacy parks (from before N40/N42) carry none of the new markers, and unblock accepts them too:

- `review_unavailable` parked by the old code: the line is BLOCKED (or FAILED) with no `current_run` and no live launch, and Stage-Gate is still at `primary_review` / `secondary_review` (or `blocked` from one), with no `review_parked` / `review_retry` record. Unblock resumes the review on the run the scheduler recorded as settled for the line's current attempt, with a fresh retry budget, through the same path as a new-style `review_parked` line (Stage-Gate `resume` only when it is blocked, and only if Stage-Gate accepts it). Without such a run it answers `no_settled_run`.
- `no_new_commit` / `uncommitted_changes` parked in a stage other than `implementing` (a fix round that made no commit waits in `fixing`): unblock writes the same checkpoint and the Stage-Gate pause. `dispatch reassign` was already accepted from `fixing`; this is why `unblock` answered `line_not_wedged` for such a line before.
- A line that already has a valid `worker_exited` checkpoint answers `already_at_checkpoint`: reassign it.

A line parked before N40 (or by worker recovery before N38b) has no checkpoint, and reassign answers `reassignment_not_at_checkpoint`. `conductor dispatch unblock <id> <line> [--note T]` (`POST /api/v1/dispatch/{id}/lines/{line}/unblock`) moves it to one. It applies when:

- the line's stage is `implementing`, or `blocked` from `implementing` with no valid checkpoint;
- the line is BLOCKED or FAILED;
- it has no `current_run` and no live launch in this process (`execution.live_runs_for_line`);
- it has no pending fix.

It writes the checkpoint with failure class `operator_unblock` (the note is kept), the `block` transition, and a scheduler audit entry `operator_unblock`. The line stays parked; `reassign` is then accepted. Refusals: `not_gated`, `line_not_parked`, `run_out`, `fix_pending`, `line_not_wedged`, `request_pending`, and `drive_active` (only while a human approve or reject holds the slot). On a parked dispatch the write is applied, the epoch is bumped, and the drive resumes, so the other ready lines move.

### Adjudicate a reviewer conflict (N41)

A review conflict stores its finding on the line (`line.blockers`) and in the snapshot's `adjudication` record (conflict, reviewer, findings, other_blockers). `conductor dispatch adjudicate <id> <line> in-scope|out-of-scope [--note T]` (`POST /api/v1/dispatch/{id}/lines/{line}/adjudicate` with `{ruling, note}`) sends Stage-Gate `adjudicate_conflict`:

- **in-scope** is `maintain_block`. Stage-Gate goes to `fixing`: the line becomes ASSIGNED, its blockers are the findings plus the ruling note, and a brief for the same author and attempt makes the relaunch prompt the fix task. The drive relaunches it; G2 and the secondary review run again. After a second failed round Stage-Gate goes to `replanning` instead: the line is BLOCKED `two_failed_rounds`, and a reassign uses `replace_developer`.
- **out-of-scope** is `overturn_block`. Stage-Gate goes to `integration_checks`: the line is PASSED, its outcome is recorded, and the finding is appended to `gates.follow_ups`. A conflict is raised only by a blocker that carries the conflict tag at its start (`scope:` / `security:`, or `[scope]` / `[security]`; the review prompt asks reviewers to tag that way). The word elsewhere in a blocker ("a module-scoped fixture") is no conflict. Only the tagged blockers are the finding (`adjudication.findings`); every other blocker of the same verdict is kept in `adjudication.other_blockers`, and `other_blocker_count` counts them before any cap. When there are such blockers, out of scope still files the finding as a follow-up but keeps the BLOCK (`maintain_block`): the author gets a fix round for the other blockers, and the ruling is noted in the fix prompt.

A security conflict is ruled out of scope only with a `--note` (`security_out_of_scope_needs_note`). Other refusals: `invalid_ruling`, `not_awaiting_adjudication`, `run_out`, `adjudication_unsupported`, plus the same `request_pending` / `drive_active` as unblock. The audit entry is `human_adjudication`. Parked and queued handling is the same as unblock.

### Cancel or reject a parked dispatch (N36)

`conductor dispatch cancel <id> [--note T] [--force]` (`POST /api/v1/dispatch/{id}/cancel`) ends a dispatch in REVIEW, LAUNCHING, MONITORING or ASSIGNED. It refuses APPROVAL: that decision is `reject`. `conductor dispatch reject` keeps its APPROVAL behaviour; for a dispatch in one of those four states (and not planner-failed) it cancels the same way. A planner-failed dispatch still takes the planner-failure cancel.

- A line that is RUNNING, has a `current_run`, or has a live launch refuses with `line_running: <ids> ...`. `--force` first stops each such run through the launcher's cancel path (`execution.cancel_live_run`: TERM, then KILL, of the process group).
- A drive that owns the dispatch refuses with `drive_active`. `--force` goes ahead, and the epoch bump fences that drive off the row.

Then every unfinished line becomes FAILED `dispatch_cancelled` (or `dispatch_rejected`), its Stage-Gate task gets `cancel`, and queued human requests, `pending_fix` and `pending_relaunch` are dropped. An `operator_cancel` / `operator_reject` audit entry lists the released lines. The dispatch goes to ABORT with a message. No branch is touched: no set-aside and no worktree cleanup.

Limitation: only launches started by this backend process are visible. An orphan left by an older backend is not seen, so `line_running` cannot report it and `--force` cannot stop it.

### Recover a parked line (N76)

The liveness sweep (`_sweep_liveness`, N76 L2) gives every parked line an owner and a next step from the recovery table (`liveness/policy.py`): R-EXIT reassigns twice then splits, R-REVIEW resumes the review after 60 s, 300 s and 900 s, R-QUOTA waits for reviewer headroom for up to 24 h, R-REPLAN reassigns once then splits, R-SPLIT removes the line and its DAG dependants from the round, and the person group (`handoff_incomplete`, reviewer conflicts, G5 failures, `liveness_inconsistent`, ...) goes straight to `needs_human`. `CONDUCTOR_DISPATCH_LIVENESS_MODE` is `shadow` by default (record what the sweep would do, act on nothing; the planner prompt, reassign briefs and every transition stay exactly main's), `enforce` (take the step), or `off` (no record either). `tests/unit/tools/auto_dispatch/test_n76_off_mode_equivalence.py` replays 14 drives against traces recorded on main and requires off to match every save, launch, review, merge and gate command, and shadow to match once the `liveness` records are removed.

`conductor dispatch recover <id> <line> [--note T]` (`POST /api/v1/dispatch/{id}/lines/{line}/recover` with `{mode: "next", note}`) runs the line's next step now, skipping its backoff; a quota wait is skipped too and the review resumes. `--split` (`mode: "split"`) runs R-SPLIT on the line, and `--human` (`mode: "human"`) hands it to a person: its liveness record is latched to `needs_human` with the notification stamped, and no automatic step runs again. Without a line, `recover <id>` asks for the next step of every BLOCKED line and reports each refusal next to its line.

`next` and `split` leave a marker (`gates.lines[id].liveness_operator`) that the drive's sweep performs in every mode, `off` included: a person's request is not a shadow observation. The step is recorded on the line's liveness record with the detail `operator recover: ...` and spends the line's budget like an automatic one. A split still refuses an atomic group that would lose one member and a round with nothing PASSED left; both hand the line to a person. The request takes the unblock path: the same route ownership check (404 for a dispatch the caller does not own), checked and applied to a parked row which is then re-driven, or queued in `gates.human_reassigns` with `kind: "recover"` while a drive runs and performed at its next loop step. The audit entries are `operator_recover` and `human_recover`.

Refusals (409): `not_gated`, `line_not_parked`, `already_split`, `run_out`, `recover_pending`; `needs_human` for `next` on a line a person owns or whose table and budget are spent (use `--split`, `reassign`, `adjudicate` or `cancel`); `already_needs_human` for a second `--human`; `split_waits` when a split (explicit, or the row `next` would run) would leave nothing PASSED while other lines still run; and the queue's `line_moved`, `fix_pending`, `request_pending` and `drive_active`. An unknown mode is a 422 from the API (`invalid_mode` from the service). A request the drive can no longer perform as asked (the line moved, or the split has to wait again) is audited as a refused `human_recover`. `unblock`, `reassign`, `adjudicate` and `cancel` keep every refusal code they had.

A person-owned line goes back to the sweep once a person acts on it: an applied `reassign`, `unblock` or `adjudicate` stamps the liveness record handled and closes it, with the per-line cap restored and the steps already spent kept, so a later park is a new episode the sweep owns (and pages for again if it ends with a person).

`dispatch status` shows `owner` and `budget_left` for each open line, `next_step` and `next_at` for each parked one, and once per dispatch the lines that need a person (`needs a person: ...`). A parked line names its next step and the recover command (`→ next: review at ...; manual: conductor dispatch recover <id> <line>`; under `shadow` or `off` it says the step runs only on request) instead of a list of verbs; a reviewer conflict still names `adjudicate`. The summary's `liveness` key carries the same view (`mode`, `lines`, `needs_human`). A server without it keeps the old hints.

### While a drive runs

`reassign`, `unblock` and `adjudicate` are checked on a copy of the row first, so a refusal answers at once. The request is then queued in `gates.human_reassigns` with a `kind` (`reassign` when absent, `unblock`, `adjudicate`, or `recover`); the queue write does not touch the line or the epoch. The drive applies it at its next loop step, or the service applies it when the drive hands the dispatch off. A request that no longer fits the line when it is applied is dropped and audited as `human_reassign`, `human_unblock`, `human_adjudicate` or `human_recover`. The summary's `human_reassigns.queued` shows each request's kind, ruling and recover mode.

## Live planner: Claude Code

A plain LLM call cannot see the repository, and a real run planned lines with paths that do not exist (`server/alembic/...`, `ptc-cli/...` instead of `migrations/versions/`, `libs/ptc-cli/...`, `src/server/...`). Live dispatch therefore plans with Claude Code by default. `ClaudeCodeComplete` (`src/tools/auto_dispatch/claude_planner.py`) is the `complete(prompt, system)` callable handed to `LLMPlanner`; the validation of the reply is unchanged.

It runs, in `CONDUCTOR_DISPATCH_REPO`, with the planning prompt on stdin:

```
claude -p --output-format json --permission-mode plan --model <model> \
  --restricted --safe-mode --strict-mcp-config --disable-slash-commands \
  --settings '{"disableAllHooks": true}' \
  --tools Read,Grep,Glob --allowedTools Read,Grep,Glob \
  --disallowedTools Bash,Edit,MultiEdit,Write,NotebookEdit,WebFetch,WebSearch \
  --no-session-persistence --append-system-prompt <PLANNER_SYSTEM_PROMPT>
```

Plan mode alone is not the isolation boundary: it is advice the model follows, and a hook in the repo's `.claude/settings.json` would still run under it (see the Claude Code row in `docs/NATIVE-CLI-AGENTS.md`). The planner uses the same boundary as the read-only reviewer in `src/llms/native_cli/claude.py`, plus flags for the extensions that reviewer has no tools to reach. Per `claude --help` on 2.1.309:

| Flag | What it removes |
| --- | --- |
| `--restricted` | user, project and local settings files (and so their hooks); the code-running tools; file access outside the working directory |
| `--safe-mode` | CLAUDE.md, skills, installed plugins, hooks, MCP servers, custom commands and agents, output styles |
| `--strict-mcp-config` (no `--mcp-config`) | every MCP server |
| `--disable-slash-commands` | skills and commands |
| `--settings '{"disableAllHooks": true}'` | hooks in the one settings layer `--restricted` still reads |
| `--tools Read,Grep,Glob` | every other built-in tool; reading and searching the repo stays |

`LS` from the first cut was dropped: `--tools` names the only tools that exist, and `Glob` covers listing. No real CLI has been run against these flags in this repo's tests; the argv is pinned by `tests/unit/tools/auto_dispatch/test_claude_planner.py`.

The `result` field of the JSON envelope is the planner reply. A non-zero exit, a reply that is not a success envelope, an empty result, a missing binary or a timeout raises `ClaudePlannerError`, whose message is a reason code only (`claude_planner_exit:<code>`, `claude_planner_bad_json`, `claude_planner_bad_envelope`, `claude_planner_error_result`, `claude_planner_empty_result`, `claude_planner_unavailable`, `claude_planner_timeout`). The machine parks it as `planner_failed:<reason>`, where the reason is one of a fixed vocabulary (`invalid_json`, `no_json`, `unassigned`, `goal_too_large`, `planner_timeout`, else `planner_error`), so an operator reads why the plan never arrived and the model's own words stay out of the row. Model output and stderr are never logged or stored, and raw or failed model output is never stored as a plan: only a reply `LLMPlanner` has validated becomes lines and a SPEC archive entry.

The CLI runs in its own process group (`start_new_session`). The group is SIGKILLed on timeout, on cancellation and after a normal exit, whether or not the CLI itself has already exited, and the CLI is then reaped. A descendant that outlives its parent (holding the output pipes, or not) does not outlive the call.

The user turn also tells the planner to inspect the repository, name only paths that exist or that the line creates, and give every risk-bearing line (migrations, schemas, auth) a `cmd:` acceptance entry so G2 does not block with `g2_no_commands_risk_bearing`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CONDUCTOR_DISPATCH_PLANNER` | `claude-code` | Live planner backend: `claude-code`, or `llm` for the previous `LLMService.complete` call. Any other value is refused at startup. Dev adapters ignore it and keep `FixedPlanner`. |
| `CONDUCTOR_DISPATCH_PLANNER_BIN` | `claude` | Claude Code executable. |
| `CONDUCTOR_DISPATCH_PLANNER_MODEL` | `claude-opus-5-5` | `--model` for the planner run. |
| `CONDUCTOR_DISPATCH_PLANNER_TIMEOUT` | `600` | Seconds before the run is killed (`claude_planner_timeout`). |

Unit tests never start the real CLI: `tests/unit/conftest.py` replaces the planner's spawn seam with a guard that starts a binary only when its resolved absolute path is on an explicit allowlist (the set the `_no_real_claude_planner` fixture returns). The planner tests add their fake script to it; every other binary, on PATH or at an absolute path, is refused as missing.
