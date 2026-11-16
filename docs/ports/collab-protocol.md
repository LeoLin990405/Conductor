# Collaboration protocol port (N80)

Status (2026-11-13): N80 line L1 (protocol package, schemas, migration 150,
storage helpers) is done; line L5 (rollout mode table, metrics script, this
file) is done. L2 (services and HTTP routes), L3 (MCP server and CLI) and L4
(claimable dispatch lines) are **planned, not built**. The rollout default is
`off`.

PRD: the internal planning note `n80-collab-protocol` (not published)
(identity §3, protocol §4, isolation §5.3, liveness §7, security §8, rollout
§9, hard boundaries §10, lines §11, Planner rulings §12). North star:
the internal planning note `north-star-collaboration` (not published)
§5 item 1 and milestone B.

The protocol is a thin layer (P1): every verb lands on an existing object
(Ticket, dispatch line, the N1 completion contract, the N17 Ticket worktree,
the peer channel, the review gates). It adds identity, leases, three
transports and an audit trail, nothing else.

## 1. What exists on this branch

| Part | Files | Line |
|---|---|---|
| Schemas (JSON source of truth) | `src/collab/schema/*.json` | L1 |
| Schema loading and validation | `src/collab/schemas.py` | L1 |
| Wire types, refs, error vocabulary | `src/collab/types.py` (`errors.py` re-exports it) | L1 |
| Scope table | `src/collab/scopes.py` | L1 |
| Response envelope, `untrusted_fields` | `src/collab/envelope.py` | L1 |
| In-process token buckets (fail closed) | `src/collab/ratelimit.py` (`rate_limit.py` re-exports it) | L1 |
| Payload secret refusal | `src/collab/payload.py` (`secrets_check.py` re-exports it) | L1 |
| Tables `collab_apps`, `collab_leases`, `collab_handoffs`, `collab_messages`, `collab_reviews`, `collab_audit` | `migrations/versions/150_collab_apps.py` (revision `150`, down `149`) | L1 |
| Raw-SQL helpers | `src/server/database/collab/` (`apps.py`, `audit.py`, `leases.py`, `handoffs.py`, `messages.py`, `reviews.py`, `transactions.py`) | L1 |
| Rollout mode table | `src/collab/modes.py` | L5 |
| Metrics report | `scripts/collab_report.py` | L5 |

Planned (PRD §11): L2 `src/server/services/collab/` and
`src/server/app/collab.py` (routes under `/api/v1/collab`, app-token auth,
claim CAS, sweep, report reconciliation); L3 `src/collab_mcp/`
(`conductor-collab-mcp`), `src/conductor_cli/apps_cli.py`,
`src/conductor_cli/collab_cli.py`; L4 `Line.app_claimable` and the machine
changes, queued behind the `machine.py` chain and needing the owner's approval.

## 2. Three surfaces, one protocol (PRD §4.1)

Every capability is exposed the same way on MCP, CLI and HTTP, with the same
JSON schema. The CLI verbs (`conductor collab …`) and HTTP routes are planned
(L3 and L2); the tool names and scopes below are already fixed in
`src/collab/scopes.py` (`TOOL_SCOPES`).

