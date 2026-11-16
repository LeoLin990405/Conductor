# Dynamic Sub-agent Orchestration

This document is the working design for the Super-agent role model. It is a
first-class layer above the existing Ticket pipeline; it does not replace the
Router/Worker split in [`SUPER-AGENT.md`](SUPER-AGENT.md).

## Role policy

| Role | Default model | Writes code | Responsibility |
|---|---|---|---|
| Planner | `claude` | No | Clarify the goal, write the SPEC, decompose the Task DAG, define acceptance tests |
| Primary reviewer | `codex` | No | Design/code review, security and test adequacy, primary verdict |
| Developer pool | `kimi`, `glm`, `grok`, `cursor`, `stepfun`, `deepseek`, `minimax` | Yes | Implementation, focused tests, handoff. Order is the tie-break; `minimax` is last, so it is the first one dropped when the pool is short of places |
| Work tail | `doubao` | Yes | Still eligible for work, after the developer pool. Not reviewers |
| Secondary reviewer | `stepfun` | No code changes in that task | Developer pool minus `REVIEW_INELIGIBLE`, never the author. `grok` and `cursor` keep work eligibility but have no read-only review mode yet — v2, not this wave |
| Integrator / human | — | Merge only | Gate arbitration and final approval |

The reservation is deliberate: the model that plans is not the model that
writes the main implementation, and the primary reviewer does not review its own
work. `allow_reserved_code` is an explicit emergency override, not a default.
When that override selects a reserved model for work, the dispatch line stores
`assignment_audit` with the reason.

`stepfun` is therefore the only default secondary reviewer. `grok` and
`cursor` stay in the work roster and keep taking work in roster order; they
are simply not reviewers, because neither has a read-only CLI mode. That gap
is a v2 item rather than a choice for this wave, and `secondary_reviewer_pool`
can name them once it is closed.

An operator can replace this roster with `CONDUCTOR_ROLE_POLICY_FILE` (YAML or
JSON, parsed by `RolePolicy.from_mapping`). A missing or invalid file raises
at dispatch startup and does not fall back to the table above.

The pure policy lives in
[`src/server/services/autonomy/roles.py`](../src/server/services/autonomy/roles.py).
It exposes `RolePolicy`, `AgentState`, and selection helpers for the planner,
developer pool, primary reviewer and secondary reviewer. Selection is
deterministic and auditable: success rate, then free capacity, then unlimited
quota ahead of any finite quota, then earlier roster order.

## Task lifecycle

```text
Mission
  → Plan (Planner)
  → G0 SPEC accepted
  → D1 Task DAG
  → Assign (dynamic scheduler)
  → Implement + handoff
  → G2 focused tests pass
  → Primary review (Codex)
  → Fixes / reassign
  → Secondary review (another developer-pool model)
  → G5 full suite + single Alembic head
  → G6 human merge
```

Dynamic reassignment happens only at a checkpoint (before a review or after a
BLOCK), never mid-commit without a new handoff. The handoff carries branch,
commit, changed files, test commands, results and known limitations, so a
replacement model does not need the author's chat history.

## Gates and failure policy

- A reviewer never receives the author's private reasoning; it reviews the
  branch diff, tests, acceptance criteria and handoff.
- Two failed fix rounds under the same reviewer: stop, return to the Planner
  for re-decomposition, or replace the developer.
- Reviewer conflict: the Planner decides against the SPEC; a security or scope
  disagreement goes to the human.
- The primary reviewer's `MERGE` does not replace the secondary review; both
  are required for risk-bearing changes.
- Every gate and every assignment/reassignment is recorded as a Ticket event.

## Implementation status

Implemented in this repository:

- reserved-role policy and deterministic dynamic selection (unit-tested);
- Ticket/DAG building blocks and isolated worker worktrees;
- single-author/no-self-review constraints in the existing review path;
- review/merge API and Codex gate.

