# Project orchestration

Conductor's project layer turns one goal into a durable, concurrent plan.

## Model

```text
Project
  workspace_id, name, goal, concurrency, status
Task
  title, prompt, depends_on[], status, attempts, thread_id, run_id, error
```

Task lifecycle: `queued → dispatching → running → completed / failed / cancelled`.
A project can be started, paused, cancelled and recovered. On recovery, tasks
that were running without a live run are requeued.

## Scheduling

1. The scheduler selects ready tasks: queued tasks whose dependencies completed.
2. It respects the project concurrency limit.
3. It creates a thread for each task and dispatches through the same chat entry
   point a user send uses (`X-Dispatch: background`).
4. Workers choose PTC or native CLI runtime from the task/thread settings.
5. Completion or failure settles the task and wakes the scheduler for the next
   ready task.

## Workspace isolation

Every worker owns:

```text
.orchestrator/projects/<project>/<task>/
```

The prompt contract requires writes below that folder. Two workers on one
workspace cannot silently overwrite each other's files; shared reads remain
possible.

## API

```text
GET/POST        /api/v1/projects
GET/PATCH/DELETE /api/v1/projects/{id}
POST            /api/v1/projects/{id}/start|pause|cancel
POST/GET        /api/v1/projects/{id}/tasks
POST            /api/v1/projects/{id}/tasks/{task_id}/cancel
```

The console lives at `/projects` and `/projects/:projectId`.

## Operational notes

- The scheduler is restart-safe: it reconciles project tasks with existing
  threads/runs on startup.
- A failed dispatch records its error on the task; retrying reuses the task.
- Long worker turns stream into the thread transcript like ordinary chat.
- Cancellation stops the run and marks the task cancelled; dependencies of a
  cancelled task do not become ready.