| MCP tool | CLI (`conductor collab …`, planned) | HTTP (`/api/v1/collab`, planned) | scope |
|---|---|---|---|
| `whoami` | `collab whoami` | `GET /whoami` | read |
| `task_list` | `collab tasks [--claimable \| --all]` | `GET /tasks` | read |
| `task_get` | `collab task <ref>` | `GET /tasks/{ref}` | read |
| `task_create` | `collab propose` | `POST /tasks` | propose |
| `task_claim` | `collab claim <ref>` | `POST /tasks/{ref}/claim` | claim |
| `task_heartbeat` | `collab heartbeat <lease>` | `POST /leases/{lease_id}/heartbeat` | claim |
| `task_release` | `collab release <lease>` | `POST /leases/{lease_id}/release` | claim |
| `task_report` | `collab report <lease> --file report.json` | `POST /leases/{lease_id}/report` | report |
| `handoff_request` | `collab handoff <lease>` | `POST /leases/{lease_id}/handoff` | claim |
| `handoff_list` | `collab handoffs` | `GET /handoffs` | read |
| `handoff_accept` | `collab accept <handoff>` | `POST /handoffs/{handoff_id}/accept` | claim |
| `agent_ask` | `collab ask <to>` | `POST /messages` | ask |
| `agent_inbox` | `collab inbox` | `GET /messages/inbox` | read |
| `review_request` | `collab review-request <lease>` | `POST /reviews` | report |
| `review_list` | `collab reviews` | `GET /reviews` | read |
| `review_submit` | `collab review-submit <review>` | `POST /reviews/{review_id}/verdict` | review |

`collab tasks` lists only tasks this app could claim now (`claimable` defaults
to `true` in `task_list.json`). `--claimable` states that default explicitly
(sends `claimable=true`); `--all` sends `claimable=false` and also lists open
tasks the app cannot claim, each with its `claim_refusal`. The two flags are
mutually exclusive.

Task references (`src/collab/types.py`, `TASK_REF_PATTERN`):

- `t:<ticket_uuid>`: a Ticket (`ticket_ref()`);
- `d:<dispatch_id>:<line_id>`: a dispatch line (`line_ref()`), readable now,
  claimable only after L4.

## 3. Schemas

- Location: `src/collab/schema/*.json`. Every file declares
  `"$schema": "https://json-schema.org/draft/2020-12/schema"` and is a closed
  object (`additionalProperties: false`). `src/collab/schemas.py` refuses to
  load a file that is not draft 2020-12 or not closed at the root, and
  `assert_schemas_are_draft_2020_12()` checks that every nested object node
  is closed too. Schemas cross-reference each other by absolute `$id`.
- The JSON files are the source of truth; another runtime can read them
  without importing Python.
- Validation: `validate_tool_input(tool, instance)` and
  `validate_tool_output(tool, instance)`. An unknown tool raises
  `InvalidSchemaError`; a bad instance raises the first `jsonschema`
  `ValidationError` (the caller maps it to `invalid_schema`). Nothing is ever
  repaired or coerced. `check_schemas()` meta-validates all files.

| Tool | Input schema | Output schema |
|---|---|---|
| `whoami` | `empty_input.json` | `whoami_result.json` |
| `task_list` | `task_list.json` | `task_list_result.json` |
| `task_get` | `task_get.json` | `task_get_result.json` |
| `task_create` | `task_create.json` | `task_view.json` |
| `task_claim` | `task_claim.json` | `task_claim_result.json` |
| `task_heartbeat` | `lease_input.json` | `lease_state.json` |
| `task_release` | `task_release.json` | `lease_state.json` |
| `task_report` | `task_report.json` | `task_report_result.json` |
| `handoff_request` | `handoff_request.json` | `handoff_request_result.json` |
| `handoff_list` | `handoff_list.json` | `handoff_list_result.json` |
| `handoff_accept` | `handoff_accept.json` | `handoff_accept_result.json` |
| `agent_ask` | `agent_ask.json` | `message_result.json` |
| `agent_inbox` | `agent_inbox.json` | `agent_inbox_result.json` |
| `review_request` | `review_request.json` | `review_request_result.json` |
| `review_list` | `review_list.json` | `review_list_result.json` |
| `review_submit` | `review_submit.json` | `review_submit_result.json` |

`handoff_accept_result` carries `heartbeat_every_seconds` (required, integer
≥ 1) like `task_claim_result`: the L3 heartbeat tracks a lease from either
answer with `data.lease_id` and `data.heartbeat_every_seconds`.

