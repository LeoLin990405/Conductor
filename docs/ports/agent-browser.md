# Managed local browser, web routing, and skill hub

Status: completed for the offline scope. Related work items: **t2901u** native
browser commands, **t2902u** browser-first web routing, and **t2903u** unified
skill packaging and installation.

## Features

- `conductor browser` owns a private, persistent Chromium profile per agent.
  Its native command registry returns JSON Schemas; hostname-matched adapters
  provide bounded semantic page reads. The included `docs` and `catalog`
  adapters are samples for reserved `*.example.test` fixtures.
- Page evaluation and network capture require `--enabled` on each call.
  Evaluation returns result metadata only. Opted-in evaluation can still
  cause page-side effects or requests, while its returned value is suppressed.
  Network capture returns only method, origin, resource type, and status,
  without URL paths, queries, headers, or bodies. Login-state import filters
  to exact user-specified domains.
- `conductor web search/read` uses the current agent's isolated browser first.
  Search HTML is fetched only after a classified browser availability or
  timeout failure; extracted result URLs are then read browser-first. Direct
  fetch is explicit for `--raw` or `--api`, automatic for `.json` and known raw
  suffixes, and a fallback only for classified browser availability/timeouts.
  Binary file responses are returned as base64.
- The single `skills/agent-browser/SKILL.md` source is bundled in the wheel.
  `conductor skills-hub list/install` uses allowlisted Claude, Codex, Cursor,
  OpenCode, and `.agents` adapters. Existing skill destinations are preserved.
  The shared main and subagent prompt policies prefer each current agent's own
  isolated background browser or matching adapter after search.

## Provenance and files

This is Conductor-owned Python and documentation. The read-only context source
was `/Users/you/conductor-wt/legacy-survey/docs/MIGRATION-FROM-LEGACY.md`.
The command-discovery design is OpenCLI-inspired;
there is no external OpenCLI executable/runtime dependency, including
`~/.local/bin/opencli`. No OpenCLI or Ego-Lite source was copied, so no
third-party port attribution applies.

Implementation lives in `src/conductor_cli/browser/` and
`src/conductor_cli/skills_hub/`. The owner/transport pair manages the local
process and socket; the driver, reference registry, service, and adapters
implement page operations; `web.py` adds search/read routing; `install.py`
uses the hub catalog's safe destination map. The skill source is
`skills/agent-browser/SKILL.md`. Design details are in
[`docs/design/agent-browser.md`](../design/agent-browser.md).

## Verification and limits

The final combined socket-blocked suite passed **413 tests in 5.56 seconds**.
The exact command was run from the worktree root:

```sh
env -i \
  PATH=/usr/bin:/bin:/opt/homebrew/bin \
  HOME=$PWD/.pytest_cache/agent-browser-review/home \
  TMPDIR=$PWD/.pytest_cache/agent-browser-review/tmp \
  XDG_CONFIG_HOME=$PWD/.pytest_cache/agent-browser-review/config \
  XDG_CACHE_HOME=$PWD/.pytest_cache/agent-browser-review/cache \
  PYTHONPATH=$PWD/src:$PWD \
  PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 \
  LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false LANGCHAIN_TRACING=false \
  /Users/you/Documents/deepseek-harness/default-workspace/AlphaCN/.venv/bin/python \
  -m pytest --disable-socket --allow-unix-socket \
  --basetemp=$PWD/.pytest_cache/agent-browser-review/final-commit-cases \
  tests/unit/conductor_cli/browser \
  tests/unit/test_browser_install.py \
  tests/unit/test_skills_hub.py \
  tests/unit/test_conductor_cli.py \
  tests/unit/test_conductor_cli_operations.py \
  tests/unit/conductor_cli/test_github.py \
  tests/unit/test_provider_breaker_cli.py \
  tests/unit/ptc_agent/agent/prompts \
  tests/unit/prompts \
  -q --tb=short
```

Ruff passed for the touched Python files; `git diff --check` passed; the
Skill Creator validator reported the skill valid. CLI subprocess smoke checks
for `browser commands`, `browser help`, and `skills-hub list` returned success
envelopes without starting a browser or backend. `eval` without `--enabled`
returned an invalid-input envelope and exit status 2. A focused prompt rerun
after the final prompt wording change passed **255 tests**.

Tests use fake drivers, blocked network sockets, and worktree-local temporary
paths. AF_UNIX is allowed for asyncio and local browser-control tests. No real
home installation, Chromium, live-site, provider, backend, or database
acceptance was performed. Wheel packaging was checked through the existing
force-include mapping and source-resolution tests; a wheel was not built.

During staged implementation, three in-progress web tests failed and were
fixed before the final combined run. A separate reviewer run initially used
`--disable-socket` without `--allow-unix-socket`, which blocked asyncio's local
socketpair and caused fixture setup errors; the corrected isolated run passed
30 tests. Neither event was a real-provider failure.