Live `conductor dispatch` (`CONDUCTOR_DISPATCH_ADAPTERS=live`) takes its roster
from this policy. Work does not select the planner or the primary reviewer
unless `allow_reserved_code` is set. Review tries `codex` first, even when a
secondary has a higher success rate or fewer assignments, and only then
`stepfun`. `grok` and `cursor` stay in the work roster but are not reviewers
until they have a read-only CLI mode. If no read-only secondary remains, the
selector returns no model and sets `review_refusal` to `no_secondary_reviewer`.
The selector reads success rate from outcomes
already stored on dispatch rows, and quota from `default_headroom()`: the more
exhausted of the CodexBar snapshot file (`CodexBarHeadroom`) and the snapshot
behind `load_usage_headroom`; with neither, headroom is unlimited. The planner's
DAG assignment reads the same port. Every provider spelling is folded by
`roles.canonical_provider` before a headroom read. Dispatch names that differ from native-runtime ids go through
`PROVIDER_ALIASES`: `claude` to the claude runtime id, `glm` to `zcode` (the
GLM Worker key with full capabilities; `zhipu` can only cancel), `deepseek` to
`deepseek-dsh`, and `minimax` to `minimax` — written out explicitly even though
both sides spell it the same, so a rename has one place to happen. `doubao`
is a development Worker on the native `doubao` provider (`arkcli`). It stays
in the work tail, after the developer pool, and it is not a reviewer.
`DISPATCH_ONLY_REASONS` is empty. Manual
`reassign` uses the same reserved-role rule as live work selection and writes
`assignment_audit` when `allow_reserved_code` is set. The dev adapters are
unchanged: round-robin over `DEV_MODEL_POOL`, no role rules.
`MODEL_POOL` remains only as a deprecated alias of that tuple.

The planner is live, and no longer only exercised by unit tests.
`LLMPlanner.plan_dag(goal)` asks one model for a SPEC and a task DAG, and
`validate_plan` holds the reply to a strict schema: a malformed payload is a
`DispatchError` that names the stable reason instead of a guess. The developer
assignment goes through `role_plan.plan_developer_assignments` with the
`scopes_overlap` check, and the validated plan is archived by `FileSpecArchive`
as `SPEC.md` plus `plan.json`.

`cursor` is in the work roster. The launch script outside this repository
(`~/conductor-wt/launch.sh`) still has no `cursor` branch; that is an
operations follow-up, not part of this policy.

Not wired yet:

- Kimi, GLM, DeepSeek, Grok, Cursor and MiniMax have no read-only review mode (v2), so `stepfun` is the only default secondary reviewer;
- the dispatch service passes no archive and no policy into `LLMPlanner`, so a live run validates and assigns but archives nothing;
- `Line.risk_bearing` is carried in `PlanResult` and in the SPEC archive rather than driving a gate;
- DAG-ordered launch and merge on `depends_on` belongs to the Stage-Gate wiring, not to this policy;
- success rate only sees the newest 500 dispatch rows; a dedicated rollup is v2;
- the TUI does not yet show the roster, DAG and gate state.

The next integration step is the Stage-Gate state machine on the autonomy
scheduler. This policy does not implement that gate.

## Assignment planner

`src/server/services/autonomy/role_plan.py` turns a task DAG into a
deterministic developer assignment:

- tasks are considered in dependency order;
- a task is assigned only after every dependency was assigned;
- capacity is simulated across the plan, so one agent cannot be given more
  parallel places than it has;
- the reserved planner is never assigned development work;
- a missing dependency, cycle, duplicate task id or exhausted pool is reported
  as an unassigned task with a stable reason rather than a partial plan that
  looks complete.

The planner mutates no Tickets. Applying the plan, leasing the assigned agent
and recording the decision remain the scheduler's job.

A failed developer can be replaced without rebuilding the whole plan:
`replan_after_failure(previous, tasks, agents, failed_task_id=..., failed_provider=...)`
keeps every unaffected assignment, charges their capacity first, and offers the
failed task again without the provider that failed. If no place remains, the
task is returned unassigned with a stable reason.

Ready tasks may carry a priority; a higher-priority task is considered first,
but a dependency always wins: a high-priority child never jumps an unassigned
prerequisite. `AssignmentPlan.to_dict()` is the audit shape used by the future
Supervisor: task id, provider, status, and stable unassigned reasons only — no
prompt, token or tool content.

`plan_supervisor_mission(tasks, agents)` returns the reserved planner and the
developer assignment in one audit-friendly object. A missing planner is
explicit (`planner_reason: no_planner`); the planner never falls back to a
developer, and the developer plan is still reported so an operator can see
what would run once a planner is available.

