# Architecture

Conductor is a multi-agent collaboration and aggregation operating platform.
The web console and desktop shell are thin clients; the FastAPI server owns
projects, tasks, agent threads, workspaces, files, memory, plugins and model
routing.

How Conductor, memfw, the hubs and the M2 conversation store fit together so every agent and agent app can collaborate: the internal planning note `north-star-collaboration` (not published) (Chinese).

## Layers

```text
Web / Desktop shell
  ├── Chat console
  ├── Projects console
  ├── Memory / Automations / Plugins / Settings
  └── Shared chat routes
        │ HTTP / SSE / WebSocket
        ▼
FastAPI server (src/server)
  ├── projects: scheduler, dispatcher, recovery, task dependencies
  ├── chat handlers: PTC runs, native CLI runs, HITL, steering
  ├── workspaces: files, blobs, livefs, sharing
  ├── memory, automations, wakeups, notifications
  ├── MCP catalog, egress relay, tool binding
  └── auth, BYOK/OAuth, usage accounting
        │
        ▼
Agent runtime (src/ptc_agent)
  ├── PTC agent (LangGraph + tools + subagents)
  ├── native CLI adapters (Codex, DSH, Kimi, MiniMax, MiMo, Grok, StepFun, ZCode)
  └── sandbox providers (Daytona, E2B, Docker, local SSH)
        │
        ▼
PostgreSQL · Redis · sandbox runtime
```

## Project orchestration

A project is a durable plan over one workspace. It holds a goal, a concurrency
limit, task dependencies and a lifecycle state. The scheduler creates one agent
thread per ready task and dispatches it through the same chat path a user would
use. Workers write below `.orchestrator/projects/<project>/<task>/` so parallel
tasks cannot overwrite each other. Pause, cancel and restart recovery are
project-level operations; a task records attempts, thread id, run id and error.

See [CORE-ORCHESTRATOR](CORE-ORCHESTRATOR.md) for the operational contract.

## Agent runtimes

- **PTC worker** — the built-in agent: Python code execution in a sandbox, MCP
  tools, file and web tools, subagents, HITL approvals.
- **Native CLI worker** — a vendor CLI runs the turn in its own session and
  cache. Conductor owns the thread, stream projection, cancellation and
  workspace transport.

Both are represented as agent threads, so the console reads one transcript
shape regardless of runtime.

## Model plane

Model manifests declare providers and models; the resolver chooses a client
from deployment config, BYOK keys, OAuth subscriptions or native CLI auth.
Fallbacks, token accounting and reasoning/effort settings are resolved per run.

## Data and state

- **PostgreSQL** — users, workspaces, threads, runs, projects, tasks, memory,
  automations, MCP catalog/grants, OAuth tokens.
- **Redis** — streams, locks, cache, in-flight state, pub/sub.
- **Workspace files** — per-workspace file blobs, live workspace filesystem and
  project worker folders.
- **Sandbox** — isolated execution for code and MCP tools.

## Security boundaries

- Remote MCP servers go through an egress relay with a per-connection grant.
- Tool bindings decide sandbox wrapper vs direct tool call.
- HITL approvals gate sensitive actions; the project scheduler inherits the same
  approval path.
- Secrets live in the vault or encrypted provided-key rows; tokens are never
  sent to the browser.
