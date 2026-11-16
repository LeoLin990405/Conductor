# Agent fleet alternate branch review fixes

This branch closes five review gaps in the read-only fleet scan and CLI. It
continues the port documented in `agent-fleet.md`. Predecessor context and
migration sources are recorded there: `legacy-survey/docs/MIGRATION-FROM-LEGACY.md`
§2–3, `model-farm-handoff` fleet tooling, the host's launcher conventions, and
the `codexbar_usage.py` sanitization contract. Rounds 5–6 in
`agent-fleet-review-fixes-5.md` and `agent-fleet-review-fixes-6.md` give the
sanitizer history.

## Latest review causes and fixes

1. **Short prefixed PEM lines could leak.** The sanitizer now recognizes an
   opaque payload after common timestamp and level prefixes. Regression coverage
   spans 10 prefix forms and four 8/16/19-character or title-case payloads.
2. **Redaction could erase scan signals.** Bare `ERROR` and `FAILED` are kept
   visible before an orphan PEM end marker. The scanner derives error state and
   test counts from the bounded raw tail, then returns only the sanitized log
   tail and parsed scalar fields; raw log text is not retained in the response.
3. **Process ownership relied on loose path matches.** CWD and command hints now
   use canonical, component-aware containment; the deepest nested worktree wins.
   An explicit external cwd cannot fall back to a positional path elsewhere.
4. **JSON bypassed CLI filtering and sorting.** Table and JSON now share one
   selection/order pipeline, while JSON preserves raw agent fields and envelope
   metadata.
5. **CLI numeric order differed from the dashboard.** Elapsed time and commits
   now sort descending, with unknown values last; substring filters ignore case.

The sanitizer is deliberately conservative: a one-word display line that looks
like a PEM body may be redacted with a nearby key marker. The dashboard does not
claim to preserve all such one-word log text.

## Local validation

The final combined run passed **327 tests in 3.28 seconds**, including the
explicit external-cwd ownership guard. Ruff on all six changed Python files and
`git diff --check` passed.

```sh
PYTHON_DOTENV_DISABLED=1 PYTHONDONTWRITEBYTECODE=1 TMPDIR="$PWD/.tmp" \
  ../../Documents/deepseek-harness/default-workspace/AlphaCN/.venv/bin/python \
  -m pytest \
  tests/unit/server/utils/test_sanitize_agent_logs.py \
  tests/unit/server/services/test_agent_scanner.py \
  tests/unit/server/app/test_agents_fleet.py \
  tests/unit/test_conductor_cli_fleet.py \
  tests/unit/config/test_agent_fleet_config.py \
  tests/unit/test_conductor_cli.py \
  tests/unit/config/test_core_config.py \
  tests/unit/config/test_loaders.py \
  -q --tb=short --basetemp="$PWD/.tmp/root-review-final"
```

The supplied fallback interpreter was used because this worktree has no local
`.venv`. An interim broad-exception/scan-preserving approach was rejected in
review and is not in the final implementation.

Changed sources are `src/server/utils/sanitize.py`,
`src/server/services/agent_scanner.py`, and `src/conductor_cli/fleet.py`, with
three focused test modules. No frontend or migration changed. Remote publication
was not attempted; work stayed in this worktree, with no provider, GitHub, live
backend, or database connection.
