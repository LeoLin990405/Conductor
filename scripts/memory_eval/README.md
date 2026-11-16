# Memory eval harness (Phase 2)

Evaluates the memory-context **selection policy** — tier quotas, dedupe, token
budget, staleness, leakage and selected-entry metadata — against a deterministic
synthetic in-memory store. Retrieval quality (Postgres FTS) is measured
separately. This runner does not generate answers or establish answer fidelity.

Design and thresholds: [`docs/refactor/0005-memory-eval.md`](../../docs/refactor/0005-memory-eval.md).

```bash
uv run python -m scripts.memory_eval.runner --json /tmp/memory-report.json
uv run python -m scripts.memory_eval.runner --min-recall 0.95
```

Exit code 1 when the corpus misses the thresholds; `--json` writes the evidence
used in a PR.

## Gates

| metric | threshold |
| --- | --- |
| mean required recall | ≥ 0.90 |
| mean precision | ≥ 0.70 |
| leakage (must-not-include / other user) | 0 |
| stale (superseded/rejected) selected | 0 |
| cases over budget | 0 |
| missing or unsupported selected-entry metadata checks | 0 |

The selection outcomes are deterministic, so the same corpus yields the same
selected ids and policy metrics. Per-case `latency_ms` uses `perf_counter` around
selection only; it is an observational timing for this local synthetic runner,
not production retrieval latency. Each case lists the ordered rows retrieval
would return.

## Live Postgres retrieval probe

`live_pg.py` measures the real FTS path (not the policy) against a real
Postgres, with a transaction-scoped temp copy of `memory_entries` so nothing is
written:

```bash
# 1. emit SQL (one corpus per case, production predicate, BEGIN/ROLLBACK)
uv run python -m scripts.memory_eval.live_pg --sql > /tmp/retrieval.sql
# 2. run it on the host that owns the database
ssh <host> 'cd <stack> && docker compose exec -T postgres \
  psql -U postgres -d postgres -A -t -F "|" -v ON_ERROR_STOP=1' \
  < /tmp/retrieval.sql > /tmp/retrieval.out
# 3. score by language, optionally write JSON
uv run python -m scripts.memory_eval.live_pg --results /tmp/retrieval.out --json report.json
```

Current corpus result (2026-10-25, M2):

| run | en fully retrieved | zh fully retrieved |
| --- | --- | --- |
| before remediation (FTS only) | 5/6 | 0/4 |
| after remediation (`--cjk-path`, 092 applied in a scratch DB) | 5/6 | **4/4** |

The before run is the finding (CJK cannot be retrieved under `simple`
tokenization); the after run is the `pg_trgm` word-similarity path from
migration 092. A migrated-database acceptance run (cloned app schema at 085 →
`alembic upgrade head` → 092) reproduced en 5/6 · zh 4/4 and `EXPLAIN` showed the
trigram index in the plan; see
[`docs/refactor/0005`](../../docs/refactor/0005-memory-eval.md) for both tables
and the deployment note. Drop `--scratch-db` to target a database that already
has 092. This probe is measurement,
not a gate; the gate on the committed corpus stays `runner.py`.

## Cases

`cases.json` holds 16 synthetic cases, including four P1 cases for a conditional
rule, a user preference across sessions, an exact-wording source reference, and
assistant inference that must remain non-evidence. The condition and source
pointer checks inspect each selected `ContextEntry.to_dict()` result. Missing
or unsupported output fails the gate; the committed corpus requires zero
unsupported checks. The source-pointer check validates only message-id and
offset shape. It does not resolve a source message, confirm ownership, compare
the quote with source text, or measure generated-answer fidelity. This remains
a selection-policy test over synthetic rows, not a live retrieval or response
quality test. Edit an expectation only with a rationale.