`envelope.json` composes the per-tool result schemas. `task_report.report` is
the N1 completion contract (`status`, `summary`, `artifacts`, `verification`,
`next`, `handbacks`); its size limits stay with `completion_report_from_text`.

### 3.1 Envelope

Every response (`src/collab/envelope.py`, `ok_envelope` / `error_envelope` /
`from_error`):

```json
{"ok": true, "code": "ok", "data": {}, "untrusted_fields": [], "redacted": 0}
```

- `code`: `ok` or one error code; an unknown code is rendered as
  `internal_error`. A refusal may add `message` and `retry_after`.
- `untrusted_fields`: paths whose text comes from another agent or an
  external source (task `body` / `acceptance`, message `text`, handoff
  `worker_reported` / `self_report` / `last_report`, review `verdict` /
  `focus`, and similar). The server computes the list from the shape it
  returns; it never trusts a caller to label its own text.
- `redacted`: number of fragments redacted on the way out.

### 3.2 Error codes

`ERROR_CODES` in `src/collab/types.py` (code → HTTP status): `unauthorized`
401, `scope_denied` 403, `mode_denied` 403, `rate_limited` 429, `not_found`
404, `not_claimable` 409, `already_claimed` 409, `assignment_refused` 409,
`lease_lost` 409, `lease_expired` 409, `stale_revision` 409, `self_review`
403, `invalid_schema` 400, `secret_in_payload` 400, `channel_full` 409,
`too_large` 413, `not_deliverable` 409 (PRD §6.4), `internal_error` 500.
Each has a `CollabError` subclass; `error_for_code()` builds one.

Report reconciliation reasons are not envelope codes
(`RECONCILIATION_REASONS`): `git_not_delivered`, `tests_failed`,
`out_of_scope`, `push_suspected`. A report can be accepted with a veto reason
that only changes the Ticket state.

## 4. Identity, scopes, audit

- **app_id** (`APP_ID_PATTERN`, e.g. `claude-code`, `codex`) is fixed at
  registration and is what authorizes, through its token. **agent** (e.g. a
  model name) is self-reported, audit and display only, never used for
  authorization. Actor strings are `app:<app_id>[/<agent>]`.
- A registration starts with `read` only (`DEFAULT_SCOPES`); each write
  scope is opened one at a time. An unknown scope or tool is refused, never
  defaulted (`normalize_scopes`, `scope_for_tool`).

| scope | tools |
|---|---|
| `read` | `whoami`, `task_list`, `task_get`, `agent_inbox`, `handoff_list`, `review_list` |
| `propose` | `task_create` (produces an `awaiting_clarification` Ticket only) |
| `claim` | `task_claim`, `task_heartbeat`, `task_release`, `handoff_request`, `handoff_accept` |
| `report` | `task_report`, `review_request` |
| `ask` | `agent_ask` |
| `review` | `review_submit` |

- Registration, scope changes, rotate and revoke (`conductor apps …` and the
  `cond apps` forwarder) are planned in L2/L3. The storage side exists:
  `src/server/database/collab/apps.py` (`create_app`, `update_scopes`,
  `rotate_token`, `set_enabled`, `revoke_app`, `list_apps`).
- Rate limits (`src/collab/ratelimit.py`, `POLICIES`): `read` 120/min, all
  write tools share 20/min, `agent_ask` also 10/min, `task_create` also
  5/hour. Buckets live in the server process; a missing bucket, a bad cost or
  a clock that raises or moves backwards denies.
- **Audit:** `collab_audit` gets one row per call, refusals included
  (`record_audit` in `src/server/database/collab/audit.py`). It stores app,
  agent, tool, target, `params_sha256` (a digest, never the parameters), code,
  HTTP status, `rate_limited`, `redacted` and duration. Claims, releases,
  expiries and handoffs also write `autonomy_ticket_events` in the same
  transaction (L2); a write that cannot be audited fails.

## 5. Trust model

