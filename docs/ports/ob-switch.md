# ob-switch port (G7)

Clean-room: built from the stated idea only (see `docs/design/ob-switch.md`); no external or OpenBot code was read or copied.

## What was built
A stuck line (a Ticket's Worker) can be moved to another provider/model, keeping its worker thread and worktree.
- `src/server/services/autonomy/line_switch.py` — pure logic: `detect_stuck` (stall / consecutive failures / breaker open), `pick_target` (skips the current model, the reviewer, and open-breaker providers), `build_handoff` / `render_handoff` / `inject_handoff` (bounded conversation tail, last diff, open findings), `plan_switch`.
- Migration `125_ticket_switches.py` — `autonomy_tickets.switches JSONB` (chained after 124, reversible). `TicketView.switches` exposes it.
- `tickets_db.record_switch` — status- and model-fenced append of the switch record; swaps `worker_provider/model`; stages the handoff in `mandate.handoff`.
- `worker.launch_worker_effect` prepends `mandate.handoff` to the relaunched Worker prompt.
- `POST /api/v1/autonomy/tickets/{id}/switch` and `conductor tickets switch <id> [--provider --model --reason --candidate P/M]`. The reviewer is read from `mandate.reviewer`; a switch to it (or with no eligible candidate) returns 409.

## Not done
Automatic triggering from the watchdog/breaker (the `detect_stuck` hook exists but isn't called); the caller supplies messages/diff/findings. Not verified against a live backend or DB.

## Tests
New: `test_line_switch.py` (8), `test_line_switch_api.py` (4, fake DB), `test_125_ticket_switches.py` (1), CLI test (1) — 14 new, all passing. Head assertions bumped to 125 in `test_refactor_migrations.py`, `test_119_channel_reply_approval.py`, `test_refactor_status.py`.
Run: unit autonomy + migrations + refactor_status + conductor_cli suites → 689 passed. Ruff clean on touched source/new files. No network, provider, GitHub, live backend or DB used.

## Review fixes (rr3)
- Switching a running cloud line now cancels the old run, clears `worker_run_id` and enqueues `launch_worker`; the resume dispatch uses a per-switch request key.
- The handoff is also prepended on daemon claim, and cleared once delivered (no longer repeats on later launches).
- Reviewer is read from the mandate only; the request body no longer accepts one.
- `pick_target` skips candidates with a blank provider or model.
- Tests: autonomy suite, 357 passed. Known gap: a daemon already holding a lease keeps its old run until it next claims.

## Review round 5 fixes
- Switching a running Ticket now requeues it in one statement (thread, run and daemon lease dropped), so a native thread is recreated on the new provider and an old daemon lease cannot report a completion.
- The replacement launch effect is inserted in the same transaction as the switch (`record_switch(launch_effect=...)`); the old run is cancelled best-effort afterwards.
- Handoff cleanup is a compare-and-clear on the delivered text (`clear_handoff_if`), so a newer handoff survives. Initial launch also uses the per-switch request key.
- Tests: `tests/unit/server/services/autonomy`, `tests/unit/migrations`, `tests/unit/test_conductor_cli.py` — 633 passed.

## Fix 5: fence PTC writes after a line switch
- `host.py` routes `worker_progress` and `apply_worker_report` through `tickets.py`'s `record_worker_report`. The required reporting thread ID and guarded update accept writes only while the ticket is `running` and its `worker_thread_id` matches; ticket projection and report insertion share one transaction.
- If a switch or another transition out of `running` wins the row lock, late progress/final reports cannot change the report list or ticket state. Regressions cover switch-after-read for progress and final reports, current-line acceptance, and rollback when report insertion fails. No additional legacy source was ported.
- Validation: autonomy service unit suite — 370 passed; full unit run — 16,383 passed, 49 skipped, 974 deselected. The initial full run had two `rg` glob-test failures because its ignored `.pytest_cache` basetemp was hidden from `rg`; both tests passed unchanged with a non-ignored basetemp, followed by the successful full run. Compile and diff checks passed. No real provider, GitHub, live backend, live database, or external service calls.
- This is the scoped PTC write-fence fix; it does not claim that the other rr9 launch/claim findings are resolved.

## Fix 6: fence PTC no-report waiting fallback; drop double revision bump
- From: `src/server/services/autonomy/executor.py` (PTC branch of `poll_running_workers`); `src/server/database/autonomy/tickets.py` (`record_worker_report`). No legacy file ported — review follow-up on fix5.
- (1) When a PTC turn ends without the report tool, `update_ticket(..., status="waiting")` now fences with `expected_status="running"` and `expected_thread_id` for the observed thread; a lost fence is ignored like a switched native report.
- (2) `record_worker_report` no longer issues a second standalone `revision = revision + 1` after the guarded UPDATE already advanced it.
- Tests: `test_ptc_without_report_fences_waiting_on_observed_thread`, `test_ptc_without_report_ignores_switched_away_line` in `test_native_report_text.py`; revision-once assertions in `test_line_switch_api.py`.
- Validation: targeted autonomy regressions — 77 passed; autonomy suite — 372 passed; `uv run pytest tests -q -p no:cacheprovider` — 16,385 passed, 49 skipped, 974 deselected. No real provider, GitHub, live backend, or DB.
