# Close the live run and deliver instructions published with the thread

Review fixes for the task-first interaction on `feat/sa-task-first`. Nothing here is copied from a legacy repository. The behaviour is in `src/server/app/autonomy.py` and `src/server/services/autonomy/worker.py`.

Close is `POST /api/v1/autonomy/tickets/{id}/close`. It cancels the thread with no `run_id`, so the stop targets the live run. The stored `worker_run_id` is the run that first started the thread. A reply starts another, and cancelling the stored id reports `already_finished` while the new run continues. A second close of an already closed task still does not write, and it cancels the live run again. A failed stop answers 503. The first close leaves the task open. A later close of an already stamped task asks for a retry.

A reply that starts a turn stores that receipt's run id. If the task closed during the dispatch, the reply cancels that run. The status update is fenced on the status the reply read. A lost fence re-reads the ticket and returns 409 only when the task is missing or already ended.

Launch snapshots undelivered instructions before `create_thread`. Publishing the thread is fenced on `status=queued`. If close already won, launch does not set the task running and does not dispatch. `create_thread` does not start a run. Cancelling that idle thread is best-effort. If close wins after publish, or during the dispatch, launch cancels the run it started and does not mark a closed task failed. A retry of the launch effect, when `closed_at` is set and a thread id is published, cancels the live run and raises if that stop fails.

A threadless reply and the post-publish instruction snapshot take the same session advisory lock, `pg_advisory_lock(hashtextextended('autonomy-instruction:{ticket_id}', 0))`. The snapshot runs after the thread id commits, still under that lock. A reply that still sees no thread only stores the instruction (`queued_on_task`). A reply that sees the published thread queues `deliver_input` with idempotency key `autonomy:input:{input_id}`, the same key the launcher uses for an instruction the prompt missed. Instructions already in the launch prompt are not queued again. They are marked delivered only after a successful dispatch. The lock is not held across `create_thread` or `dispatch_turn`.

The close stamp is `migrations/versions/120_task_close.py`. It adds `closed_at` and `close_reason` on `autonomy_tickets` and drops both columns on the way down. Revision 119 is `migrations/versions/119_channel_reply_approval.py`, the same file as on `integrate/r2`, with `down_revision = "118"`. The close stamp follows 119. A second revision that also depends on 118 gives Alembic two heads, and `alembic upgrade head` refuses to run. Reusing the id `119` hides one of the two files, because Alembic keeps a single script per revision id.

Nothing in this fix is copied from a legacy repository. The channel-reply file is the Conductor revision that already owns 119.

The live backend on port 8000, the live database, GitHub, and provider APIs were not contacted. The UI was not opened in a browser, because that would use the live app. Web was not changed. No migration was applied to a database.

The chain fix was checked with `../../Documents/deepseek-harness/default-workspace/AlphaCN/.venv/bin/python -m pytest` on `tests/unit/migrations` and `tests/unit/scripts/test_refactor_status.py`. **255 passed.**

Earlier interaction tests on this branch used the same interpreter on `tests/unit/server/test_task_interaction_api.py`, `tests/unit/server/test_autonomy_daemon_followup.py`, `tests/unit/server/services/autonomy/test_worker_pending_instructions.py`, `tests/unit/server/services/autonomy/test_worker_launch_context.py`, `tests/unit/server/services/autonomy/test_worker_slots.py`, `tests/unit/server/services/autonomy/test_daemon_resume.py`, `tests/unit/server/services/autonomy/test_task_interaction.py`, `tests/unit/server/services/autonomy/test_interaction_contracts.py`, `tests/unit/server/services/test_dispatch_cancel.py`, and `tests/unit/server/test_autonomy_api.py`. **126 passed.**
