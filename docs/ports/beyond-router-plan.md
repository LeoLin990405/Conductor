# Router startup noise and prompt quality (`feat/beyond-router-plan`; draft name: `feat/startup-noise`)

This port plan at `docs/ports/beyond-router-plan.md` is the source for this implementation. The legacy survey at an internal survey of the legacy projects (not published) provides migration context; no legacy implementation was copied.

**Problem:** The Router can spend context on detailed channel norms and old thread history before reaching the current Eval batch. Decision-quality evidence is also needed before enabling a persistent snapshot broadly.

**Trade-off:** A compact baseline can omit useful history, so history remains available through `autonomy_read`. Persistent snapshots require invalidation on Source and policy changes. Metrics add database writes and do not establish model quality by themselves.

## Design

### 1. **Lazy initialization and state persistence**
- Keep the static channel reference in a versioned, timestamped per-user snapshot with a SHA-256 integrity checksum. The checksum detects accidental changes; it is not a signature and does not establish authenticity.
- Invalidate the snapshot in PostgreSQL when Source or policy fields that affect routing change. Source state-only polling updates do not invalidate it.
- With the feature enabled, inline the active Eval batch and current-batch context. Read older trigger or Ticket history with `autonomy_read` only when it affects the decision.

**Implemented files:**
- `src/server/services/autonomy/router.py`: Load and verify the snapshot, render compact instructions, and fall back to the legacy prompt if unavailable.
- `src/server/services/autonomy/evals.py`: Scope enabled-path history to the active batch.
- `src/server/database/autonomy/router.py` and migration `120`: Store per-user snapshots and metrics; PostgreSQL triggers invalidate on semantic Source/policy changes.

### 2. **Bounded decision-quality instrumentation**
- Record per-Eval dispatch status and dispatch latency, canonical route outcome, and reservation-to-decision latency. Token counts remain nullable when the dispatcher does not report them.
- Store bounded observations in a user-scoped metrics table; expose them through authenticated `GET /api/v1/metrics/router`.
- Metric writes use bounded best-effort timeouts. Failed or timed-out writes can leave rows or fields absent, so these observations do not guarantee complete coverage.

**Implemented files:**
- `src/server/services/autonomy/router.py` and `host.py`: Reserve per-Eval metrics before dispatch and write the canonical outcome only after the route transaction succeeds.
- `src/server/database/autonomy/router.py`: Persist bounded measurements and route outcomes.
- Tests use a deterministic fake and fixture batches. They do not establish live model accuracy, latency, or decision stability rates.

### 3. **Reduce baseline prompt context**
- Replace the broad channel catalog with the norms needed by the current candidate batch after a verified snapshot hit. The enabled path also avoids loading full old thread history before routing.

**Implemented files:**
- `src/server/services/autonomy/router.py`: Log same-payload legacy and snapshot prompt byte counts.

## Validation coverage

1. **Deterministic Router fake** (`tests/unit/server/services/autonomy/test_router_quality.py`):
   - Routes 10 fixture batches through the real host boundary, checks selected candidates and stable candidate/batch keys under reversed input order, and verifies that only committed host decisions are recorded. Decision IDs identify one Eval observation and are reused by dispatch retries. The test does not establish live model accuracy or prove that a real model chooses equivalently.

2. **Snapshot invalidation regression** (`tests/unit/server/services/autonomy/test_router_snapshot.py` and `tests/unit/server/test_router_snapshot_migration.py`):
   - Verify snapshot freshness/CAS behavior and inspect the migration's Source/policy invalidation triggers. A real PostgreSQL race test remains outstanding.

3. **Prompt byte checks** (`tests/unit/server/services/autonomy/test_router_baseline_size.py`):
   - Compare full bounded history with current-batch context and compare legacy versus snapshot rendering for the same active payload.

4. **Persistence and API checks:** `tests/unit/server/database/test_autonomy_router_persistence.py`, `tests/unit/server/app/test_router_metrics_api.py`, and `tests/unit/server/services/autonomy/test_router_metrics_hook.py`.

5. **Scoped offline suite:** 689 tests passed across autonomy services, autonomy API and database, Router metrics and migration, config, import layering, and migration single-head. This does not include a live startup-latency or PostgreSQL race run.

The planned live startup-latency check is deferred.

## Migration and feature flag

- Add `AUTONOMY_ROUTER_SNAPSHOT_ENABLED` (default False initially, rolled to True after baseline acceptance).
- Snapshot and metrics migration: `120_router_snapshot_metrics.py`, after migration `119`.
- No existing API shape changes. The flag-off path and host authority checks remain in place; equivalence of model choices after enabling the compact prompt has only deterministic fake evidence.

## Original acceptance targets and current evidence

- **Decision quality:** Not established against a live model. The deterministic fake tests stable candidate selection under input reordering; this is not a production acceptance rate or a decision-stability statistic.
- **Prompt bytes:** The deterministic fixture measured 10,633 bytes for bounded full history and 1,485 bytes for the snapshot/current-batch path (86.034% lower). With the same active payload, legacy rendering measured 1,602 bytes and snapshot rendering 1,485 bytes (7.303% lower), isolating instruction and rendering changes. These fixture measurements do not establish a production-wide reduction.
- **Latency and tokens:** Dispatch latency (`latency_ms`, exposed as `dispatch_latency_ms`) and reservation-to-route-decision latency (`decision_latency_ms`) are recorded where available. Metric writes are best-effort and may be absent or null after a timeout. No live Router P99 or model-token acceptance result is established; token count can remain unknown.
- **Invalidation:** Migration `120` defines Source and policy triggers. Tests inspect the migration and fake-backed behavior; a real PostgreSQL race-condition check remains outstanding.
- **Rollout:** `AUTONOMY_ROUTER_SNAPSHOT_ENABLED` defaults to `false`. It has not been enabled for a 48-hour production observation.

The original targets of >95% decision stability, P99 below 1 second, a PostgreSQL race check, and 48 hours of enabled production observation remain targets, not completed acceptance evidence.

## Implemented port summary

Migration `120` adds a per-user snapshot with generation-based compare-and-set refresh and a SHA-256 integrity checksum, plus one bounded metrics row per Eval. PostgreSQL triggers invalidate snapshots after routing-relevant Source or policy changes. The authenticated metrics endpoint returns only the requesting user's rows. The opt-in config flag remains off by default.

The local deterministic prompt fixture measured an 86.034% reduction when replacing its bounded old history with active-batch context. The same-payload renderer comparison measured 7.303%. Offline validation passed 689 tests in the scoped autonomy, API, database, config, import-layering, and migration set. No live database, backend, provider, model, or production rollout validation was performed.

The persistent local change record is an internal change record (not published).

## Final review fix

The final review found one P2: `GET /api/v1/metrics/router` did not apply the `super_agent` feature gate used by the autonomy routes. The gate from `src/server/app/autonomy.py:_require_feature` is now applied before reading metrics, returning the same 404 when disabled. No legacy implementation was copied. Regression coverage checks both the disabled path (no metrics service call) and enabled authenticated behavior. The focused API test passed 2 tests; the broader offline review suite passed 701 tests, including the focused test (counts overlap). No live validation, web change, or migration change was made. See an internal change record (not published) for hashes and commands.
