# 0005 — Memory evaluation design

Purpose: make memory quality measurable so the refactor can prove it improved
retrieval/injection instead of merely changing it. This document defines the
evaluation set, metrics, harness and thresholds. The harness itself is built in
Phase 2; Phase 1 adds the injection audit rows the harness reads.

## What is evaluated

Given a user + thread + workspace + a task prompt, the runtime must select and
inject memory entries from the five tiers (user, project, workspace, session,
task) under a token budget. The eval checks **selection** (what was injected),
**grounding** (whether required facts are available to the answer) and
**safety** (no cross-user/tier leakage).

## Case format

Cases live in `tests/eval/memory/cases/*.yaml` (Phase 2) and are versioned with
the schema. One case:

```yaml
id: user-preference-language
tiers:
  user:
    - key: prefs/language
      content: "The user prefers Simplified Chinese answers."
  workspace:
    - key: fact/milestones
      content: "The workspace tracks the Q3 milestones."
goal: "Ask for a quick summary of the tracked milestones."
query: "用一句话总结我关注的项目"
required_entries: [prefs/language, fact/milestones]
budget_tokens: 800
must_not_include: []
```

Required fields: `id`, `tiers`, `query`, `required_entries`, `budget_tokens`.
Optional: `goal`, `must_not_include`, `stale_entries`, `expected_tier`.

## Seed cases (Phase 0 draft, 10)

| id | Tier(s) | Checks |
| --- | --- | --- |
| `user-preference-language` | user | preference recalled; answer language follows |
| `user-feedback-correction` | user | corrected instruction supersedes the older one |
| `project-constraint` | project/workspace | a "never do X" rule is retrieved |
| `workspace-symbols` | workspace | tracked items appear without being restated |
| `session-handoff` | session | a decision made earlier in the session is recalled |
| `task-progress` | task | remaining work is recalled after a checkpoint |
| `stale-entry` | user | superseded entry not injected |
| `cross-user-isolation` | user | another user's entry must never appear (`must_not_include`) |
| `budget-pressure` | mixed | selection stays within `budget_tokens` and keeps required entries |
| `chinese-tokenization` | workspace | Chinese query matches Chinese memory (Postgres FTS config) |
| `temporal-expired-fact` | workspace | an expired fact is never injected; the current view is |

The corpus is small on purpose: it is hand-labelled, versioned, and extended only
from real failures (a failed acceptance or a user-reported memory bug adds a
case). Target ≥ 30 cases before Phase 2 sign-off.

## Metrics

| Metric | Definition | Threshold (Phase 2) |
| --- | --- | --- |
| required recall | required entries present in injection audit / total required | ≥ 0.90 |
| precision@k | injected active entries judged relevant / injected | ≥ 0.70 |
| budget compliance | cases staying within `budget_tokens` | 100% |
| leakage | cross-user/tier forbidden entries found | 0 (hard fail) |
| staleness | superseded/rejected entries injected | 0 (hard fail) |
| injection tokens | mean/median/p95 injected tokens per case | reported, no regression > 10% |
| retrieval latency | p95 selection+index time | ≤ 150 ms |
| grounding (optional) | scripted presence of required facts in the answer | report only in Phase 2 |

## Harness

- Runner: `scripts/eval/memory_eval.py` (Phase 2) seeds a scratch user/workspace,
  writes memory through the normal store path, runs retrieval/injection, and
  compares against `required_entries` + `must_not_include`.
- Data source: the injection audit from `agent_run_contracts.injected_memory`
  (ids/keys only) and `memory_entries`; the runner never reads raw content from
  logs.
- Output: `docs/refactor/eval/memory-<commit>.json` + a Markdown summary table
  (committed for the record) with per-case pass/fail and aggregate metrics.
- CI: PRs touching `src/ptc_agent/agent/backends`, memory middleware, prompts or
  `src/server/app/memory.py` run the suite; a regression beyond thresholds fails.
- Manual run: `uv run --frozen python scripts/eval/memory_eval.py --cases tests/eval/memory/cases`.

## Phase 2 harness (implemented)

`scripts/memory_eval/runner.py` evaluates the **selection policy** against a
deterministic in-memory store, so a policy regression cannot hide behind an FTS
ranking change. Retrieval quality against the real projection is covered by
`tests/integration/test_refactor_memory_context.py` and is measured separately.

- Policy under test: `src/server/services/refactor/memory_context.py`
  (`select_memory_context`: per-tier quota → second pass over leftovers; dedupe
  by content hash; status filter; CJK/latin token estimate; index-block render).
- Baseline (committed corpus, flag-free): **recall 1.00 · precision 0.95 ·
  leakage 0 · stale 0 · budget 0/10** (`budget-pressure` is the intentional
  precision dip: a tiny budget admits background entries).
- Run: `uv run python -m scripts.memory_eval.runner [--json out] [--min-recall]`;
  exit 1 below thresholds; `make memory-eval` is the shorthand.
- Case corpus: `scripts/memory_eval/cases.json` (10 hand-labelled cases from the
  table above). Add cases from real failures, not hypotheticals.
- The render switch is wired (Phase 2): with `REFACTOR_MEMORY_INDEX_ENABLED` on,
  turn start selects the context, audits it on the run contract
  (`agent_run_contracts.injected_memory` + `context_budget`, plus a
  `memory.injected` event with ids/tokens only), and passes the rendered
  `<memory-context>` block into the build, where the legacy per-tier store
  indices are suppressed. Flag off — or a selection failure — keeps the legacy
  baseline exactly as it was.
