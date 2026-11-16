# What open-source memory systems teach Conductor

This is a design reference, not a vendored framework. Conductor keeps the
LangGraph Store as the source of truth and takes the patterns that fit a
single-user, research-agent product.

## Systems reviewed

| System | Core idea | What we take | What we do not take |
|---|---|---|---|
| [mem0](https://raw.githubusercontent.com/lhl/agentic-memory/a26d9df2e1f93cfc0a80900ccd98d25b681bef27/references/chhikara-mem0.md) | Extract candidate facts, then ADD / UPDATE / DELETE / NOOP against existing memories; vector + optional graph | Extract-then-reconcile write path, explicit supersession instead of append-only | Automatic LLM write on every turn without review |
| [Graphiti](https://mintlify.wiki/getzep/graphiti/concepts/temporal-model) | Bi-temporal knowledge graph: facts carry event time and ingestion time; edges invalidated, not deleted | `valid_from` / `valid_until` and “expired facts never injected”; future graph edges | Full graph database and community summaries now |
| [Letta](https://github.com/letta-ai/letta) | Small always-in-context core blocks + searchable archival memory; memory is edited through tools | Index/detail split: `memory.md` is core, detail files are archival; no bodies in context | Self-editing all blocks every turn |
| [LangMem](https://github.com/langchain-ai/langmem) | Memory manager with hot-path tools and background extractors over namespaces | Namespaces per scope; proposals as a reviewable write path | Background LLM extraction in this iteration |
| [A-MEM](https://dl.acm.org/doi/10.52202/085713-0593) | Zettelkasten-style notes with links; new notes can trigger memory evolution | `supersedes` / `entities` / `tags` as a seed for links; future link expansion | Full note graph and re-linking pipeline |
| [Hindsight](https://hindsight.vectorize.io/blog/2026/07/24/recall-vs-reflect) | Separate retain (write), recall (retrieve), reflect (synthesize) | Clear lifecycle verbs: write / retrieve / forget; future reflection report | Separate service/runtime |
| [MemOS](https://raw.githubusercontent.com/lhl/agentic-memory/refs/heads/main/references/li-memos.md) | Memory cubes and scheduling policies | Layer/scope as first-class metadata; future consolidation scheduler | New operating-system abstraction |

## Design conclusions

1. **Keep canonical Markdown, add a typed envelope.** mem0 and Graphiti both
   separate “the fact” from “its metadata”. Conductor now does the same: the body
   stays the memory, frontmatter carries `kind`, `importance`, `confidence`,
   `valid_from`, `valid_until`, `tags`, `entities`, `supersedes`, `source`.
2. **Temporal truth is a first-class filter.** Graphiti's strongest lesson is
   that old facts should remain for audit but never enter a prompt. The
   projection filters expired/not-yet-valid rows at context assembly.
3. **Reconciliation before insertion.** mem0's ADD/UPDATE/DELETE/NOOP is the
   real quality gain. Conductor currently writes through the agent/proposal path;
   the next iteration should make “supersede, don't append” the default and add
   a consolidation proposal type.
4. **Core vs archival is already right.** Letta validates the index/detail
   split; do not inject full memory bodies. The session layer is a third block,
   not a fourth retrieval store.
5. **Links are a later multiplier.** A-MEM/Graphiti show entity links improve
   multi-hop recall, but they require extraction and maintenance. `entities` and
   `supersedes` are recorded now so a graph can be added without rewriting
   memories.
6. **Evaluation must remain deterministic.** Keep the hand-labelled corpus and
   add cases for temporal expiry, supersession, session isolation and
   contradictory facts before adding LLM extraction.

## Iteration 1 (implemented)

- Optional structured frontmatter parsed by `parse_memory_metadata`.
- `metadata JSONB` on `memory_entries` (migration 098), carried through the
  dual write and backfill.
- Context selection drops `valid_until <= now` and `valid_from > now` rows.
- Read/index APIs expose metadata; the memory inspector renders kind,
  importance, confidence, valid-until and tags.
- Prompt teaches agents when to write each field, without forcing them.

## Next iterations

### Iteration 2 — reconciliation and ranking (in progress)

Implemented:

- `GET /api/v1/memory/lineage` returns the read-only `supersedes` /
  superseded-by chain, owner-scoped and projection-backed;
- the inspector renders the chain (`old → current → newer`) under the detail;
- a committed eval case covers temporal expiry (`temporal-expired-fact`), not
  only the existing ten cases.

Remaining:

- Add `MemoryConsolidation` proposals: duplicate/contradiction candidate with
  ADD / UPDATE / DELETE / NOOP decision; approval applies supersession.
- Rank candidates by `retrieval_rank + importance + confidence + recency`, with
  deterministic tie-breaking and eval coverage.
- Validate that a new active memory's `supersedes` target exists and show a
  broken-link warning instead of silently accepting it.

### Iteration 3 — extraction and reflection
- Post-turn extractor (opt-in) creates proposals from durable user statements,
  never direct writes; redact before the LLM call.
- Reflection report: “what changed in this project this week”, built from
  retrieval + validity changes.
- Graph expansion over `entities` / `supersedes` once extraction quality is
  measured; do not add a graph store before that.

## Non-goals

- No mem0/Graphiti service dependency; no graph DB until eval shows multi-hop
  recall is the bottleneck.
- No automatic silent writes from background extraction; the review queue stays
  the safety boundary.
- No task memory inside the bank; task progress stays in `agent.md` / notes.