- **Per-app local tokens distinguish cooperating local apps.** They are not a
  defense against a malicious local process that can read the keychain. The
  real boundary is the hard boundaries plus server-side checks.
- Tokens are stored only as SHA-256 digests (`collab_apps.token_sha256`;
  `hash_token` in `apps.py`). The plaintext is written once to the user vault
  and the keychain by the planned registration verb and never printed.
- **App tokens never reach a sandbox.** The vault row
  `COLLAB_APP_TOKEN_<APP_ID>` is withheld from every sandbox-bound read
  (`SANDBOX_WITHHELD_PREFIXES` in `user_vault_secrets.py`): the vault
  snapshot behind every PTC publish (provisioning, the settle loop, the push
  fan-out) and the named `${vault:NAME}` lookups of MCP servers, probes and
  the egress relay. The redactor still reads it, so a leaked value is still
  scrubbed. Rotation writes the digest and then the vault in one transaction.
- **Request bodies** are read only after the loopback and mode checks (on the
  admin routes, after the operator credential) and never past 256 KiB
  (`MAX_BODY_BYTES`, `too_large` 413). Refusals from callers that never
  authenticated get an audit row only within a per-peer bucket
  (`REFUSAL_AUDIT_POLICY`, 60 per minute; at most 256 peer buckets, the
  rest share one), so they cannot flood `collab_audit`.
- The tool routes under `/api/v1/collab/*` accept only app bearer tokens:
  never the internal service token, never an `X-User-Id` impersonation (L2).
  The MCP server never holds the internal service token (L3).
- **The operator routes (`/api/v1/collab/admin/apps*`) need an explicit
  operator credential on every call.** The normal user check is not enough:
  in OSS mode it answers the local user with no credential at all, so any
  local process, a registered app included, could register apps, grant
  itself scopes, rotate or revoke other apps' tokens and read the audit
  trail. Every admin route therefore requires the internal service token in
  `X-Service-Token` (with `X-User-Id`, as for every other service caller),
  compared in constant time with `INTERNAL_SERVICE_TOKEN` read at call time
  (`auth.require_operator_credential`). A missing or wrong credential, or a
  server with no `INTERNAL_SERVICE_TOKEN`, is `unauthorized` (401) with an
  audit row; an app bearer is refused even next to a valid service token.
  `conductor apps` (and `cond apps`) reads the token from
  `INTERNAL_SERVICE_TOKEN` in the environment or the checkout `.env`, sends
  it on the admin routes only, and never prints it.
- Loopback only: the MCP HTTP transport binds `127.0.0.1` with no address
  option, and the server routes check that the caller is loopback (403
  otherwise), independent of the server bind default.
- `untrusted_fields` are data, not instructions. They cover every
  app-authored field: task `title`, `body`, `acceptance[]`, `repo`,
  `write_scope[]`, messages, notes, focus, verdicts, self-reports, the prior
  report, `conversation_ref` and the caller's own `agent` label. Authorization never reads
  task or message text; a message claiming an approval has no effect.
- Payload secret refusal (`src/collab/payload.py`, `assert_no_secrets`):
  credential-shaped text in a stored payload is refused with
  `secret_in_payload`, not silently redacted and stored. Findings never echo
  the matched text. Output redaction happens again on the way out (L2).
- **H1 is checked after the fact (PRD §5.3, P7).** Conductor cannot sandbox
  an interactive app's process. Each claim gets its own worktree (N17
  `conductor/ticket/<id>` for Tickets; the line worktree after L4), and at
  `task_report` the server checks that commits are only on that branch, that
  every changed path is inside `write_scope` (the `path_ownership` decision)
  and that the worktree is clean. Committed paths are listed with rename and
  copy detection off (`diff --name-only -z --no-renames`), so a move counts
  its source as well as its destination and a deletion counts; new commits
  that change no path are `git_not_delivered`. Out of scope → the report is vetoed to
  `blocked` with reason `out_of_scope`. The worktree gets a refusing
  `pre-push` hook via a worktree-level `core.hooksPath`; an upstream on the
  branch is recorded as `push_suspected` and sent to a human.
