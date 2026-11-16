# p_python results

In-process Super-agent turn loop on langchain-core `BaseChatModel` interfaces.
No LangGraph graph, no sandbox, no PTC import. The injected
`ScriptedFakeChatModel` is adapted behind `ScriptedLangChainChatModel`
(`bind_tools` + `astream`); the turn loop never calls the scripted double
directly. Role tools go through `FAKE_TOOL_HANDLERS`.

Command:

```bash
SUPERAGENT_RESULT_DIR=/tmp/superagent-results-p-python \
  uv run pytest -p no:cacheprovider --disable-socket --allow-unix-socket \
  tests/prototypes/superagent -v
uvx --offline ruff@0.15.14 check prototypes/superagent/p_python
```

Suite: **55 passed, 24 skipped** (`n_pi` / `c_native` absent). Ruff: clean.
Every parametrized check that collected `p_python` passed.

## Per-check results (`p_python`)

| Check | Result |
|---|---|
| Role tool set (`router` / `supervisor` / `watchdog`) | PASS |
| Restart survival (store deep-copy + foreign row) | PASS |
| Offered tools == role list on every model round | PASS |
| Transcript records user / memory / assistant / done | PASS |
| Ordered per-chunk lockstep streaming | PASS |
| Credit gate stops before the model | PASS |
| Credit gate stops mid-turn after a tool round | PASS |
| Memory injected before the model call | PASS |
| Scripted role tools run and continue | PASS |
| No shell / file / CLI-builtin execution (`tool_refused`) | PASS |
| No real provider (`provider_name == "scripted"`) | PASS |
| Cold-start + LOC JSON | PASS |

## Cold start

From `/tmp/superagent-results-p-python/cold-start.json` (fresh interpreter,
import + construct + `start` for a Router session; harness bridge setup
subtracted):

| Field | ms |
|---|---|
| `cold_start_ms` | 1141.47 |
| `import_ms` | 1141.14 |
| `construct_ms` | 0.088 |
| `start_ms` | 0.23 |

Almost all cold start is import cost (`langchain_core` + adapter). Construct
and `start` are sub-millisecond once imported.

## LOC

From `/tmp/superagent-results-p-python/loc.json` (non-blank source lines in
`prototypes/superagent/p_python/` only; `RESULT.md` excluded):

| File | loc |
|---|---|
| `__init__.py` | 6 |
| `adapter.py` | 248 |
| `chat_model.py` | 123 |
| `messages.py` | 149 |
| `tools.py` | 69 |
| **total** | **595** |

## langchain-core interface

- `ScriptedLangChainChatModel(BaseChatModel)` wraps the contract double.
- The turn loop calls `self.chat.bind_tools(tools).astream(lc_messages)`.
- Chunks are `AIMessageChunk` values; tool requests come from
  `tool_calls` / `tool_call_chunks`.
- Dict conversion for the scripted backend is private to `chat_model.py`
  (`messages_to_scripted_dicts`), so a production `BaseChatModel` can replace
  the wrapper without changing the loop.

## Production providers

The prototype always refuses anything other than `provider_name == "scripted"`.
A production swap replaces `ScriptedLangChainChatModel` with a real
langchain-core chat model built the same way the server already does (via
`LLMService.complete` / the configured provider route), not by calling
`create_llm()` from a request path.

Provider groups currently declared in `src/llms/manifest/providers.json`
(`models` keys): `__auto__`, `anthropic`, `cerebras`, `dashscope`,
`dashscope-intl`, `deepseek`, `doubao-plan`, `gemini`, `gemini-oauth`,
`grok-oauth`, `groq`, `lm-studio`, `mimo`, `minimax`, `moonshot`,
`moonshot-coding`, `openai`, `openrouter`, `stepfun-plan`, `vllm`, `xai`,
`z-ai`, `z-ai-cn`. Those models already expose langchain-core
`BaseChatModel` surfaces (`astream` / `bind_tools`); this loop is shaped to
call that interface without a LangGraph agent graph.

## Failure / restart behaviour

- **Credit denial (preflight):** one `stop` event with the gate reason, one
  store commit with session `status=stopped`. No memory recall, no model call.
- **Credit denial (after a tool round):** `stop` + commit `stopped`; the model
  is not opened again.
- **Shell / file / out-of-role tool:** `tool_refused` with the tool name; no
  `FAKE_TOOL_HANDLERS` dispatch; the loop continues with a refusal message in
  the next model request.
