# Native Runtime Routing

Status: **Phase 1 implemented and wired into thread creation.**

The native runtime is bound to a thread for its lifetime. The vendor CLI owns
the prompt, tool loop, session and prompt cache, so Conductor cannot swap
providers in the middle of a native turn. Routing therefore decides **before a
thread is created**, never inside or after a turn.

## What exists

```text
POST /api/v1/native-agents/route
```

Read-only: it does not create a thread, call a model, or invoke a CLI.

Request:

```json
{
  "task": "重构这段代码并跑测试",
  "required_capabilities": ["approvals", "mcp"],
  "preferred_provider": "codex",
  "allow_ptc": true
}
```

Response:

```json
{
  "runtime": "codex",
  "reason": "explicit preference codex is eligible, meets: approvals, mcp",
  "fallbacks": ["ptc"],
  "confidence": 0.85,
  "scores": {"codex": 1.18, "deepseek-dsh": 0.92}
}
```

## Policy

1. Only `installed && live_verified` providers are candidates.
2. Every `required_capabilities` entry is a hard gate.
3. An explicit eligible `preferred_provider` wins.
4. Otherwise base provider priority plus a small task-affinity score:
   - coding/refactor/debug/test → Codex;
   - Chinese-language requests → DSH;
   - all providers share the same capability gate, so affinity cannot
     override a missing approval/stream requirement.
5. If no native provider is eligible, return PTC with the concrete missing
   capability in `reason`.
6. A preferred provider that cannot meet the required capabilities does **not**
   silently become another provider: the decision says which capability was
   missing. `allow_ptc=false` returns `runtime=none` rather than inventing one.

ZCode is deprioritised (read-only one-shot) and never selected by default when
another live provider is eligible.

## Deliberate non-goals

- No provider switch inside a live native thread.
- No hidden classifier prompt.
- No fallback that changes runtime after the first token: the caller must pick
  a decision before creation.
- No claim that every provider has token-level streaming or approvals; the
  decision scores only the capabilities the registry actually reports.

## Thread creation `auto`

`POST /api/v1/threads` accepts:

```json
{
  "workspace_id": "...",
  "first_query": "refactor this code and run tests",
  "native_provider": "auto",
  "routing_capabilities": []
}
```

The backend resolves the decision before creating the row, binds the resolved
provider (or PTC when the decision is PTC), returns it as `native_provider`,
returns `runtime_decision`, and stores the decision under
`metadata.runtime_decision` together with the immutable
`metadata.native_runtime` binding when native.

Live result on this host:

```text
"refactor this code and run tests"  -> codex
"帮我梳理一下今天的工程进展"           -> deepseek-dsh
"你好，随便聊聊"                     -> PTC (no native signal)
```

The same `auto` sentinel is handled by the legacy `POST /threads/messages`
first-message door in `request_prep.ensure_thread`.

## Automation runs

An automation's typed `native_provider` may be `auto`. The binding is resolved
**per run** (not when the automation is saved), because each new-thread run can
have a different instruction and workspace. The execution-time pre-create:

- routes `instruction` with the same capability/affinity router;
- writes `metadata.runtime_decision` and, when native, the immutable
  `metadata.native_runtime` binding;
- leaves a PTC decision with only the decision record.

The automation row keeps `auto` as a sentinel; validation only requires that at
least one installed, live-verified native provider exists on the host.

Live on this host (2026-10-28): manual trigger of an `auto` automation with
instruction "refactor this code and run tests" created thread
`616ffdb5-...` bound to `native_runtime.provider=codex`, with
`runtime_decision.reason="codex is the best eligible native runtime (task
affinity +0.18)"`; the executor started the native Codex run.

## Decision telemetry

`GET /api/v1/native-agents/routing/decisions?limit=20`

Reads recent threads whose metadata contains `runtime_decision`, joined with
the immutable `native_runtime` provider. This is evaluation telemetry only:
the decision is not recomputed and the binding is not changed. Archived or
deleted workspaces are excluded.

## Next

1. Apply fallbacks only when a provider fails **before** the first turn; after
   a native session starts, use thread fork/handoff rather than silent switch.
2. Phase 2 (partial): the main-agent `NativeAgent` tool can delegate to a
   native provider; `provider="auto"` uses this router. It runs one isolated
   task in a private cwd and returns one answer. A full orchestrator still
   needs nested cancellation/HITL/provenance and MCP/memory/skills injection
   for the delegated call.
