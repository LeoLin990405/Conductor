# 0003 — Conductor current state and depth gaps

Evidence for this inventory: repository at `main = 12d17232` (the deep refactor
branch is cut from it), plus the running M2 app stack (backend/frontend) and the
Homelab deployment. Commands used are listed at the end.

## System shape

Hierarchy (from `AGENTS.md`): **User → Computer → Workspace → Thread → Turn**.
A computer owns a sandbox (Daytona/E2B/Docker or SSH machine); each workspace is
a folder on it; a thread is a conversation and its turns. Two agent roles:
`analyst` (inside a workspace) and `chief_of_staff` (work between workspaces),
selected per turn by `turn_runtime.turn_route`. Desktop is a remote-URL Electron
shell; the M2 app stack runs backend on 8000 and frontend on 5173.

## Agent layer

Exists today:

- **Runtime**: `create_agent()` from `langchain.agents` in
  `src/ptc_agent/agent/agent.py` (`PTCAgent.create_agent`), with a custom
  middleware stack (compaction, file operations, steering, ask_user, todo
  operations, skills, background subagent, runtime context, provider caching…)
  and LangGraph checkpoints (`graph.py`, `main_state.py`).
- **Roles**: `agent/roles.py` (`analyst`, `chief_of_staff`), role prompts and
  chief-of-staff coordination tools (`src/tools/secretary/`), activity baseline.
- **Subagents**: `agent/subagents/` (definition/assembly/compiler/merge/registry),
  built-ins (`research`, `general-purpose`, `report-builder` and two others),
  `memory=True` per definition, background orchestration in
  `agent/middleware/background_subagent/`; persistence in
  `subagent_tasks` + `subagent_runs` (migration 020) with a one-live-run slot,
  predecessor chain and credit ledger (`src/server/database/runs/`).
- **Task/todo tracking**: TodoWrite middleware emits `todo_update` SSE events
  (`agent/middleware/todo_operations/`); `taskStatusUi.tsx` renders subagent
  task status in the web app.
- **Progress/checkpoint substrate**: `turn_slices` / `task_run_slices`
  (migration 064), `thread_transcripts` (056), `replay_facts` (065),
  `provenance_records`, `hook_outbox`, `wakeups`.
- **HITL**: `AskUserMiddleware` uses LangGraph `interrupt()`/resume
  (`agent/middleware/ask_user.py`); credit gate, repeat guard and sandbox policy
  exist as separate checks. This build has no external transaction capability: the
  related governance, ledger and permission were removed.

Depth gaps (mapped to the adoption matrix):

| Gap | Evidence | Matrix |
| --- | --- | --- |
| No first-class goal/stop-condition entity | No goal tables/prompts; thread ends implicitly | AG-01 |
| No independent verifier at stop time | Final message is the only completion signal | AG-02 |
| Subagent contract implicit (inputs/tools/context budget/result schema/verification) | `definition.py` fields + prompts + `merge.py`, no contract version/result schema persistence | AG-03 |
| Deterministic workflow definitions missing | `backends/workflows.py` + `workflowRunUi.tsx` exist, no versioned definition/runner | AG-04 |
| Permission modes not unified | ask_user + scattered policies; no per-thread mode | AG-06 |
| Context assembly not auditable | baseline index frozen at turn start; compaction/slices exist but no per-turn injection record | AG-07 |
| Distillation path absent | skills exist (`user_skills`, skills middleware); no run→skill proposal | AG-05 |

## Memory & data layer

Exists today:

- **Long-term memory**: LangGraph `BaseStore` namespaces
  `(user_id, "memory")` and `(user_id, "workspaces", workspace_id, "memory")`
  (`backends/langgraph_store.py`), rendered as a frozen per-thread baseline
  index (`middleware/runtime_context/baseline.py`, `MEMORY_INDEX_FILENAME`),
  read on demand by the agent; subagents read memory and may only propose
  entries (`prompts/templates/subagents/memory.md.j2`); read-only HTTP API
  `GET /api/v1/memory/user`, `GET /api/v1/memory/workspaces/{id}` (+`/read`)
  (`src/server/app/memory.py`).
- **Memo store**: user-uploaded docs, agent read-only, with PDF extraction and
  metadata (`agent/memo/`, `server/app/memo.py`, `services/memo_binary_storage.py`).
- **Scratchpad**: opt-in per-thread notes folder with checkpoint notes
  (`middleware/compaction/notes.py`).
