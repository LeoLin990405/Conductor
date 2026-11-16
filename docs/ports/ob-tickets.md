# ob-tickets: single-owner Tickets, ownership operations, and the line dashboard

Self-iteration line G7/G10, ported clean-room into the autonomy subsystem.
Design note: [docs/design/ob-tickets.md](../design/ob-tickets.md). No OpenBot
or other external code was read, fetched, or copied — the design comes from
the feature idea alone, mapped onto Conductor's existing ticket/daemon
machinery.

What a Ticket gains:

- **Exactly one owner at a time.** Owner stays the single `worker_daemon_id`
  column (NULL = the backend scheduler, "cloud"); there is no second owner
  column to disagree. Owner changes go through one guarded UPDATE
  (CAS on status plus from-owner) in
  [`lines.py`](../../src/server/database/autonomy/lines.py), and reassignment
  clears the lease triple so the previous daemon is fenced out before the
  new owner can claim.
- **Operations with an audit trail.** `stop` (existing cancel semantics),
  `resume` (cancelled → queued, Mandate revalidated like retry, launch
  effect re-enqueued), `reassign` (to another daemon — the ticket follows
  the daemon's provider — or back to cloud; running cloud threads are
  cancelled), and `archive` (new terminal status). Each writes a
  `autonomy_ticket_events` row (actor, action, from/to owner, detail) in the
  same flow, and the daemon claim path records `claimed`, so the trail
  shows when ownership took effect. All four operations live in
  [`ownership.py`](../../src/server/services/autonomy/ownership.py).
- **Lines.** `autonomy_lines` (name, branch, worktree_path) and a nullable
  `tickets.line_id` group Tickets for the dashboard; tickets on archived
  lines hide with their line rather than resurfacing as ungrouped.

Where it landed:

- Migration `120_ticket_lines_ownership.py` (down_revision 119,
  reversible; CHECK widened with `archived` per the 115 NOT VALID +
  autocommit-validate pattern, downgrade settles `archived` rows to
  `cancelled` first). Schema-head pins bumped to 120 in
  `test_refactor_status.py` and `test_refactor_migrations.py`; the head
  assertion in `test_119` now expects 120, following the newest-owns-head
  convention.
- API under `/api/v1/autonomy`: `GET /tickets/lines` (dashboard:
  status, owner, last activity = greatest of `updated_at` and latest
  event), `POST /tickets/{id}/resume|reassign|archive`,
  `PUT /tickets/{id}/line`, `GET|POST /lines`, `POST /lines/{id}/archive`;
  ticket detail now includes `events`. `archived` is folded into every
  open/terminal status check (watchdog candidates, subscriber liveness,
  daemon in-use guard, worker launch, report paths, reply gate).
- CLI: `conductor tickets lines [--include-archived] [--json]`.
- Web: read-only `/lines` panel (line cards, ticket rows with status chip,
  owner, relative last activity; 15 s polling).

Validation, all with fakes/mocks — no live provider, GitHub, backend, or
database calls:

- Backend unit suite: **16,396 passed, 28 skipped** after two pin fixes
  re-verified (migrations dir re-run: **266 passed**; the only other full-run
  failure, `test_main_agent_demo`, passes standalone and is a known
  timing-flaky demo unrelated to this change). New tests: migration AST/DDL
  (5), DB layer fake-cursor (12), ownership service (18), API routes (11),
  CLI contract + formatter (4).
- `ruff check` clean on every touched file.
- Web: **1,540 tests passed** (Vitest, full suite incl. the new LinesPage
  test and locale-key parity), `pnpm typecheck` clean, ESLint clean.

Not verified live: the new endpoints against a running backend (the local
stack was left untouched per the worktree rules), and the web panel against
a real API response — the contract is pinned by the shared types and the
mocked component tests instead.
