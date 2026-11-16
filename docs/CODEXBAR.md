# CodexBar quota rules

Conductor reads provider quota from [CodexBar](https://codexbar.app/) to decide who may work and who may review. The rules live in two places:

- `src/conductor_cli/codexbar_snapshot.py` runs `codexbar usage` for each provider and writes a small, non-identifying snapshot to `~/.conductor/usage/codexbar-snapshot.json`.
- `src/server/services/native_runtime/usage_headroom.py` turns that snapshot into `available`, `low` or `exhausted` for each provider.

## How a provider's state is decided

- Every window carries a `role`: `primary` and `secondary` are plan windows; `tertiary` and `extra` meter one bucket of the plan (for example Cursor's "Third Party" models, or a bot's own window).
- A full plan window means `exhausted`, unless the provider still has spendable credits (`credits_remaining > 0`, read from CodexBar's `credits` block, today only Codex "Extra usage"). Then it is `low`: work keeps running and is billed to the credits.
- A full bucket window means `low`, never `exhausted`.
- A snapshot written by an older writer (windows without `role`) keeps the old rule: any full window means `exhausted`.

## Keeping the rules in step with CodexBar

CodexBar changes its output upstream. Three things catch that:

1. **Version stamp.** Every snapshot records `codexbar_version` (from `codexbar --version`) next to `validated_codexbar_version`, the release these rules were last checked against (`VALIDATED_CODEXBAR_VERSION`).
2. **Drift flag.** Any output key outside the known key sets (`KNOWN_RESULT_KEYS`, `KNOWN_USAGE_KEYS`, `KNOWN_WINDOW_KEYS`, `KNOWN_CREDITS_KEYS`) is listed by name in that provider's `schema_drift`. Values are never recorded.
3. **`cond doctor`.** The `codexbar_rules` check fails when the running CodexBar differs from the validated release, or when any provider shows drift.

When `cond doctor` reports `codexbar_rules`, or after upgrading CodexBar:

```bash
.venv/bin/python scripts/refresh_codexbar_fixtures.py
```

This writes sanitized output (identity fields dropped, balances replaced with a fixed number) to `tests/fixtures/codexbar/<version>/` for codex, cursor and stepfun. Then:

1. Run `tests/unit/scripts/test_codexbar_rules.py` against the new folder. To do that, set `VALIDATED_CODEXBAR_VERSION` to the new release. `test_fixtures_only_use_known_keys` names every new key.
2. For each new key, decide whether it changes the meaning of quota (a new window, a new credits field, a renamed reset time). Update the parser and the state rules if it does, and add it to the known key set either way.
3. Keep the old fixture folder, so older behaviour stays covered. Run the related tests and open a PR.

## The snapshot job

The launchd job `com.conductor.codexbar-snapshot` runs every 300 s. It must run from the deployment checkout (`cond doctor` check `codexbar_snapshot`). Render it from that checkout with `src/conductor_cli/deploy_launchd.py`; never point it at a retired clone.