The planner also serializes overlapping `write_scope` entries: a task whose
scope is already owned by an assigned task is returned unassigned with reason
`write_scope_conflict:<task-id>` instead of letting two developers edit the
same file in parallel. An empty scope means the task declared no file claim.

### Planner output contract

`LLMPlanner.plan_dag(goal)` asks one model for a SPEC and a task DAG, then
holds the reply to a strict schema before anything downstream sees it. A
malformed plan is a plan failure, not a dispatch failure: the caller is told
to refine the goal rather than being handed a guess.

The normative example is
[`tests/fixtures/planner_output_v1.json`](../tests/fixtures/planner_output_v1.json).
It is the fixture the tests load, so it must not be edited by hand. The same
schema is stated to the model by
[`src/tools/auto_dispatch/planner_prompt.py`](../src/tools/auto_dispatch/planner_prompt.py),
at `PLANNER_PROMPT_VERSION` (`planner_prompt_v1`), and
[`src/tools/auto_dispatch/planning.py`](../src/tools/auto_dispatch/planning.py)
checks the reply against it.

```json
{
  "schema_version": 1,
  "spec": {"title": "string, at most 256 bytes", "body": "markdown, at most 16000 bytes"},
  "lines": [
    {
      "name": "snake_case, unique in the plan, at most 256 bytes",
      "task": "one imperative sentence, at most 8000 bytes",
      "depends_on": ["names of other lines of this same plan"],
      "write_scope": ["repository-relative path or glob, 1 to 32 entries"],
      "acceptance": ["cmd: <argv> or a human-readable assertion, 1 to 16 entries"],
      "risk_bearing": true
    }
  ]
}
```

Every rejection carries a stable reason, and `validate_plan` runs its checks in
a fixed order — `schema_version`, `spec`, `spec` size, line count, per-line
text and byte limits, duplicate names, dependencies, the dependency cycle,
write scopes, acceptance, field types, unknown fields — so the same payload
always yields the same first reason. The planner-output reasons and the
`role_plan` reasons share one table. Rows marked *reused* are `role_plan` codes
kept verbatim, so an operator meets one vocabulary for a graph error wherever
it is caught.

| Reason | Where it comes from | Meaning |
|---|---|---|
| `goal_too_large` | planner input gate | the goal is over 8 000 bytes after stripping; refused before the model is called |
| `schema_version_unsupported:<value>` | planner validator | `schema_version` is missing, not an integer, or not `1` |
| `missing_spec:<detail>` | planner validator | no `spec` object, or an empty `title` or `body` |
| `spec_too_large:<title or body>` | planner validator | the SPEC title is over 256 bytes, or its body over 16 000 |
| `no_lines:<value>` | planner validator | `lines` is missing, not a list, or empty |
| `too_many_lines:<count>` | planner validator | more lines than `max_lines` (8); a longer plan is refused, never truncated |
| `invalid_field:<detail>` | planner validator | the payload or a line is not an object, a `name` is empty or not a string, or a `task` is blank |
| `invalid_text:<field>` | planner validator | the text carries a NUL or a lone surrogate, which shortening would not fix. The SPEC title and body are checked as `spec_title` / `spec_body` too, because the archive writes them as UTF-8 |
| `line_too_large:<field>` | planner validator | a `name`, `task`, `write_scope` or `acceptance` entry is over its byte ceiling, or a list is over its entry ceiling |
| `duplicate_task_id:<name>` | planner validator, reused from `role_plan` | two lines share a `name` |
| `self_dependency:<name>` | planner validator | a line names itself in `depends_on` |
| `missing_dependency:<name>` | planner validator, reused from `role_plan` | a dependency names a line the plan does not define. After a line is filtered because its write scope failed, the same code is `missing_dependency:<filtered line id>` and applies only to lines that depend on that line, directly or through other filtered dependents. A line with no such dependency is planned as if the filtered line were absent, and is not given this reason |
| `dependency_cycle` | planner validator, reused from `role_plan` | the dependencies form a cycle |
| `empty_write_scope:<name>` | planner validator | no non-blank `write_scope` entry: the list is missing, not a list or empty, or an entry is blank once trimmed |
| `invalid_text:<name>` | planner validator | the first failing `write_scope` item, before the byte, blank, and path checks: not a string, a NUL, or a lone surrogate. An `acceptance` entry that is not a string is `invalid_field:acceptance:<name>`; a NUL or lone surrogate in acceptance is `invalid_text:acceptance` |
| `invalid_write_scope:<name>` | planner validator | an absolute path, a `..` segment, a leading `~` or a drive letter in the trimmed spelling |
| `line_too_large:<name>` | planner validator | more than 32 `write_scope` entries, or one entry over 512 bytes; measured only after the value encodes, so a lone surrogate is `invalid_text` rather than oversized |
| `write_scope_conflict:<a>:<b>` | planner validator, reused from `role_plan` | two lines can write the same file; the assignment stage reports the same code as `write_scope_conflict:<task-id>` |
| `empty_acceptance:<name>` | planner validator | no non-blank `acceptance` entry |
| `invalid_field:<field>:<name>` | planner validator | a field has the wrong type, for example a non-boolean `risk_bearing` |
| `unknown_field:<field>` | planner validator | an unknown field at the top level or on a line |
| `dependency_unassigned:<id>` | `role_plan` assignment | a dependency of this line could not be assigned |
| `no_capacity` | `role_plan` assignment | no developer has a free place for this line |

