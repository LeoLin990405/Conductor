# Port summary: sa-lifecycle — code-review fixes for the unsubscribe/reassign lifecycle

Date: 2026-11-03 · Branch: `feat/sa-lifecycle` · Worktree: `sa-lifecycle`

## What was requested

Fix all P1/P2 findings from the latest read-only code review of this branch
(log: `codexrev--sa-lifecycle--review-sa-lifecycle.log`, review range
`7ffc9b54e..8bc5334f4`, verdict BLOCK), each with a regression test.

## Findings fixed (4)

All four are in the autonomy unsubscribe/reassign lifecycle shipped in
`b9978d7f7` (port doc: the internal document `sa-unsubscribe-reassign` (not published)).

- **P1 — `src/server/database/autonomy/tickets.py` (`record_handoff`)**:
  handoff state embedded the previous owner's latest report row, whose
  `created_at` is a `datetime`; `Json(state)` refused the bind, and the raise
  landed after ownership and inputs had already moved. The state tree is now
  coerced JSON-safe (`_json_safe`: datetimes/dates → ISO, UUIDs → str,
  anything else → str) and bound via `SafeJson` (also strips NUL and non-finite
  floats). Raw report rows keep their datetime shape for the other consumers
  that compare them (`executor.py`).
- **P2 — `src/server/services/autonomy/host.py` (`reassign` delivery key)**:
  reassignment reused the original `autonomy:input:{input_id}` idempotency key,
  so `ON CONFLICT DO NOTHING` kept the previous owner's stale payload and the
  moved-input guard in `inputs.py` then skipped the delivery — no delivery
  reached the target. Deliveries now use
  `autonomy:input-reassign:{to_ticket_id}:{input_id}`.
- **P2 — `src/server/database/autonomy/tickets.py` (`move_undelivered_inputs`)**:
  a target that already held the same Trigger (a context subscriber's context
  copy) made the move skip the input, stranding pending work on the Ticket that
  just lost the Thread. Conflicting rows are now adopted: the actionable
  payload takes the target's row over (undelivered again) and the previous
  owner's duplicate is deleted.
- **P2 — `src/server/services/autonomy/host.py` (`reassign` atomicity)**:
  each step committed independently, so a mid-transition failure left
  ownership half-transferred and retry failed because the source subscription
  was already ended. The whole transition — guarded subscription end, target
  reactivation, input transfer, handoff ledger, delivery/launch effect
  enqueues — now commits in one `conn.transaction()` on one pooled connection
  (`conn=` threaded through `end_subscription`, `add_subscription`,
  `move_undelivered_inputs`, `latest_report`, `record_handoff`, and
  `enqueue_effect`, the pattern `insert_effect` was built for). A failure
  rolls back whole; a retry still sees the source subscription active.

## Tests

Regression tests added:

- `test_record_handoff_state_is_json_safe` — datetime/UUID/NUL state binds
  (P1).
- `test_move_adopts_inputs_the_target_already_holds` — conflicting context
  copy adopted, previous owner's duplicate deleted (P2b).
- `test_reassign_delivery_key_does_not_reuse_the_original_input_key` (P2a).
- `test_reassign_rolls_back_when_a_step_fails` — mid-transition error rolls
  back, no delivery effect survives (P2c); the happy-path test also pins
  every write to the same connection in exactly one transaction.

Runs (repo `.venv`, fakes/mocks only, no live services):

- `tests/unit/server/services/autonomy/` + `tests/unit/server/database/`:
  **989 passed**.
- `tests/unit/server/test_import_layering.py` + `tests/unit/tools/`:
  **416 passed**.
- Full backend unit suite (`tests/unit`): **16,265 passed, 28 skipped**,
  0 failures.

## Not verified here

Real-DB transaction behavior, live daemon unsubscribe/reassign, and
multi-worker stress: the atomicity rests on Postgres transaction semantics
plus the fake-pinned call contract. The live checkout, `:8000` backend, and
live DB were not touched. No migration needed (no schema change).