- **Restart:** `transcript` / `session` / `events` read only the injected
  store. Closing the adapter, deep-copying the store, and building a new
  adapter on the copy restores role, history, and prior tool text into the
  next model request. Process-local subscriber events are not replayed. One
  turn is one store transaction (`commits` advances by 1).
- **Concurrent turn CAS:** commit fails if `next_seq` moved under the turn.

## Wiring to replace `astream_ptc_workflow` (supervisor + watchdog)

Today `src/server/app/threads/messaging.py` (`_handle_send_message`) resolves
credit, model config, and autonomy tool hosts, then always builds:

```python
gen = astream_ptc_workflow(
    request=..., thread_id=..., run_id=..., user_input=...,
    user_id=..., route=..., is_byok=..., config=...,
    dispatched=..., named_model=...,
    extra_configurable=..., extra_tools=...,
)
```

For the two Super-agent control-plane roles the extras are already selected:

| Role | When | Tools | Host |
|---|---|---|---|
| supervisor | `route.role == "supervisor"` | `AUTONOMY_ROUTER_TOOLS` | `AutonomyToolHost` |
| watchdog | `is_dispatch and platform == "superagent_watchdog"` | `AUTONOMY_WATCHDOG_TOOLS` | `AutonomyToolHost` |

Minimum wiring to swap those two onto this loop (workers stay on PTC / native
CLI):

1. **Branch before `astream_ptc_workflow`** when
   `route.role == "supervisor"` or
   `request.platform == "superagent_watchdog"`.
2. **Map doubles → product services:** session/transcript/events → the
   conversation / run ledger (or a thin Postgres store with the same
   `transaction` / `next_seq` CAS shape); `FakeCreditGate` → the existing
   `enforce_credit_limit` decision reused per model round; `FakeMemoryRecall`
   → the autonomy recall path; `EventSubscriber.publish` →
   `RunSSEProducer` / the Redis stream outbox so reconnect still works;
   `ScriptedLangChainChatModel` → the real langchain-core `BaseChatModel`
   from `config.llm` through `LLMService` (`bind_tools` + `astream`).
3. **Tool dispatch:** replace `FAKE_TOOL_HANDLERS` with
   `AutonomyToolHost` methods for the role list only; keep the same refuse
   path for shell/file/CLI builtins and out-of-role names.
4. **SSE contract:** translate `token` / `tool_call` / `tool_result` /
   `tool_refused` / `stop` / `done` into the chat-wire events
   `sse_producer` already emits, and keep each type’s replay home registered.
5. **Dispatch priming:** return the same generator shape `_prime_dispatch`
   expects (async iterator of SSE frames), so admission, run ledger, and
   multi-worker ownership stay unchanged.
6. **Do not** boot `build_ptc_graph_with_session`, a sandbox, or PTC
   middleware for these two roles; leave `superagent_worker` on the existing
   PTC / native path.

## Risks

- **Import weight:** cold start is dominated by `langchain_core` (+ contract →
  autonomy tool import). A per-process import cache helps; a fresh worker
  still pays ~1.1 s in this measurement.
- **SSE / replay gap:** the prototype’s event kinds are not the production
  chat-wire vocabulary. A naive map will break reconnect and
  checkpoint-projected replay unless every new frame has a registered home.
- **Credit semantics:** product credit is checked once before
  `astream_ptc_workflow` today; this loop re-checks every model round. Mid-turn
  denial must stamp the same pause / stop surfaces the credit gate already
  uses, or billing and UI diverge.
- **Tool host side effects:** fake handlers are pure echoes. Real
  `AutonomyToolHost` tools mutate tickets and threads; refusing shell/file is
  mandatory or the slim loop becomes a bypass of the sandbox boundary.
- **Store shape vs LangGraph checkpoint:** production history still lives in
  the LangGraph checkpoint + conversation tables. This adapter’s
  Postgres-shaped store is a contract stand-in; dual-writing or replacing the
  checkpoint without a migration plan loses restart survival for live threads.
- **Multi-worker:** subscriber state is process-local here. Production must
  keep truth in Postgres / Redis (run ledger, stream buffer), not in adapter
  memory, or a different worker cannot resume the turn.
- **Scope creep:** wiring only supervisor/watchdog is intentional. Pulling
  workers onto this loop would drop sandbox, native CLI adapters, and PTC
  tool policy unless those are re-homed first.
- **BaseChatModel.astream extras:** langchain may emit a trailing empty
  `chunk_position=last` chunk; the loop ignores empty non-tool chunks so they
  do not become transcript rows.
