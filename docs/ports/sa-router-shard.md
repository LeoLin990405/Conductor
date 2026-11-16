# §8.1 Router shard threshold + bounded peer Worker channel

Closes the `SUPER-AGENT-ALIGNMENT.md` §8.1 gap ("know when to shard Router or add peer Worker communication"): the Router shards candidate dispatch above a configurable threshold, and Workers message peer Tickets through a bounded channel. This is a gap closure against the article's §8.1 design direction in `SUPER-AGENT-BEYOND.md`, not a legacy port — `docs/MIGRATION-FROM-LEGACY.md` has no router-shard or peer-channel source.

## What was added

- **Router sharding.** `src/server/services/autonomy/shard.py` (new): `AUTONOMY_ROUTER_SHARD_THRESHOLD` (default 32) and `AUTONOMY_ROUTER_MAX_SHARDS` (default 4), read as positive ints; `router_shard_count` returns 1 at or below the threshold and `ceil(load / threshold)` capped above it; `router_shard_for` assigns each batch a stable shard by SHA-256 of its batch key. `evals.start_due_evals` stamps `shard` on every router-batch effect payload, and `evals.route_eval_effect` passes it through `router.dispatch_candidate_batch` to `ensure_router_session`, which keeps one Router session per `(user, shard)` (`src/server/database/autonomy/router.py`). Deadline and timeout requeries preserve the original shard. Worker reports and watchdog reviews stay on shard 0.
- **Peer Worker channel.** `src/server/services/autonomy/peer_channel.py` (new): same-user, non-ended Tickets may send messages of at most 2000 chars while the receiver's pending (unconsumed) count is under `AUTONOMY_PEER_CHANNEL_CAPACITY` (default 32); a full channel returns `channel_full` instead of queueing. Reads consume pending rows and tag each as untrusted peer data that grants no authority. Backed by `src/server/database/autonomy/peer_channel.py` (new) on the `autonomy_peer_messages` table: count-and-insert runs under a transaction-scoped advisory lock so concurrent backend workers cannot overshoot capacity. Exposed as `autonomy_peer_send` / `autonomy_peer_inbox` on `AutonomyWorkerHost` and in `AUTONOMY_WORKER_TOOLS` (`src/tools/autonomy/tools.py`), with one discoverability line in the PTC Worker prompt (`worker.py`). Native CLI Workers have no tools and are unchanged.
- **Migration.** `migrations/versions/122_router_shard_peer_channel.py`, chained after `121_dispatch` (renumbered here from the duplicate 119 this branch originally claimed, then from 120 when the auto-dispatch line took 121): adds `shard INTEGER NOT NULL DEFAULT 0` to `autonomy_router_sessions` with primary key `(user_id, shard)`, and creates `autonomy_peer_messages` plus its receiver index. Downgrade collapses sessions to the lowest shard per user, drops `shard`, restores the old `user_id` key, and drops the message table.
- **Docs.** `SUPER-AGENT-ALIGNMENT.md` §8.1 row Partial → **Aligned**, combined with integrate/r2's §4.2 channel model and §7 experiment rows (counts now 28 Aligned / 7 Partial / 0 Missing, verification-boundary paragraph updated). `SUPER-AGENT-BEYOND.md` §8.1 "Decide when and how to shard the Router" and "Define when peer Workers should communicate" rows now **Yes**, with the configuration-derived-threshold and same-user-inbox limits stated.

## Tests

New: `tests/unit/server/services/autonomy/test_router_shard.py` (9), `tests/unit/server/services/autonomy/test_peer_channel.py` (11), `tests/unit/migrations/test_120_router_shard_peer_channel.py` (3) — 23 new tests. Head pins updated in `tests/unit/migrations/test_refactor_migrations.py`, `test_118_deliberation_groups_continue.py` (chain continues past 118; sole head is 120), `test_119_channel_reply_approval.py`, and `tests/unit/scripts/test_refactor_status.py`.

## integrate/r2 merge (this branch)

Merged `integrate/r2` @ `7017473c0` into `feat/sa-router-shard`. Conflicts resolved:

- **`docs/SUPER-AGENT-ALIGNMENT.md`** — kept both sides: §4.2 channel model + §7.1/§7.2 experiment rows from r2, §8.1 router-shard/peer-channel Aligned from this branch. Combined counts **28 Aligned / 7 Partial / 0 Missing**.
- **`src/server/services/autonomy/host.py`** — kept both `peer_channel` (this branch) and `channels` imports (r2); `AutonomyWorkerHost.peer_send`/`peer_inbox` and `AutonomyToolHost.reply` coexist.
- **Duplicate alembic revision 119** — r2 brought `119_channel_reply_approval`; this branch's router-shard migration renumbered **119 → 120**, `down_revision = "119"`, file `migrations/versions/120_router_shard_peer_channel.py`. Single head **120**.
- **Tests** — `test_118_…` asserts the chain continues past 118 and sole head is 120; `test_refactor_status` and `test_refactor_migrations` pin head 120; `test_119_channel_reply_approval` and `test_120_router_shard_peer_channel` both assert `heads == {"120"}`.
- **Stress harness fake** — `scripts/superagent_router_stress.py`'s `ensure_session` fake now accepts `shard=` (the production `dispatch_candidate_batch` passes it).

