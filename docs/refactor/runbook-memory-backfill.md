# Runbook — memory projection backfill

The LangGraph store is the source of truth for memory; `memory_entries` is the
relational projection that Phase-2 retrieval and the shell read. The migration
(086–092) creates the table but does not populate it: this job does.

## Preconditions

- Migrations applied (`alembic upgrade head`), i.e. head **092**.
- `REFACTOR_MEMORY_INDEX_ENABLED` is not required for the backfill itself; it
  only gates retrieval/selection/injection at runtime.
- The process can reach both the LangGraph store table and the app database
  (run inside the backend container, which already has `DB_*` / `MEMORY_DB_TYPE`).

## Dry run (default, no writes)

```bash
docker compose exec -T backend uv run --frozen \
  python /app/scripts/refactor_memory_backfill.py --json /tmp/memory-backfill-dry.json
```

Read the table: `store_items` (store entries read), `rows` (projection rows the
job would write), `missing`/`extra`/`changed` (store-vs-projection parity), and
`failures`. A non-UUID user id (`local-dev`) is reported and skipped — the
projection column is UUID — so a dev user's memory is never partially projected.

Single user:

```bash
docker compose exec -T backend uv run --frozen \
  python /app/scripts/refactor_memory_backfill.py --user <user-id>
```

## Apply

```bash
docker compose exec -T backend uv run --frozen \
  python /app/scripts/refactor_memory_backfill.py --apply --json /tmp/memory-backfill.json
```

The job is idempotent (`upsert` on `(user_id, tier, entry_key, status='active')`);
re-running after a partial failure is safe and is the repair path when a dual
write is dropped. With `REFACTOR_MEMORY_INDEX_ENABLED=1` the store write path
mirrors memory writes itself, so the backfill is a one-off history import plus
the periodic/parity repair, not the only writer. `--apply` exits 1 if any entry failed, with an empty parity
report as the success signal.

## Verify

```sql
SELECT tier, count(*) FROM memory_entries WHERE status = 'active' GROUP BY tier;
SELECT user_id, tier, entry_key, left(index_text, 60)
FROM memory_entries WHERE status = 'active' ORDER BY updated_at DESC LIMIT 10;
```

Then search through the real retrieval path (CJK needs 092 applied):

```bash
# inside the backend container's Python or a scratch probe
python -m scripts.memory_eval.live_pg --sql --cjk-path | psql ...
```

## Rollback

The projection is rebuildable: `TRUNCATE memory_entries` (or delete one user's
rows) and re-run the backfill. The store is untouched by this job, and the
migration downgrade (092 → 085) drops the projection tables entirely.

## What the job writes

- one row per **detail** memory entry, never `memory.md` itself;
- `index_text` from the `memory.md` pointer line (`- [Title](file.md) — hook`)
  or, when unpointed, the detail's frontmatter `description`, else its first
  meaningful line;
- `content_hash = sha256:<hex>`, `size` in bytes, `provenance.source =
  store_backfill`, `pointed` true/false;
- tier `user` for `(<user>, "memory")`, tier `workspace` for
  `(<user>, "workspaces", <workspace>, "memory")` of owned, non-deleted
  workspaces. Project/session/task tiers have no store namespace yet and are not
  projected.
