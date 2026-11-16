# ob-tickets: single-owner Tickets, ticket operations, and the line dashboard

Self-iteration line G7/G10, ported clean-room into Conductor's autonomy subsystem.
Source: the feature idea stated for this line only — no OpenBot or other external
code was read, fetched, or copied.

## Goal

Every autonomy Ticket has exactly one owner at a time — a daemon, or the backend
scheduler ("cloud") when `worker_daemon_id` is NULL. Operators can **stop**,
**resume**, **reassign** (to another daemon, or back to cloud), and **archive** a
Ticket, and every one of those operations leaves a durable audit row. On top of
that, a **line dashboard** groups Tickets by *line* (a named branch/worktree
pair) with each Ticket's status, owner, and last activity, exposed three ways:
an API endpoint, `conductor tickets lines`, and a read-only web panel.

## Data model (migration 120, down_revision 119)

- `autonomy_lines` — a line of self-iteration work: `line_id` UUID PK,
  `user_id`, `name` (UNIQUE per user), `branch`, `worktree_path`,
  `status` CHECK (`active`, `archived`), `created_at`/`updated_at`.
- `autonomy_tickets.line_id` — nullable FK to `autonomy_lines`
  ON DELETE SET NULL. Existing Tickets stay ungrouped.
- `autonomy_tickets` status CHECK widened with `'archived'` (terminal),
  using the 115 pattern: `NOT VALID` under a short lock, then
  `VALIDATE CONSTRAINT` in an `autocommit_block()` (upgrade only). Downgrade
  moves `archived` rows to `cancelled` before restoring the old CHECK, which it
  adds validated inside the command's transaction (see `MIGRATIONS.md`).
- `autonomy_ticket_events` — the audit trail: `event_id` UUID PK,
  `ticket_id` FK ON DELETE CASCADE, `user_id`, `actor` (`user`,
  `daemon:<name>`), `action` CHECK (`claimed`, `stopped`, `resumed`,
  `reassigned`, `archived`), `from_owner`/`to_owner` (daemon UUID or
  the literal `cloud`), `detail` JSONB, `created_at`; indexes on
  `(ticket_id, created_at)` and `(user_id, created_at)`.

Owner is not a new column: it stays `worker_daemon_id`, NULL meaning cloud.
"Exactly one owner at a time" is enforced structurally — there is only ever one
owner column, owner changes go through a single guarded UPDATE (CAS on status
plus from-owner), and a reassignment clears the lease triple so the previous
daemon is fenced out before the new owner can claim.

## Operations (service: `src/server/services/autonomy/ownership.py`)

All four operations validate the transition, write the ticket through a guarded
UPDATE, append an audit event in the same flow, and perform execution side
effects. Routes in `app/autonomy.py` stay thin.

- **stop** — existing semantics, now audited: open status → `cancelled`,
  `ended_at` set, running worker thread cancelled (cloud), daemon lease
  fences the daemon out via status.
- **resume** — `cancelled` → `queued`, `ended_at`/`error`/`attention` cleared,
  launch effect re-enqueued (daemon-owned tickets are skipped by the launch
  effect, so this is safe for both paths); Mandate revalidated like `retry`.
- **reassign** — any open status → owner swap (target daemon must exist and
  belong to the user; `daemon_id: null` means cloud), status → `queued`,
  lease cleared, cloud thread cancelled if it was running. Same-owner
  reassignment is an idempotent no-op.
- **archive** — any non-archived status → `archived` (terminal). Archived
  Tickets leave the open-ticket queries (`list_open_for_user`, subscriber
  liveness, the daemon in-use check), the scheduler ignores them (it only
  scans `queued`/`running`/`waiting`/`blocked`), and the dashboard hides
  them unless asked.

The daemon claim path additionally records a `claimed` event
(actor `daemon:<name>`) so the trail shows when ownership took effect.

## API (`/api/v1/autonomy`, same `super_agent` feature gate)

- `GET /tickets/lines` — the dashboard. Registered before `/tickets/{id}`.
  Returns active lines (each with `branch`, `worktree_path`, and its Tickets:
  `status`, `owner` — daemon name or `cloud` — `worker_provider`,
  `last_activity_at` = greatest of ticket `updated_at` and its latest event),
  plus an `ungrouped` bucket for Tickets with no line. Query:
  `include_archived` (default false).
- `POST /tickets/{id}/resume`, `POST /tickets/{id}/reassign`,
  `POST /tickets/{id}/archive`, `PUT /tickets/{id}/line`
  (body `{"line_id": ... or null}`).
- `GET /lines`, `POST /lines`, `POST /lines/{id}/archive`.
- `GET /tickets/{id}` detail gains `events` (the audit trail for that Ticket).

## CLI

`conductor tickets lines [--json]` — hits `GET /api/v1/autonomy/tickets/lines`;
plain output groups Tickets under `name (branch @ worktree)` headers with
status, owner, and last-activity columns; `--json` dumps the raw payload.

## Web

Read-only `/lines` panel in the superagent feature (`LinesPage.tsx`):
line cards with a Ticket table each (status chip, owner, provider, relative
last activity), `refetchInterval` polling like AgentsPage. API call +
types live in `web/src/api/autonomy.ts`; nav item + locale keys (en/zh).

## Gap mapping (idea → landing spot)

| Idea element | Where it lands |
|---|---|
| exactly one owner (daemon/agent) at a time | `worker_daemon_id` single-owner column + guarded reassign + lease clearing |
| stop / resume | `cancelled` ↔ `queued` transitions in the ownership service |
| reassign to another daemon/provider | `reassign` op (daemon target copies its provider; null → cloud) |
| archive | new terminal `archived` status + op |
| audit trail | `autonomy_ticket_events` + `events` on ticket detail |
| lines = branch/worktree groups | `autonomy_lines` + `tickets.line_id` |
| line dashboard (status, owner, last activity) | `GET /tickets/lines` → CLI + read-only web panel |

## Tests

- `tests/unit/migrations/test_120_ticket_lines.py` — AST chain test + DDL
  assertions (follows 120's own conventions; head moves 119 → 120, and the
  pin in `tests/unit/scripts/test_refactor_status.py` is bumped).
- DB layer (`tests/unit/server/database/autonomy/`): fake-cursor tests for
  line CRUD, the guarded reassign UPDATE, event recording, and the dashboard
  aggregation query.
- Service layer: fake `tickets_db`/`daemons_db`/dispatcher/effects — every
  op's transition guard, audit write, and side effect; multi-worker safety
  comes from the CAS guards, asserted at the SQL level.
- API: autonomy route tests in the existing style (feature gate 404s, 409 on
  CAS conflict, response shapes).
- CLI: MockTransport contract row for `tickets lines` plus a render test.
- Web: Vitest component test for `LinesPage` with the api module mocked.