- **H4 by app_id:** the author is the lease holder's `app:<app_id>`;
  `review_submit` from the same app_id is `self_review`, whatever the agent
  name. `collab_reviews` also carries a storage CHECK
  (`collab_reviews_not_self`).
- H1–H5 are unchanged (PRD §10); their existing tests are not edited.

## 6. Rollout modes (`src/collab/modes.py`)

`CONDUCTOR_COLLAB_MODE` is `off`, `shadow`, `docs` or `full`. The default is
`off`; an unset, blank or unknown value also resolves to `off` (case and
surrounding space are ignored). The table is pure: no database, no clock, and
it reads the environment only when the caller passes no mapping. Every route,
CLI verb and MCP tool asks `decide` before reaching a service; the lease sweep
asks `lease_allowed` after a mode change. The mode never grants a scope: an
app still needs the scope in its registration; the mode only takes scopes
away.

| mode | scopes allowed | task limit |
|---|---|---|
| `off` | none, every verb is `mode_denied` | - |
| `shadow` | `read`, `propose` | - |
| `docs` | `read`, `propose`, `claim`, `report`, `ask`, `review` | docs/tests tasks only |
| `full` | every scope | any `app_claimable` Ticket |

Rules:

- **Docs-stage task** (`is_docs_task`): every `write_scope` entry stays
  inside `docs/`, `tests/` or is a Markdown file (a glob such as
  `src/**/*.md` qualifies; a trailing wildcard does not); no entry hits
  `HIGH_RISK_GLOBS`, including patterns `CONDUCTOR_DISPATCH_HIGH_RISK_GLOBS`
  appends; the task is not `risk_bearing`. Absolute, `~`, backslash and `..`
  paths never qualify. An empty `write_scope` fails closed.
- In `docs`, the claim, report and review scopes (`TASK_BOUND_SCOPES`) need
  task facts that pass `is_docs_task`; this covers `task_claim`,
  `task_heartbeat`, `handoff_request`, `handoff_accept`, `task_report`,
  `review_request` and `review_submit`.
- `task_release` is exempt from the docs task limit (`RELEASE_TOOLS`), so a
  full-stage lease can still be given back after a downgrade to `docs`.
- `agent_ask` is checked against the docs limit only when it names a task.
- `task_claim` needs task facts and `app_claimable` in every mode that
  allows the claim scope; otherwise `not_claimable`.
- `d:` line claims are refused with `not_claimable` until L4 flips
  `LINE_CLAIMS_ENABLED` (currently `False`).
- `lease_allowed` drives the sweep: `off` and `shadow` expire every lease (no
  claim scope); `docs` expires non-docs leases; `full` keeps every lease.
  Whether the task is still `app_claimable` is a claim-time check only.

### 6.1 API

- Constants: `MODE_ENV = "CONDUCTOR_COLLAB_MODE"`; `OFF`, `SHADOW`, `DOCS`,
  `FULL`; `MODES = ("off", "shadow", "docs", "full")` (rollout order);
  `DEFAULT_MODE = "off"`; `MODE_SCOPES: dict[str, frozenset[str]]`;
  `TASK_BOUND_SCOPES`; `RELEASE_TOOLS`; `DOCS_ROOTS = ("docs/", "tests/")`;
  `DOCS_SUFFIX = ".md"`; `LINE_CLAIMS_ENABLED = False`.
- `TaskFacts(kind="ticket", app_claimable=False, write_scope=(),
  risk_bearing=False)`, frozen; `kind` is `ticket` or `line`; a string
  `write_scope` is a `TypeError`. `TaskFacts.from_mapping(value)` ignores
  unknown keys, accepts a single string path and treats `app_claimable` as
  true only when it is exactly `True`.
