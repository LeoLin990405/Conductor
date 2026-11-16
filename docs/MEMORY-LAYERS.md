# Memory layers

Conductor's memory bank has three durable layers and one working state. The
canonical bytes stay in the LangGraph Store; `memory_entries` is the retrievable
projection and `memory.md` is each layer's index.

```
User (global)
  └─ Project / Workspace (this research project)
       └─ Session (this conversation)
```

| Layer | Store namespace | Mounted path | Scope | Injected? | UI |
|---|---|---|---|---|---|
| **User** | `(<user>, "memory")` | `.agents/user/memory/` | Every workspace and thread | Yes | 用户记忆 |
| **Project / Workspace** | `(<user>, "workspaces", <workspace>, "memory")` | `.agents/memory/` | One research project (workspace); its threads share it | Yes (current workspace) | 项目记忆 |
| **Session** | `(<user>, "threads", <thread>, "memory")` | `.agents/session/memory/` | One thread only; mounted only while that thread runs | Only when that thread is the turn's thread | 会话记忆 |
| **Task** | — | task note / scratchpad | Current task | Not durable memory | Not shown |

A **workspace is the project** in Conductor: there is no separate project entity,
so the database tier remains `workspace` while the UI calls it 项目记忆 /
Project memory. `project`, `session` and `task` are still accepted tier names
for compatibility; only `user`, `workspace` and `session` have store
namespaces.

Task progress is not a memory tier. It belongs in `agent.md`, the task note or
the scratchpad; only durable facts that should shape future work enter memory.

## Write and retrieval rules

- User and project indexes are part of the turn's memory background.
- Session memory is mounted per thread and its index can be selected only for
  that thread's turn; it is never injected into another thread. The agent can
  also read it on demand from `.agents/session/memory/`.
- If a turn carries a thread id, a session index can be selected for that turn
  only; no thread id means no session candidates.
- `proposed/*` candidates are never active memory until approved; approval
  promotes them to the root of the user tier.
- Search/ranking is FTS + CJK trigram, plus optional API embeddings (see
  [MEMORY-EMBEDDINGS](MEMORY-EMBEDDINGS.md)).
- Detail frontmatter may carry `kind`, `importance`, `confidence`,
  `valid_from`, `valid_until`, `tags`, `entities`, `supersedes`, `source`.
  Expired or not-yet-valid entries are excluded from context; the inspector
  shows the rest. The inspector's lineage view reads the `supersedes` /
  superseded-by chain. See [MEMORY-RESEARCH](MEMORY-RESEARCH.md) for the
  rationale and iteration status.

## APIs

```text
GET  /api/v1/memory/user
GET  /api/v1/memory/workspaces/{workspace_id}
GET  /api/v1/memory/threads/{thread_id}
GET  /api/v1/memory/index?tier=&q=&thread_id=
GET  /api/v1/memory/summary?workspace_id=&thread_id=
GET  /api/v1/memory/lineage?tier=&key=&workspace_id=
POST /api/v1/memory/forget   { tier, key, workspace_id?, thread_id? }
```

Every read proves ownership from the credential; the workspace/thread ids in
the path are never trusted as identity.

## Projection mapping

`memory_dual_write.memory_tier_for_namespace` maps the three namespaces to
`user`, `workspace` and `session`; `session` rows carry the thread id in
provenance. The backfill can project user/workspace namespaces; session rows are
written by the same dual-write facade while a thread runs.

## Future: project groups

If Conductor later introduces a project entity that groups several workspaces, add
a `project` store namespace above `workspace` rather than overloading either
existing tier. The UI layer then becomes User → Project → Workspace → Session.
