# Runbook — canary and rollback for the refactor flags

Every refactor surface is additive and default-off. A deployment that ships the
code with flags unset behaves like the pre-refactor build; the flags are the
canary. This runbook is the order to flip them, what to verify at each stage, and
how to roll back.

Tooling: `scripts/refactor_status.py` (flags, schema revision, projection row
counts), `make memory-eval` (selection gate), and the existing `/health` +
backend logs.

```bash
uv run python -m scripts.refactor_status              # flags + repo schema head
uv run python -m scripts.refactor_status --database   # + schema revision and row counts
```

## Stage 0 — ship dark

1. Deploy the refactor code with **all six flags unset**.
2. Apply migrations with the normal deploy (`alembic upgrade head` → **094**);
   they are additive and the flags are off, so no projection is written yet.
3. Verify: `/health` healthy; `refactor_status --database` shows schema
   `migrated`, every flag `off`, the refactor tables present with 0 rows.
4. Abort if the schema is `behind`/`unknown` — the flags must not be turned on
   over an unmigrated database (the status tool compares the repo head with the
   database revision).

## Stage 1 — cheap dual writes

Flip on the writes that are invisible to the prompt and the read APIs:

```bash
REFACTOR_EVENTS_ENABLED=1
REFACTOR_TASK_TREE_ENABLED=1      # also enables the TodoWrite snapshot mirror
REFACTOR_SUBAGENT_CONTRACT_ENABLED=1
REFACTOR_PERMISSIONS_ENABLED=1    # no behaviour change until a thread says manual
```

Run one normal turn, then check `refactor_status --database`: `agent_events`,
`agent_run_contracts`, `agent_tasks` gain rows. With `subagent_contract` on, a
finished subagent run also gains `subagent_runs.result`/`contract_version` and a
`subagent.result` event once its result text resolves. Verify a turn's
user-visible behavior is unchanged (same answer path; the TodoWrite chip still
comes from SSE).

Rollback: unset the flags and restart. Rows stay; projections are not read by
legacy paths, so removal is a no-op behaviorally.

## Stage 2 — goal read surface

```bash
REFACTOR_GOALS_ENABLED=1
```

Verify `GET /api/v1/threads/{id}/goal` on an owned thread returns
`{enabled:true, goal, plan, tasks}` and a legacy thread still returns
`goal:null`; the composer strip appears only when a goal exists. Plan decisions
(`POST .../goal/plan/{plan_id}/decision`) write a `plan.decided` event.

Rollback: unset `REFACTOR_GOALS_ENABLED` → the endpoint answers
`enabled:false` and the shell hides the strip; no data migration needed.

## Stage 3 — memory projection, then retrieval

Memory is the only stage that changes model input, so it is last and it has a
data precondition:

1. **Backfill** (flags still off): dry-run, then apply —
   `python -m scripts.refactor_memory_backfill` then `--apply`; require
   `missing/extra/changed` empty and `rows > 0` if the store had memory. See
   `runbook-memory-backfill.md`.
2. **Enable** `REFACTOR_MEMORY_INDEX_ENABLED=1`. This turns on the write-path
   dual write, the selection/audit, and the `<memory-context>` render switch in
   one flag. Verify:
   - `memory_entries` grows as memory files are written (dual write);
   - a turn's `agent_run_contracts.injected_memory` is populated and the
     `<memory-context>` block replaces the per-tier `<memory>` blocks
     (LangSmith / prompt capture);
   - `make memory-eval` still passes (selection policy baseline);
   - one CJK retrieval probe run on the deployment database still returns the
     expected hits (`scripts/memory_eval/live_pg.py --cjk-path`).
3. With `memory_index` on, the subagent `ProposeMemory` tool is exposed and
   extracted proposals land in `agent_memory_proposals`; the inspector lists
   them and approving one writes the store before the queue status moves. Verify
   a rejected proposal writes nothing and an approved one appears in memory.md.
4. Abort immediately on: a turn error rate increase, a missing
   `injected_memory` row, or a memory-eval regression.

Rollback: unset the flag and restart. The next turn renders the legacy per-tier
indices again; projection rows and the trigram index stay (re-enable later is
immediate; the backfill is already done).

## Rollback matrix

| flag | unset restores | data left behind |
| --- | --- | --- |
| `events` | no event append, no `permission.decision` projection | `agent_events` rows (append-only, unread) |
| `task_tree` | no TodoWrite mirror | `agent_tasks` rows |
| `subagent_contract` | no contract rows/subagent result columns written | `agent_run_contracts` rows (`subagent_runs` columns null) |
| `permissions` | no per-thread mode read; every build uses standing approvals (today's behaviour) | `agent_thread_permissions` rows |
| `goals` | endpoint `enabled:false`, no plan decisions | `agent_goals`/`agent_plans` rows |
| `memory_index` | legacy per-tier indices, no dual write/audit/selection | `memory_entries` rows + pg_trgm index |

Code rollback: restore the previous image/commit; the additive tables are
ignored by the old code. Migration downgrade is only needed to remove the
tables deliberately (`alembic downgrade 085`), and it destroys projection data —
the store is the source of truth, so the backfill rebuilds it.

## Abort thresholds (guidance)

- any 5xx rate above the pre-canary baseline for 15 minutes;
- p95 turn latency +20% over baseline, sustained;
- any `[refactor] ... failed` log for a dual write that repeats across turns;
- memory stage only: `make memory-eval` below thresholds, or an injected-context
  regression visible in prompt capture.