- Next: read the audit from real turns (needs a valid model credential on M2),
  report recall/precision against the live selection, then flip rendering and
  keep the fake-store run as the pre-merge policy gate.

## Live Postgres retrieval probe (2026-10-25)

The fake-store eval isolates the selection policy; this probe measures the
retrieval the projection will actually use. `scripts/memory_eval/live_pg.py`
emits a transaction-scoped SQL script (temp copy of `memory_entries` with the
migration-089 generated `tsvector` + GIN index, one corpus per case) and the
exact production predicate (`websearch_to_tsquery('simple', …)` + `ts_rank`),
runs it on the M2 Postgres, then scores recall/precision/MRR by language. The
script wraps everything in `BEGIN`/`ROLLBACK`, so the probe leaves no rows.

Result on the current DDL and corpus (`retrieval_cases.json`, 10 cases):

| language | cases fully retrieved | mean recall | mean precision | misses |
| --- | --- | --- | --- | --- |
| en | 5/6 | 0.83 | 0.83 | `en-no-stemming` |
| zh | 0/4 | 0.00 | 0.00 | all CJK cases |

Findings, in order of impact:

1. **CJK retrieval does not work under `simple`.** Even a spaced query
   (the spaced fixture in `scripts/memory_eval/retrieval_cases.json`) misses: the
   lexer keeps a contiguous CJK run as one token, and `websearch_to_tsquery` ANDs
   the query terms, so a natural query can never match a longer run. Unspaced
   queries are a single token and cannot match either.
2. **No stemming.** `tracking symbols` does not retrieve `Tracked symbols` —
   acceptable for an index of short lines, but it bounds recall.
3. Mixed scripts (an ASCII token followed by a CJK run) fail for the same
   CJK reason even though the ASCII token matches.

Decision: remediation was a **CJK retrieval path**, not a tokenizer dependency.
Implemented in migration 092 (`pg_trgm` + a GIN trigram index over
`entry_key || ' ' || index_text`) and `database/refactor/memory.search_entries`,
which now branches by script: latin keeps `websearch_to_tsquery('simple', …)` +
`ts_rank`; CJK uses contains predicates per whitespace term (AND) or the
word-similarity operator (`<%`) for a single unspaced run, ranked by
`word_similarity`. The unspaced branch sets a per-query
`SET LOCAL pg_trgm.word_similarity_threshold = 0.45` (a plain `SET` would leak
through the pool), a floor measured on the corpus: the unspaced case scores ~0.57
and an unrelated line stays below 0.3.

### After remediation (same probe, 2026-10-25)

Run against a scratch database on the same M2 Postgres (the app database has not
had migration 092 applied yet; the probe creates the extension only in the
scratch database and drops it, so the app database is untouched):

| language | cases fully retrieved | mean recall | mean precision | change |
| --- | --- | --- | --- | --- |
| en | 5/6 | 0.83 | 0.83 | unchanged (the one miss is still `simple` having no stemming) |
| zh | **4/4** | **1.00** | **1.00** | was 0/4; spaced, unspaced, preference and mixed-script queries all retrieve |

The app database stayed clean (`pg_trgm` not installed there, no scratch
database left). Acceptance for deployment: apply 092, then re-run the same probe
against the migrated database and confirm the trigram index is used
(`EXPLAIN` on the CJK predicate); the English non-stemming miss stays documented
rather than being papered over with a similarity fallback that would trade
precision.

### Deployment acceptance (migrated database, 2026-10-25)

Acceptance for migration 092 was run on a **clone of the app schema**, not a
scratch temp table: `pg_dump -s` of the app database (revision 085) into a new
`refactor_verify` database, seeded to 085, then the same `alembic upgrade head`
a deployment runs (086–092). It reached 092 and created both `pg_trgm` and
`ix_memory_entries_trgm` in the clone.

- Probe against the **migrated** clone: en 5/6 fully retrieved, zh 4/4 — same as
  the temp-table run.
- `EXPLAIN` on the populated clone (20k rows, 2 CJK targets): the unspaced
  `word_similarity` predicate planned as `Bitmap Index Scan on
  ix_memory_entries_trgm` (index cond `%>` = the operator's flippable form), and
  the `ILIKE` on the longer pattern did too. A two-character term cannot form
  trigrams, so it falls back to a filter scan — correctness unaffected, only the
  short-term case is not index-accelerated.
- Cleanup: `refactor_verify` dropped, copied migration files removed from the
  container, app database verified back at 085 with `pg_trgm` not installed and
  no trigram index. The app deployment was **not** migrated or restarted.

Deployment note from the run: copying files from macOS to the Linux container
must avoid AppleDouble sidecars (`._*.py`); alembic globs `*.py` and one of these
binary files aborted the upgrade with a NUL-byte SyntaxError. Use
`COPYFILE_DISABLE=1` (or `rsync --exclude '._*'`) for the copy.

## Baseline and reporting

Phase 0/1 has **no measurable baseline** because injection is not audited today.
Phase 1 records the audit; Phase 2 measures before/after the retrieval change and
reports:

```text
cases: 30  required_recall: 0.93  precision@k: 0.78  budget_ok: 30/30
leakage: 0  stale: 0  tokens mean/p95: 412 / 690  latency p95: 61ms
delta vs baseline: +0.18 recall, -0.05 precision, +3% tokens
```

## Guardrails

- Eval fixtures contain synthetic data only; never real user memory.
- Case YAML is reviewed like code; changing an expectation requires a rationale.
- A metric threshold change is an explicit commit in this document.
- Memory content is never logged by the harness; only keys/hashes/counts.
