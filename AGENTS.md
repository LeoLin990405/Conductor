# Conductor

Conductor is a self-hosted multi-agent collaboration platform. Its product surface is the **Super-agent**: a Router turns incoming work into Tickets, Workers implement them on line worktrees, a review queue vets the result, and merge lands it. One FastAPI backend (`src/server/`), a React web app (`web/`), an Electron desktop shell (`desktop/`), and CLIs (`conductor`, `ptc-agent`).

> Single source of truth for AI coding agents. `CLAUDE.md` imports this via `@AGENTS.md`; Codex/Cursor/Copilot read it directly. Edit here, not there.

## Super-agent pipeline (Router → Ticket → Worker → review → merge)

All of this lives in `src/server/services/autonomy/`.

- **Router** (`router.py`): one hidden Home thread per user and shard, `platform` `superagent_router`. It routes — it never implements. Its turns go through the same `/api/v1/threads` dispatch and admission path as manual threads.
- **Tickets** (`intake.py`, `models.py`): the unit of work. Created from chat, channels and sources (GitHub, Feishu, Slack, email), or `conductor tickets create`. The single-writer scheduler (`scheduler.py`, one Postgres advisory lock per deployment) batches evals, dispatches Tickets and recovers assignments.
- **Workers** (`worker.py`, `platform` `superagent_worker`): implement one Ticket on a line worktree. Two runtimes: the in-server agent thread, and **native CLI workers** (`src/llms/native_cli/`) chosen by the ticket's `worker_provider` — one adapter per provider CLI (claude-code, codex, kimi, deepseek, glm/zcode, doubao, grok, minimax, stepfun, cursor, …), registered in `registry.py`. Failure handling is an eight-class vocabulary with per-provider rules, deterministic context-window session rotation, redacted handoffs, and per-worker exit codes; see **the internal document `WORKER-HEALTH` (not published)**.
- **Review & merge** (`review_merge.py`): an operator review queue (a line never reviews itself; the reviewed commit is pinned) plus a mandatory automated review gate. Merge rehearses in a throwaway worktree, runs the tests there, lands only the rehearsed commit via compare-and-swap `git update-ref`, and is reachable only from loopback.

## Common Commands

```bash
make up                               # full stack (postgres, redis, backend, frontend)
make config                           # interactive setup wizard (LLM, sandbox, search/fetch, storage, infra)
make help                             # all targets (config-check, doctor, test-sandbox, …)

uv run python server.py --reload      # backend only, port 8000 (needs DB + Redis)
cd web && pnpm dev                    # frontend dev server, port 5173

make lint                             # Ruff (backend) + ESLint (frontend)

# Backend tests — default run is unit only (integration/slow/regression deselected in pyproject)
uv run pytest tests/unit/ -v --tb=short
uv run pytest -m integration          # hits real APIs — needs DB + Redis + API keys

cd web && pnpm test                   # Vitest;  pnpm test:e2e = Playwright;  pnpm typecheck = tsc -b
```

## Layout

| Path | Purpose |
|---|---|
| `src/server/` | FastAPI app: routers (`app/`), autonomy services, models, database (raw psycopg3, no ORM; Alembic migrations use raw SQL). Contracts in **`src/server/AGENTS.md`**. |
| `src/ptc_agent/` | The agent runtime the Router thread and in-server Workers run on: middleware stack, prompts, sandbox and MCP integration. |
| `src/llms/native_cli/` | Native CLI worker adapters: process lifecycle, sessions, failure classification, isolation (`isolation.py`). |
| `src/conductor_cli/` | The `conductor` CLI (pyproject script): tickets, workers, review/merge, daemons, usage, memory, auth. |
| `libs/ptc-cli/` | The `ptc-agent` CLI (optional `cli` extra). `ptc-agent --super-agent` opens the Super-agent TUI (`ptc_cli/superagent/`); plain `ptc-agent` is the legacy chat loop, slated for removal. `docs/COND.md` covers the `cond` launcher. |
| `src/tools/`, `src/llms/`, `src/utils/` | Direct LangChain tools (`src/tools/AGENTS.md`); LLM wrappers, token counting, model manifest (`src/llms/manifest/models.json`); Redis cache and shared utils. |
| `plugins/` | Built-in MCP plugin bundles (`conductor_deliverables`, `conductor_service` — skills today). |
| `web/`, `desktop/` | React 19 + Vite + TypeScript + Tailwind frontend (**`web/AGENTS.md`**); Electron desktop shell, no web bundle, entry URL only (**`desktop/AGENTS.md`**). |