- **Transcripts**: rendered JSONL per thread from checkpoints
  (`agent/transcript/`, `services/transcripts.py`, `thread_transcripts`).
- **Artifacts/files**: `workspace_files` / `workspace_file_blobs`, provenance
  records (`provenance_records`, `provenance_result_bodies`).

Depth gaps:

| Gap | Evidence | Matrix |
| --- | --- | --- |
| Only user/workspace tiers; no session/task/project tier | namespace functions expose two tiers | MEM-01 |
| No retrieval index/budget accounting/injection audit | baseline index is a rendered file list frozen at turn start | MEM-02 |
| Memory API read-only; no proposal/review rows | `server/app/memory.py` only GET | MEM-03, MEM-04 |
| No memory quality measurement | no eval set or metrics | MEM-05 |
| Entity relations/lifecycle undocumented | tables exist across 85 migrations; no ER/contract doc | DATA-01 |
| No single event stream for timeline/audit | provenance/hook_outbox/replay_facts cover parts | DATA-02 |
| No retention/export/deletion contract for memory/transcripts/artifacts | deletion code exists per store; no unified contract | DATA-03 |

## Frontend layer

Exists today:

- Chat workspace with ~122 components under `web/src/pages/ChatAgent/components/`,
  including `ChatRightPanel` with only `file | detail | preview` surfaces
  (`chatView/panelModules.ts`), `FilePanel`, `DetailPanel`, viewers,
  `ChatMinimap`, `ThreadSubagentsBanner`, `delegatedWork`, `taskStatusUi`,
  workflow run UI, mobile bottom sheets and drawers.
- Sidebar/session navigation (`components/Sidebar/AppSidebar.tsx`), chat header
  and composer stack (`chatView/ChatHeader.tsx`, `ChatComposerStack.tsx`).
- Design system: `DESIGN.md` ("Quiet Workspace"), tokens enforced by
  `styles/__tests__/tokenRefs.test.ts`; i18n zh-CN/en-US with key parity tests.
- Settings, Setup wizard, Dashboard, Automations, Plugins, OAuth callback pages.

Depth gaps:

| Gap | Evidence | Matrix |
| --- | --- | --- |
| Right panel is a fixed 3-way union, not a module registry | `panelModules.ts` lazy imports only three | FE-01 |
| No session tabs/pin/batch/global search/AI naming | sidebar lists threads only | FE-02 |
| No plan/goal/permission affordances at composer | composer stack exposes model/fast mode | FE-03 |
| No review state (per-turn/file diff accept/reject) | file preview + detail tool-call views only | FE-04 |
| Activity not a first-class filterable timeline/waiting queue | subagent banner + task status rows | FE-05 |
| No command palette / global keyboard navigation | no palette component | FE-06 |
| Terminal/browser not first-class panel modules | sandbox execution exists; preview proxy exists; no panel contract | FE-07 |

## Desktop & deployment

- `desktop/`: Electron remote-URL wrapper (`appOrigin`, URL classification,
  OSS server picker, outage screen), tested by `node --test` in `desktop/test/`.
  **Keep.**
- Homelab: rootless Docker deployment documented in the maintainer's homelab deployment (not published)
  (backup, image revision labels, rollback). **Keep**; M2 app runs a dev compose
  stack. New behavior reaches users only after Phase 4 and explicit approval.

## Evidence commands

```bash
git -C Conductor log --oneline -1                 # main = 12d17232
rg -n "class PTCAgent|create_agent" Conductor/src/ptc_agent/agent/agent.py
rg -n "BUILTIN_SUBAGENTS|memory=True" Conductor/src/ptc_agent/agent/subagents/builtins.py
rg -n "subagent_tasks|subagent_runs" Conductor/migrations/versions/020_subagent_run_ledger.py
rg -n "user_memory_namespace|workspace_memory_namespace" Conductor/src/ptc_agent/agent/backends/langgraph_store.py
rg -n "router.get" Conductor/src/server/app/memory.py
ls Conductor/web/src/pages/ChatAgent/components | wc -l
rg -n "lazy|import" Conductor/web/src/pages/ChatAgent/components/chatView/panelModules.ts
```

## What this means for Phase 1

The existing substrate is large enough that Phase 1 is **additive**: extend
`subagent_runs` with a contract/result payload, add goal/plan and event tables,
add a memory-entry projection over the existing store, and document the ER model.
No table rewrites and no runtime replacement are needed to start.
