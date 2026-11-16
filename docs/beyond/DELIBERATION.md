# Deliberation before execution

Gap 8 sits in front of Super-agent execution. This is the design that would otherwise live in `docs/SUPER-AGENT-BEYOND.md`. It is a separate file so the shared-thread branch does not collide with that writeup.

## Problem

When Conductor splits work across agents (codex, kimi, and grok daemons and their CLIs), those agents otherwise execute different plans in parallel and collide. Gap 8, beyond the Super-agent article, requires a deliberation before any of that work starts. Two or more agents share one thread. In round 1 each participant proposes a plan. Later rounds critique and revise. A planner synthesizes. Execution starts only when the participants agree, or when a configurable `max_rounds` cap (default 3) lets the planner decide and record dissent. The agreed plan becomes a ticket DAG of steps, dependencies, and assigned agents, and that DAG is what runs in parallel.

## Why this is hard

Consensus can deadlock. The cap must still produce exactly one plan, and the losing objections have to remain as dissent instead of vanishing into the last message.

Conductor already runs multiple tickets at once. Daemon claim takes one running ticket per daemon. That rule is `claim_ticket` in `src/server/database/autonomy/daemons.py`. Deliberation steps must not be forced through that claim. Routing them there would run rounds and DAG steps serially, and the change would break parallel execution across daemons and parallel steps inside one plan.

Branch `feat/shared-thread` is adding shared-thread context semantics. Deliberation must not write `conversation_threads` or `autonomy_threads` directly. Those tables stay owned by chat and by the existing autonomy thread model.

Participants are different local CLIs (`codex exec`, `kimi -p`, `grok -p`) plus a deterministic mock for tests. The planner that merges plans is not a fourth paid CLI call.

Live Postgres and Redis must stay untouched by tests. The same service has to run on an in-memory store and on sqlite.

## Design

Migration 111, `migrations/versions/111_deliberation.py`, adds `autonomy_deliberations` plus participants, rounds, messages, votes, dissents, and steps. At most one deliberation in `draft` or `running` exists per ticket. `thread_key` is plain text. It has no foreign key into conversation threads.

The state machine is `src/server/services/deliberation/machine.py`. Status moves from `draft` to `running`, then to `consensus` when every voting participant agrees, to `decided` when the planner closes at `max_rounds` and writes dissent rows, or to `failed`. Round 1 collects proposals. Later rounds are critique and revision, then planner synthesis. Votes come from non-planner participants only.

The thread seam is `DeliberationThreadPort`, implemented for now by `MemoryThreadPort` in `src/server/services/deliberation/thread_port.py`. The shared-thread branch can replace that port later. Until it does, deliberation does not insert into `conversation_threads` or `autonomy_threads`. The HTTP handlers do not keep a process-local port across workers. Each call appends to a fresh in-memory port, and the durable transcript is the message rows. Callers that already hold a port, including the demo, pass the same instance into create and start.

Adapters live in the same package. `MockParticipantAdapter` scripts consensus and a holdout so tests never spawn a CLI. `MergingPlannerAdapter` is a deterministic union and prefers the canonical spec, impl, and review plan. It is not a model call. `LocalCliParticipantAdapter` runs the daemon-local subprocess and does not HTTP to port 8000.

`plan_to_dag` rejects cyclic steps. `handoff_ready_steps` marks every step whose dependencies are completed as `running` in one call, so a whole wave leaves together. In the canonical plan, spec (codex) and impl (kimi) have no dependencies and hand off together. Review (grok) waits on both. `RecordingExecutor` records that wave. `child_ticket_id` stays empty. A later scheduler may create tickets. Deliberation is opt-in through the API and is not hooked into `launch_worker_effect` in `src/server/services/autonomy/worker.py`.

Routes stay on `/api/v1/autonomy` in `src/server/app/deliberation.py`, behind the same `super_agent` feature gate: create a deliberation for a ticket, start it, and read the transcript. Tests use the mock adapter and sqlite or memory only. `scripts/deliberation_demo.py` runs the three real CLIs once against an offline sqlite file and prints the transcript and the DAG. Store behavior is pinned on the deliberation store protocol, not on a live database.

## Tradeoffs

Synchronous start is simple and testable, but a long CLI round blocks the request. That is acceptable because the demo and the tests do not sit on the ticket scheduler.

A deterministic planner, rather than another model, makes the merge reproducible and avoids a fourth CLI. It can miss a subtle merge a model would catch.

Tests and the demo store plan payloads as JSON in sqlite or memory. Production uses the normalized tables from migration 111. Tests never open Postgres or Redis.

Skipping `claim_ticket` keeps today's one-running-ticket rule for daemons and still lets several DAG steps hand off together, including steps that name the same daemon. Parallel ticket execution across daemons is unchanged.

Dissent rows are written only when the cap is hit, not on every intermediate no. A later round that agrees therefore does not look like a rejected plan.

The default `max_rounds` is 3. The live demo may pass 1 so the three local CLIs are not invoked for three full rounds.