- `decide(tool, *, mode=None, task=None, environ=None, globs=None,
  lines_enabled=None) -> ModeDecision`. `task` may be `TaskFacts` or a
  mapping. Check order: `off`; unknown tool; scope in stage; `task_claim`
  claimability; docs limit.
- `ModeDecision(mode, tool, allowed, code="ok", reason="ok")`, frozen;
  `raise_for_refusal()` raises `NotClaimableError` for `not_claimable`,
  otherwise `ModeDeniedError`, with `details` `{mode, tool, reason}`.
- `require_mode(...)` (same arguments as `decide`, raises on refusal),
  `mode_allows(tool, *, mode=None, **kwargs) -> bool`,
  `is_docs_task(task, *, globs=None, environ=None) -> bool`,
  `lease_allowed(task, *, mode=None, environ=None, globs=None,
  lines_enabled=None) -> bool`,
  `allowed_tools(mode=None, *, environ=None) -> tuple[str, ...]` (sorted, by
  scope only, before any task limit), `resolve_mode(value=None, *,
  environ=None)`, `current_mode(*, environ=None)`, `mode_scopes(mode)`,
  `mode_rank(mode)` (index in `MODES`).
- Refusal codes: `mode_denied`, `not_claimable`. Reasons:

| reason | constant | code |
|---|---|---|
| `mode_off` | `REASON_MODE_OFF` | `mode_denied` |
| `unknown_tool` | `REASON_UNKNOWN_TOOL` | `mode_denied` |
| `scope_not_in_mode` | `REASON_SCOPE_NOT_IN_MODE` | `mode_denied` |
| `task_facts_required` | `REASON_TASK_REQUIRED` | `mode_denied` |
| `not_docs_task` | `REASON_NOT_DOCS_TASK` | `mode_denied` |
| `line_claims_not_enabled` | `REASON_LINES_DISABLED` | `not_claimable` |
| `not_app_claimable` | `REASON_NOT_APP_CLAIMABLE` | `not_claimable` |

An allowed decision has `code` and `reason` both `ok` (`REASON_OK`).

## 7. Rollout steps (PRD §9)

1. **shadow, at least one week:** `read` and `propose` only; every proposal
   is confirmed by a human. Watch per-app calls, refusal codes, rate limits,
   `untrusted_fields` coverage, redactions.
2. **docs, at least one week or at least 10 claims:** adds `claim`, `report`,
   `ask`, `review`, docs/tests tasks only. Targets: claims ending done and
   verified ≥ 70%, `out_of_scope` vetoes = 0. Also watch lease expiries,
   `self_review` refusals, handoff acceptance latency.
3. **full:** any `app_claimable` Ticket; dispatch lines after L4. Milestone B
   exit criteria are the acceptance.

The owner switches the mode (P10) and reads `scripts/collab_report.py` output
before each promotion. Rollback is setting an earlier mode, no code change;
switching to `off` expires every app lease and returns the tasks to the
Router. One misbehaving app is handled by removing its scopes or revoking it.

## 8. Metrics: `scripts/collab_report.py`

```
python scripts/collab_report.py (--dsn DSN | --from-json FILE) [--days N] [--now ISO] [--json]
```

`--days` defaults to 7. `--from-json` reads previously exported rows instead
of a database.

- **Read-only:** only `SELECT` statements inside a `READ ONLY` transaction;
  refuses a database named `postgres` (`src/conductor_cli/db_guard.py`);
  writes no file and no row; makes no network call.
- **Tables read:** `collab_audit`, `autonomy_ticket_events` (rows with
  `actor LIKE 'app:%'`), `collab_leases`, `collab_handoffs`.
- **Metrics:** per-app calls; refusals by code; rate-limited calls;
  redaction count; `self_review` refusals; claims; done-and-verified rate
  (leases whose ending state is `completed` / leases); lease expiries;
  `out_of_scope` vetoes; handoff acceptance latency (median, p95).