Post-merge tests (repo `.venv`, Python 3.14.8):

- `pytest tests/unit/server/services/autonomy/ tests/unit/migrations/ tests/unit/scripts/test_refactor_status.py tests/unit/scripts/test_superagent_ablation.py tests/unit/scripts/test_superagent_router_stress.py tests/unit/server/services/refactor/memory_federation/ tests/unit/server/services/refactor/test_refactor_dual_write.py tests/unit/conductor_cli/test_worktree.py tests/unit/server/test_import_layering.py -q` → **704 passed**
- `pytest tests/unit/server/services/autonomy/test_worker_host_read.py tests/unit/server/services/autonomy/test_channel_model.py tests/unit/server/services/autonomy/test_channel_reply_delivery.py tests/unit/server/services/autonomy/test_channel_reply_approval_api.py -q` → **58 passed**
- Alembic script directory: single head `120` → `119` → `118`.
- Ruff not installed in the repo venv; lint not run.

No live backend, database, frontend, or provider was touched; migration tests assert SQL through a mocked `op`, and channel/shard behavior is unit-tested with fakes.

## Review fixes (P1/P2)

- **P1** `database/autonomy/peer_channel.py`: the capacity count was read as `row[0]` from a `dict_row` cursor (`KeyError: 0` on every send); it is now `COUNT(*) AS n` read by name (also in `pending_count`).
- **P2** count-and-insert now runs inside `conn.transaction()` so the advisory xact lock spans both statements on the autocommit connection.
- **P2** inbox reads use one atomic `claim_pending` (`UPDATE ... WHERE message_id IN (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING`), replacing the separate `list_pending` + `mark_read`; concurrent readers never get the same message, and `consumed` equals the messages returned.
- Regression tests (fake connection) added in `test_peer_channel.py`; autonomy services + `test_autonomy_api.py` + migrations: 568 passed. Ruff is not installed in the repo venv, so lint was not run.

## integrate/r2 merge #2 — auto-dispatch line (121), migration renumbered to 122

Merged `integrate/r2` @ `72b1282b7` (the auto-dispatch port; its schema head is
`121_dispatch`, `down_revision = "119"`) into `feat/sa-router-shard`. Both
migrations chained after 119, so the merge would have forked the head; this
line's migration moved on top instead.

**What was renumbered (from `migrations/versions/120_router_shard_peer_channel.py`):**

- `migrations/versions/120_router_shard_peer_channel.py` → `122_router_shard_peer_channel.py`,
  `revision = "122"`, `down_revision = "121"`. DDL unchanged; the downgrade is
  untouched and still reversible. Chain: `… 119 → 121 → 122`, single head **122**
  (`alembic heads` confirms).
- `tests/unit/migrations/test_120_router_shard_peer_channel.py` →
  `test_122_router_shard_peer_channel.py`: asserts `("122", "121")`, the
  upgrade/downgrade SQL, and `heads == {"122"}` by AST discovery.

**Conflicts resolved (both sides' intent kept)** — all three were schema-head pins:

- `tests/unit/migrations/test_refactor_migrations.py` — `get_heads() == ["122"]`
  plus this branch's `122 → 121` link and r2's `121 → 119` link.
- `tests/unit/migrations/test_119_channel_reply_approval.py` — graph test now
  named for 122; keeps r2's `revisions["121"] == "119"` assertion and pins
  `revisions["122"] == "121"` with `heads == {"122"}`.
- `tests/unit/scripts/test_refactor_status.py` — r2's generic
  `test_repo_head_is_single` name kept; `repo_schema_head()` and the `--json`
  `schema_head` now assert `"122"`.
- Auto-merge damage fixed in `tests/unit/migrations/test_118_deliberation_groups_continue.py`:
  it had picked up both `heads == {"121"}` (r2) and `heads == {"120"}` (this
  branch); now `heads == {"122"}` with `"118" not in heads` kept.

**Tests run (repo `.venv`, Python 3.14):**

- `pytest tests/unit/migrations/ tests/unit/scripts/test_refactor_status.py -q`
  → **263 passed**.
- Targeted merge set (migrations + scripts + `tests/unit/tools/auto_dispatch` +
  dispatch API/DB/service + conductor_cli): **754 passed**.
- Migration run for real against a throwaway local Postgres database (created →
  `alembic upgrade head` → `downgrade 121` → `upgrade head` → dropped): head
  `122`, `autonomy_router_sessions.shard` with PK `(user_id, shard)`,
  `autonomy_peer_messages`, and `dispatches` present; the downgrade removes the
  peer channel/shard and keeps `dispatches`; re-upgrade restores 122. No live
  DB, backend, or provider was touched.
- Ruff on every file this merge changed, under the repo config's rule set
  (`E4,E7,E9,F,TID251`, ignore `E741`): clean. (`uvx ruff` 0.17.3's wider
  default set flags pre-existing code from both parents; the repo venv has no
  ruff installed, so `make lint`'s `uv run ruff` was not runnable without a
  network sync.)
