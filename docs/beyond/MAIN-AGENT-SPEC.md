# Main Agent orchestration contract

The Main Agent coordinates work across tickets. It accepts and clarifies requests, divides work, plans a dependency DAG, tracks worker progress, merges results, and writes the final reply. It does not execute ticket tasks. Worker callbacks own execution.

## Turn lifecycle

```text
intake → clarification gate → divide → deliberate → plan DAG
      → workers self-claim ready tasks → execute → track
      → merge results → synthesize reply
```

1. **Intake** stores the request and creates a durable run.
2. **Clarification gate** decides whether the request can proceed or needs a user answer. This is a replaceable interface; the initial implementation can be a deterministic stub.
3. **Divide** proposes task records. **Deliberation** reviews task scope and risks. Each task may name a `deliberation_agent_id`, separate from its worker `assigned_agent_id`; every named deliberation participant must return a conclusion for its task. Both interfaces stay thin so model-backed implementations can be added without changing persistence or scheduling.
4. **Plan** assigns task identifiers and dependencies. The plan must be a valid acyclic DAG. Tasks with unmet dependencies stay queued.
5. **Claim and execute** lets workers call `ready_tasks`, then `claim_task` with a worker and optional daemon identity. A successful claim returns a fencing token. Workers use it for heartbeat, completion, and failure callbacks. The Main Agent never claims or performs task work. Multiple tickets and workers can make progress concurrently.
6. **Track** records leases, heartbeat/progress timestamps, retries, and deduplicated alerts. Defaults are a 120-second heartbeat threshold, a 600-second progress threshold, and a 180-second claim lease. Lease expiry raises an alert but does not automatically requeue work; an authorized explicit retry clears the old claim fence and reopens the run.
7. **Merge and reply** runs after required tasks complete. A merger combines worker reports and a synthesizer produces the user-facing answer.

After watchdog work, the autonomy scheduler scans nonterminal runs and queues bounded, idempotent advance effects. The effect runner calls the coordinator's `tick` method to advance planning or complete merge/reply for settled task sets. Workers continue to self-claim ready tasks; users do not need to call the run `tick` endpoint to produce a final reply.

The orchestration boundary is dependency-injected: repository, clarification gate, deliberation, divider, planner, merger, and reply synthesizer. Production divide, plan, merge, and reply adapters use the canonical user-aware `LLMService` through `LLMMainAgentAdapter`. Clarification and deliberation are the explicit deterministic stubs by default; their interfaces can be replaced independently. Tests and the offline demo inject stubs for all model-facing stages, so validation makes no live model calls.

The authenticated REST lifecycle is `POST /api/v1/main-agent/runs` (submit), `GET /api/v1/main-agent/runs/{run_id}` (read), `POST /api/v1/main-agent/runs/{run_id}/clarification`, `POST /api/v1/main-agent/runs/{run_id}/advance`, and `POST /api/v1/main-agent/runs/{run_id}/tick`. Worker APIs are `GET /api/v1/main-agent/runs/{run_id}/tasks/ready`, then `POST` to the task's `/claim`, `/heartbeat`, `/complete`, `/fail`, or `/retry` route. All worker callbacks require the authenticated run owner and claim fencing token. The status board route is described below.

## Persistence and concurrency

Production run and task state belongs in Postgres. Mutations use revisions and compare-and-swap so independent backend processes cannot overwrite newer state. Task claims also check dependencies and ownership. Retrying a retryable task reopens its task and parent run in the same repository transaction. SQLite is for offline demonstrations and local contract checks only; it is not a production persistence option.

Task records carry an identifier, description, dependency identifiers, status, assigned agent, lease, heartbeat/progress timestamps, attempt count, and result or error. Run records carry the request, phase, clarification, deliberation, plan, tasks, alerts, and timestamps. A run can be pending clarification, queued, running, completed, or failed. Worker retries and stale-run alerts must be recorded without presenting a failed or partial run as complete.

## Status board API

`GET /api/v1/main-agent/status-board` returns the authenticated user's current agent rows, alerts, and configured stale thresholds:

```json
{
  "agents": [{
    "run_id": "run-id",
    "task_id": "task-id",
    "ticket_id": "ticket-id",
    "agent_id": "worker-id",
    "daemon_id": null,
    "state": "running",
    "last_heartbeat": "2026-11-02T01:02:03Z",
    "current_step": "Review source files",
    "stalled": false,
    "stall_reason": null
  }],
  "alerts": [{
    "id": "alert-id",
    "run_id": "run-id",
    "task_id": "task-id",
    "severity": "warning",
    "message": "Worker heartbeat expired",
    "created_at": "2026-11-02T01:02:03Z",
    "resolved_at": null
  }],
  "thresholds": {"heartbeat_seconds": 120, "progress_seconds": 600}
}
```

Timestamps are UTC ISO 8601 strings. Each row is a task/agent assignment, so a run can appear in several rows. Missing ticket/daemon associations and steps are `null`. The Agents page polls this endpoint every five seconds and shows loading, error, empty, active, and alert states.

## Offline demonstration

`scripts/main_agent_demo.py` uses SQLite and deterministic mock interfaces by default. It submits two tickets to one mock daemon, lets independent ready tasks self-claim concurrently, proves each dependent task waits for its prerequisites, and simulates the production outbox advance effect after worker callbacks to produce final replies without a manual client tick. The demo exercises the same service/repository contract as the application. It does not require Postgres, Redis, a running backend, a browser port, or external model credentials.

Live integrations are outside the offline demo contract. Any optional live adapter must be explicitly selected and must not change the default behavior.