- **Counting rules:** days of data run from the first `collab_audit` row
  ever (one unwindowed `MIN(created_at)`), so the default 7-day window can
  still show a full shadow week. An `out_of_scope` veto counts once: a lease
  ending `out_of_scope` is skipped when the same Ticket already has an
  `out_of_scope` event. A failed connection exits 2 and prints only the
  error class, never the DSN.
- **Promotion checks:** `shadow → docs` needs at least 7 days of audit data;
  `docs → full` needs (at least 7 days or at least 10 claims), done rate ≥
  0.70 and `out_of_scope` == 0. The script reports; it never changes the
  mode.
- Non-loopback 401/403 refusals are not recorded in `collab_audit` (the
  table has no source-address column), so the report prints them as "not
  recorded".
- **Ticket-event contract L2 must write:** actor `app:<app_id>[/<agent>]`;
  `detail.collab_event` one of `claimed`, `released`, `lease_expired`,
  `handed_over`, `reported`; `detail.reason` carries a veto reason such as
  `out_of_scope`.

## 9. Tests

L1:

- `tests/unit/collab/test_collab_schema.py`, `test_collab_envelope.py`,
  `test_collab_scopes.py`, `test_collab_ratelimit.py`,
  `test_collab_payload.py`, `test_collab_home_isolation.py`;
- `tests/unit/database/collab/test_collab_db.py`,
  `test_collab_migration.py`.

L5:

- `tests/unit/collab/test_modes.py` (mode table);
- `tests/unit/scripts/test_collab_report.py` (metrics script).

All tests are offline: no network, no real tokens or keychain, no database
named `postgres`, HOME set to a temporary directory.

## 10. Known limits

Accepted after the N80 security review; left as they are on purpose.

- **L4: a report's revision fence is `>=`, not `=`.** The `moved` CTE in
  `record_lease_report` matches `t.revision >= h.revision`, while every
  requeue path uses `=`. If a third party bumps the Ticket revision (an
  operator edit, say) while it is `running`, `waiting` or `blocked`, the live
  holder's report still overwrites the status and adopts the new revision.
  This is intentional: it keeps the lease from being stranded after the
  `add_report` and input-delivery bumps.
- **L5: app review is advisory, not independent.** `author_app_id` is the
  lease holder at `review_request` time, so app A can do the work, hand off
  to B, have B request a review from A, and approve its own work. Review
  verdicts never replace the G2/G5/G6 gates.
- **L6: some lists read a fixed 50 rows before filtering.**
  `system_section` (`handoff.py`) reads the 50 oldest completed reviews
  across all apps, so `open_findings` go missing past 50. `handoff_list` and
  `review_list` fetch 50 rows and then filter by user, so other users' rows
  can crowd out the caller's.
- **L7: process-local limits and checks.** The rate limiter is per process
  (N server workers allow N times the limit). `task_report` free text is not
  secret-checked (the completion contract and output redaction still apply).
  A local reverse proxy that adds no forwarding header would make remote
  callers look like loopback; the current setup has no such proxy.
- **Task 6 changed-path gate.** `gates.git_changed_paths`
  (`src/tools/auto_dispatch/gates.py`) still lists paths with the default
  rename detection (`diff --name-only --no-ext-diff --no-textconv`); only the
  collab report moved to the `--no-renames` form. Its allowlist lives in
  both `git_delivery.py` and `execution.py`.
- **The owner can still reveal an app token** through the user vault API
  (`/vault/secrets/{name}/reveal`); only sandbox and worker publishes
  withhold it.
- **Refusal metering is per process and per peer address.** On loopback every
  local caller shares the `127.0.0.1` (or `::1`) bucket, so a flood also
  suppresses other unauthenticated refusal rows for that minute; the
  refusals themselves are still answered.

## 11. Dispatch lines (L4)