## Computers, sandboxes & MCP plugins

Hierarchy: **User → Computer → Workspace → Thread → Turns**. A computer owns one Daytona, E2B or Docker sandbox, or is a machine the user brings over SSH — the worker runtime is bootstrapped without root under `~/.conductor`, and a machine the server cannot dial connects out through the tunnel gateway (`src/server/ssh_gateway/`). The sandbox recipes (Python and Node package lists, Playwright pin, image steps) live in `src/ptc_agent/core/sandbox/_defaults.py`, `providers/_image_recipe.py` and `providers/_ssh_bootstrap.py`, mirrored by hand in the root `Dockerfile.sandbox`; `tests/unit/core/sandbox/test_dependency_pins.py` pins that mirror.

**MCP plugins** (`plugins/README.md`): built-in MCP servers and skills ship as Agent Plugins 1.0.0 bundles — the same format a user uploads on the Plugins page; `mcp.json` is closed (`additionalProperties: false`), vendor metadata lives in `plugin.json` under `extensions["ai.conductor"]`. The model reaches MCP tools through generated Python wrappers executed in the sandbox (`execute_code`); a per-tool policy can bind a tool to the direct JSON-tool path instead (`src/server/services/tool_binding.py`). Third-party (untrusted) stdio servers launch via pinned `uvx`/`npx`, never the shared venv.

## Worker rules

Rules for every agent working in a Conductor worktree (dispatch Workers, Ticket Workers, reviewers). They come from failures in this repo; see the internal planning note `lessons-external-scheduler-fixes` (not published).

- **Never `git stash`.** The stash is shared by every worktree of the clone and has mixed up other agents' work. Commit work in progress on your own branch instead.
- **Never chain a rebase and the tests** (`git rebase …; pytest`). A half-finished rebase reported a false full pass once. Prefer merging main into your branch, and check `git status` before running gates.
- **One venv per worktree**, made with `uv sync --frozen --extra test --extra cli` in that worktree. Never symlink or reuse another checkout's `.venv`, and never run `uv` in a deployed checkout.
- **Tests write only to tmp.** A test that creates or edits files under `src/` (stub modules, fixtures) fails review.
- **Commit what you report.** If you say the tests pass, the work they test is committed on your branch before you exit.
- **Quote shell variables.** The operator shell is zsh, which does not split `$var` on spaces; use arrays or `bash -c` for word lists.
- **Do not push**, and never review your own code.

## Conventions

- **Python 3.13+, async-first**; Ruff for linting. **Package managers**: `uv` (Python), `pnpm` (web).
- **Config split**: `.env` for credentials/URLs, `config.yaml` / `agent_config.yaml` for behavior. `APP_ENV` layers a gitignored sibling YAML over each file (nested maps merge, lists replace), merged in `src/ptc_agent/config/file_utils.py` so every reader gets it.
- **Server-side LLM calls** go through `LLMService.complete`, never `create_llm()` directly (skips BYOK/OAuth/per-user prefs) — contract in `src/server/AGENTS.md`.
- **⚠️ Multi-worker server**: the backend runs `--workers N` — a request, its SSE consumer, the outbox drainer and the recovery scanner may land on different processes. Truth lives in Postgres (run ledger + advisory locks); Redis is coordination/transport. Read `src/server/AGENTS.md` § Multi-worker before touching turn lifecycle, streaming or worker ownership; verify with `scripts/multiworker_gate/`.
- **⚠️ Tool docstrings are prompt surface**: every agent-facing tool docstring, direct or MCP, follows `src/tools/AGENTS.md` and `mcp_servers/AGENT_CONTRACT.md` — the model reads it at call time, so don't reword one as a side effect.
- **Docstrings explain *why*, not *what***; verify with real calls first, and pin settled contracts with tests last.