A missing planner is a separate matter and stays explicit
(`planner_reason: no_planner`): the planner never falls back to a developer,
and the developer plan is still reported.

### A dispatch parked on a planner failure

A dispatch whose planner failed is parked in `REVIEW` with no line, and its
message is `planner_failed:<reason>`. The reason is one of a fixed
vocabulary (`src/tools/auto_dispatch/planner_reason.py`), so an operator can
grep for it and a dashboard can switch on it:

| Reason | Meaning |
|---|---|
| `invalid_json` | the reply did not parse, or it did not hold the schema |
| `no_json` | the reply held no plan at all: no JSON object, or an empty result |
| `unassigned` | the plan was fine, but the role policy could not place every line |
| `goal_too_large` | the goal was over 8 000 UTF-8 bytes and was refused before the model ran |
| `planner_timeout` | the planner run itself timed out: the Claude Code run passed its own timeout, or the model call raised a `TimeoutError` (`asyncio.TimeoutError` is that same class) |
| `planner_error` | anything else: the planner failed without saying more |

The model's own words never reach the message, the row or a log line: a
failure that names no reason is recorded as `planner_error` rather than
guessed at. A row parked before these codes existed carries the bare
`planner_failed`; it reads as `planner_error` and is rejected the same way,
so the rows that predate the codes are not stranded. The goal ceiling is one
constant (`MAX_GOAL_BYTES` in `planning.py`), held by the API request model
as well, so a goal the API accepts is a goal the planner can plan.

Nothing ran when a dispatch parks this way, so there is no branch to move
aside and no worker to stop: rejecting such a dispatch ends the run instead
(`POST /api/v1/dispatch/{id}/reject`), and the row goes to `ABORT` with
`cancelled` beside the reason it parked with.

### Write-scope overlap

`overlaps(a, b)` in
[`src/server/services/autonomy/role_plan.py`](../src/server/services/autonomy/role_plan.py)
is the single test, used by the planner validator and by the assignment stage
alike. Both scopes are normalised first — repeated separators collapsed, every
`.` segment dropped (so `././` is the root scope), a trailing separator removed,
and an empty result or a bare `**` read as the root scope `.` — and then walked
segment by segment:

| segments | outcome |
|---|---|
| either is `**` | overlap |
| two literals that differ | **no** overlap |
| a literal against a wildcard | decided by `fnmatch.fnmatchcase` |
| two wildcards | may still intersect, so the walk continues |
| one side runs out first | it covers the other's subtree, so overlap |
| both run out together | overlap |

The rule is conservative by construction: whenever the two sides cannot be
proven disjoint the answer is "overlap", because a false conflict costs one
lane while a missed one puts two developers in the same file. The root scope
overlaps everything; `*.py` overlaps `foo.py` but not `src/a.py`, because a
root-level glob cannot reach into a directory.

A scope is validated on the **trimmed** spelling, because that is the value
which is stored in the `Line`, in the archive and in the assignment claim. The
trim happens first and every check runs on the trimmed string: an empty or
whitespace-only value is `empty_write_scope`, a NUL is `invalid_text`, and an
absolute path (`/…`, `X:…`, or a leading `~`) or a `..` segment anywhere is
`invalid_write_scope`. Checking the untrimmed string instead would let a leading
space hide an absolute path or a `..` segment from the validator while the list
builder quietly stripped it afterwards — which is exactly the bug this order
closes. Refusing before folding means `src/../x.py` is a plan failure rather
than a quiet rewrite to `src/x.py`.