A dispatch line the Planner (or the owner) marks `app_claimable` can be taken by an
app instead of a Worker, only in `CONDUCTOR_COLLAB_MODE=full` *and* with
`src.collab.modes.LINE_CLAIMS_ENABLED` on (PRD §5.1, §9). While the switch is
off no app can reach a claim, so no line is held and the Planner is not told
it may mark lines. A marked line is held only once its dependencies passed.
The machine side is `src/tools/auto_dispatch/app_lines.py` (one service-owned
gates key, `gates.app_line_claims.<line_id>`); the service side is
`src/server/services/collab/lines.py`.

- **Verbs** (pure over one dispatch row; a refusal writes nothing):
  `claim_line`, `heartbeat_line`, `release_line`, `report_line`,
  `line_handoff`. `apply()` runs one inside `DispatchService.apply_mutation`
  with `resume=lambda: False`, so the save never starts a drive; a running
  drive reads the cell on its next step.
- **Claim order** (first failure wins): `mode_denied` (anything but `full`),
  `no_such_line`, `not_app_claimable`, `line_not_ready`,
  `dependencies_not_passed`, `already_claimed`, `claim_window_closed`,
  optional `stale_revision`, then N73 `check_app_claim` (`cannot_take_line`,
  `disabled`, `is_line_reviewer`; `reserved_role` skipped per P5), then
  `max_leases`. The audit entry (`assigned_by: app_claim`, the feasibility
  result and the skipped checks) goes into the cell's own `assignment_audit`
  list; `Line.assignment_audit` is unchanged.
- **Report** (H1 after the fact, P7): status must be `done`, `partial`,
  `blocked` or `needs_split` (`invalid_schema`); `partial` is a heartbeat
  only. The server reads the line worktree read-only (no fetch,
  `GIT_OPTIONAL_LOCKS=0`, timeouts): a changed path outside `write_scope` or
  unreadable paths -> `out_of_scope`; uncommitted changes -> `worktree_dirty`;
  head not the line branch tip or not a descendant of the base, or a `done`
  with no change -> `git_not_delivered`. A refused report keeps the claim
  `active`. A `done` report enters G2 on the machine's next step only after
  the machine reads the branch itself (`launcher.app_branch_facts`): the tip
  must still be the reported head, the tree clean and every path changed
  since the base inside `write_scope` (`tip_moved_after_report`,
  `worktree_dirty`, `out_of_scope`, `branch_facts_unknown` give the line back
  as `app_lease_released`). The app becomes the Stage-Gate developer; a
  refused `assign` / swap gives the line back (`stage_gate_refused`). Review,
  G5 and G6 are unchanged.
- **Worktree**: the machine prepares the line's own worktree for an active
  claim and installs the same refusing `pre-push` hook as the Ticket side
  (`install_push_guard`; `push_guard` in the cell says whether it is in
  place, which needs `extensions.worktreeConfig`).
- **Concurrency**: the drive adopts the stored cells, then saves each cell
  step on its own and only if the stored cell is still the one it decided
  from; a claim, heartbeat or report the service wrote in between wins and
  the step is decided again.
- **Leases**: 15 minutes, a heartbeat never extends past `max_lease_until`
  (4 h). Outside `full` every verb is `mode_denied` and the machine expires
  live cells (R-EXIT).
- **Handoff**: `line_handoff` returns the Conductor-built `system` facts
  (the M2c generator) and `conversation_ref: {app, session_id: null}`;
  Conductor never calls memfw (§6.3, P4).
- **Not wired yet**: the HTTP / MCP handlers still answer `d:` references
  with `not_found`, and `modes.LINE_CLAIMS_ENABLED` stays `False` (flipping
  it would let the mode table admit line claims in `docs`). Routing `d:`
  refs to `lines.py` needs dispatch ownership by `user_id`, the scope check
  and the audit row in the tool handlers; that is a follow-up.
