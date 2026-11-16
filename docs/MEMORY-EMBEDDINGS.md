# Memory API embeddings

Memory retrieval works without embeddings: FTS for latin, CJK trigram for
Chinese. The optional embedding path adds semantic recall when an API provider
is configured. It is **fail-open** — a missing key, provider outage or malformed
response leaves the lexical path running.

## Configuration

```dotenv
MEMORY_EMBEDDING_ENABLED=1
MEMORY_EMBEDDING_PROVIDER=minimax
MEMORY_EMBEDDING_API_URL=https://api.minimax.cn/v1/embeddings
MEMORY_EMBEDDING_MODEL=embo-01
MEMORY_EMBEDDING_DIMENSIONS=1536
# Falls back to MINIMAX_API_KEY for provider=minimax
MEMORY_EMBEDDING_API_KEY=
```

Provider shapes:

- **MiniMax** (`provider=minimax`): `{model, texts, type: "db"|"query"}` →
  `{vectors: [[...]]}`. `embo-01` was verified against the local key.
- **OpenAI-compatible** (`provider=openai` or any other value): `{model, input}`
  → `{data: [{embedding: [...]}]}`.

The backend container needs network access to the provider. In this local stack
the `.env` proxy values (`HTTP(S)_PROXY=http://host.docker.internal:7897`) are
used by `httpx`.

## What is sent

Only the **index text** stored in `memory_entries.index_text` (one pointer line,
e.g. `Title — one-line hook`) and the search query are sent. Memory bodies are
not sent for embedding. That is still a network boundary: set the provider
deliberately, or leave `MEMORY_EMBEDDING_ENABLED` off for a fully local/offline
memory bank.

## How retrieval uses it

- Embeddings are L2-normalised and stored in `memory_entries.embedding` as JSON
  (the local Postgres image has no `pgvector` extension).
- On a search, missing/stale vectors are generated lazily in batches (max 16)
  when a provider is configured; the summary exposes `embedded`/`pending`.
- Results combine lexical rank and cosine semantic rank with reciprocal-rank
  fusion. A provider failure returns the lexical rows unchanged.
- The same path feeds the turn's memory-context selection, so injection and the
  inspector agree.

## Future pgvector migration

The storage/search contract is intentionally behind `memory_embeddings.py`.
Switching Postgres to `pgvector/pgvector:pg16` and replacing the JSONB cosine
loop with `vector` + an HNSW index is a later optimization, not a behavior
change for callers.

## Verification

```bash
curl -s "$API/api/v1/memory/summary" | jq '.embedding_enabled,.embedded_rows,.pending_embeddings'
curl -sG "$API/api/v1/memory/index" --data-urlencode 'q=seeded memory' | jq '.entries[0].snippet'
```