`check_write_scope` is the ONE scope check, and both the validator and the
direct `assign_dag` entry call it, so the two layers cannot disagree. The whole
list is judged first (missing, not a list or empty is `empty_write_scope`; more
than 32 entries is `line_too_large`), then each entry in order, stopping at the
first failure — so the reason is always the **first failing entry's**, never a
later entry's. `assign_dag` reports that same reason as
`<reason>:<line_id>` and never offers the line as a claim, so a caller that
builds its own lines cannot get an empty, blank, unencodable or escaping scope
placed or archived.

Filtering that line removes it from the graph the role planner sees. A line
that depends on it, directly or by a chain of such dependents, is not
assignable and is reported as `missing_dependency:<filtered line id>`. The id
is the filtered line, not the intermediate child: a chain and a diamond both
name the filtered ancestor, and that ancestor is the one reached by the first
`depends_on` entry while walking in topological order, so the result does not
depend on dict order. A legal line that does not depend on the filtered line
keeps the diagnosis and the assignment it would have if that line were absent.
In particular it is not marked `missing_dependency`. A plan in which every
line fails the scope check is still rejected with the same first reason as
before. When some other line is legal, the filtered line is not built into a
`Line` and its original text is not archived: the id was already checked, and
the archive keeps only that id and the reason, on the unassigned list.
Leaving the other fields unread starts once the line has entered this filter.
Text size and dependency checks still run first, on every line, which is the
original first-failure order: a blank or oversized task, or a dependency the
plan does not name, is still that earlier reason rather than a scope filter.
After the scope check has filtered the line, its later fields — acceptance,
risk, unknown fields — are not read, and the raw scope is not copied onto a
`Line`. Lines that remain are validated in full, and those validated fields
are what the archive writes.
Overlap is checked only among lines that can still be assigned. A child of a
filtered line is not assignable, so it does not take part: its scope
overlapping an unrelated legal line does not reject the plan, and that legal
line is still assigned.

The normative pairs live in
[`tests/fixtures/write_scope_overlap_vectors_v1.json`](../tests/fixtures/write_scope_overlap_vectors_v1.json),
and both the validator tests and the assignment tests read that one file, so
the two layers cannot drift apart on what overlaps. Its `rejected_whitespace`
key holds the scopes that are invalid only because of surrounding whitespace —
the regression class a leading-space bug opened.

Two lines that depend on each other are also refused when their scopes
overlap, because every assigned line starts at once and a dependency does not
serialise the writes. Relaxing that once lines run in dependency order is a v2
item; the check stays conservative in this wave.

### Unassigned lines

Any unassigned line makes `plan()` a plan failure. The `DispatchError` lists
every `line_id:reason` at once, so an operator sees the whole breakage rather
than the first symptom, and the full `unassigned` list is kept in `PlanResult`
and in the archive. There is no selector fallback: a line the role policy
cannot place is never quietly reassigned by the quota-aware selector.

### SPEC archive

`FileSpecArchive(root)` keeps one directory per plan under `root`, named by
the plan's own hash, and writes two files atomically — a temporary file plus
`os.replace`:

- `SPEC.md`: the SPEC title and body, and nothing else;
- `plan.json`: the validated lines with their ids, names, tasks,
  dependencies, write scopes, acceptance, `risk_bearing` and
  `assigned_model`; the assignment plan in its audit shape; the
  `risk_bearing` line ids; `schema_version` and `prompt_version`.

`spec_id` is the first 16 hex characters of the sha256 of the canonical
`plan.json` body, so archiving the same plan twice is the same directory and
the same reference (`spec:<spec_id>`), and an archive that already exists is
left as it stands. A failed write is a plan failure
(`spec_archive_failed`), never a swallowed error.

Never stored: the model's raw reply, the prompt, the goal, any reasoning text,
token usage, or a credential. `LLMPlanner(complete, *, archive=None)` writes
nothing to disk when no archive is injected, and keeps the reference on
`last_result` for the caller to read.
