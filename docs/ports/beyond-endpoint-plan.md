# Protect Endpoint Devices from Compromised Cloud Agent

**Problem:** A compromised cloud worker can use a daemon's broad API surface to execute any operation on an endpoint device.

**Design:** Isolate endpoint authorization from cloud trust. Each daemon holds an immutable local policy defining: (1) narrow operation allowlist per provider, (2) read-only enforcement for speculative tasks, (3) origin verification via signed lease grants and optional per-operation capabilities, and (4) expiring leases with server-side revocation.

## Concrete Changes

**Phase 1 (Narrow operations + local enforcement):** — implemented on `feat/beyond-endpoint-plan`
- Add `daemon_policy.yaml` per Computer defining provider-specific operation allowlists and read-only rules
- Extend `conductor_local_daemon.py` to load policy on startup, validate each Ticket's operation against it, and reject out-of-scope requests before CLI invocation
- Store policy version in lease; reject lease if policy changed since grant
- Enforce read-only enforcement for speculative tasks via adapter constraints (already wired for `codex`, `grok`, `stepfun`)

**Phase 2 (Origin verification):** — implemented on `feat/beyond-endpoint-p2`
- Signed lease grants: the server signs each lease with an Ed25519 private key named by a key id; the device keeps the public halves in a local trusted-key file that can hold more than one key, so a key can rotate
- The daemon checks the signature, the expiry, and the nonce before the native CLI runs, and refuses a grant it has already accepted
- Lease revocation: the daemon polls `/api/v1/autonomy/daemon/lease/{lease_id}/status` after the claim and before the CLI; the server revokes by setting `revoked_at`. A poll that does not return a live lease is a refusal
- The signed grant binds the daemon identity and the policy version. A report whose identity or policy version no longer matches the grant is rejected

**Phase 3 (Capabilities):** — implemented on `feat/beyond-endpoint-p3`
- Per-Ticket operation capability: a subset of the device's allowed operations, bound to the ticket id, the daemon id, the policy version and an expiry
- The daemon refuses an operation outside that capability before the native CLI runs
- The daemon classifies the CLI result with the same operation patterns and does not report a result that implies an operation outside the capability
- A missing, expired, malformed or mismatched capability fails closed
- Signing is not this phase. The daemon takes a `GrantVerifier` and refuses the Ticket when none is installed

## Files to Change

**Backend:**
- `src/server/models/autonomy/daemon.py` — add `policy_version`, `trusted_pubkey`, `revocations` fields
- `src/server/services/autonomy/daemon_policy.py` (new) — load, validate, sign lease grants
- `src/server/app/autonomy.py` — `/api/v1/autonomy/daemon/lease/{lease_id}/status` endpoint (new)
- `migrations/versions/*/` (new) — schema for `daemon_policy_revocations` table

**Local daemon (client):**
- `scripts/conductor_local_daemon.py` — load policy, validate operations, check signatures, poll revocation before claim
- `src/conductor_cli/daemon_policy.py` (new) — local policy loading and enforcement

## Tests

- Unit: policy load and validation; signature verification; operation allowlist enforcement
- Integration: revocation flow; concurrent grants/revocations; provider mismatch detection (already exists)
- Local: policy rejection of out-of-scope operation before CLI invocation

**Acceptance boundary:** Phase 1 validates the Ticket against the local policy. Phase 2 verifies that the lease was signed by a key the device trusts, and refuses expired, replayed, and revoked grants. Phase 3 additionally requires a per-Ticket capability and refuses anything outside it, before the CLI runs and again before a result is reported.
---

## What was implemented

The endpoint device holds the authority boundary. A daemon reads a policy file that never travels to the cloud, checks every claimed Ticket against it before the native CLI runs, and refuses a lease whose policy version no longer matches the file on disk. It also, when given the server's public keys, refuses a lease the real server did not sign. Phase 3, below, is the per-Ticket capability the daemon enforces once a verifier has authenticated the grant.

### Phase 1 — the local policy

The policy file is the whole mechanism:

```yaml
format: 1
read_only: false
read_only_operations: [repo.read, test.run]
operations:
  - operation: repo.read
    patterns: ['\bgit\s+(status|diff)\b']
  - operation: test.run
    patterns: ['\bpytest\b']
  - operation: repo.push
    patterns: ['\bgit\s+push\b']
providers:
  codex:
    allow: [repo.read, test.run]
    deny: [repo.push]
```

`operations` is the vocabulary: each entry names one operation and the request patterns that imply it. `providers.<name>.allow` is the narrow allowlist — an operation the provider does not list is refused even when the request also names an operation the provider may run, and `deny` wins over `allow`. A request that names no operation at all is refused unless the provider sets `allow_unclassified: true`. `read_only_operations` names the operations that cannot change the device; a read-only run (`read_only: true` in the file, `read_only: true` on a provider, or a speculative Ticket) accepts only those.

### What a Ticket is checked against

The daemon checks **two texts**, because two texts reach the CLI: the **Ticket's own request** — its title and scope — and the **launch prompt** the cloud sends next to it. Both get the same full verdict: an operation the provider may not run refuses the Ticket before the CLI starts, and so does a request or prompt that names no operation at all unless the provider sets `allow_unclassified: true`.

The prompt repeats the server's own instructions ("Do not modify files, run mutating commands, or push"), so a pattern that matches those instructions refuses every Ticket. Keep the patterns narrow: the shipped example matches `git push`, not `push`. The claim response therefore still carries the Ticket's `scope` next to its title, so the Ticket's own request can be checked on its own.

### Executable content is authorized by its command signature

Pattern matching alone would let a prompt smuggle a command the policy never named next to one it did: `git status && rm -rf .` names `repo.read`, and the second command names nothing. The daemon therefore parses each command line the way a small shell would and refuses any command the policy does not name, even with `allow_unclassified: true` (which admits prose, not executable lines):

- shell words are decoded before the command is judged: quote concatenation (`r"m"` is `rm`, `git p""ush` is `git push`) and backslash escapes are resolved, so normalized words cannot hide a command;
- a command line is split on `&&`, `||`, `|`, `;`, `&` and compound/control keywords (`{`, `}`, `!`, `if`/`then`/`else`/`fi`, loops, `case`, subshells); env-assignment prefixes are skipped;
- comments are dropped outside quotes, and quoted argument data is never command text;
- each command is authorized by its **signature** — the command word plus its non-argument continuation. A flag, path, expansion or assignment ends the signature, so `rm -rf ./victim git status` is the command `rm`, and the operation pattern has to match from the start of the signature. `echo git status` therefore does not become `repo.read`, and `write the file` stays a signature because no word is argument-shaped;
- the operations the command signature names are judged by the same allow, deny and read-only rules as the request, so a normalized `git push` is denied and a mutating command on a read-only run is refused;
- the parser only authorizes plain, fully resolved command lists. Shell syntax the daemon cannot resolve is unsupported and refuses the line: `$(...)`/backtick substitution, `$'...'`/escaped or otherwise expansion-generated command words, compound/control commands (`{ }`, `!`, `if`/`then`/`else`/`fi`, loops), and redirection. Redirection counts however it is spaced (`git status > victim` and `git status>victim` are the same write effect, including a redirection-only line such as `> victim`): output redirection is a write effect the policy does not name. List markers are stripped before a command is judged, so `- rm -rf ./victim` cannot pass as prose, and an unresolved command word is never treated as prose.

A line whose first command position is not a command word is prose and is left alone: the server prompt's inline folder reference (`` `.orchestrator/autonomy/<id>/` ``), its apostrophes and its "Do not modify files..." warning are prose, and the actual `worker_prompt(..., native=True)` passes both the executable check and the full verdict for ordinary and speculative Tickets.

### The read-only restriction reaches the adapter and the sandbox

A run that must not change the device is the strictest of three: `read_only: true` in the policy file, a provider's own `read_only: true`, or a speculative Ticket. All three now reach the CLI and the outer workspace sandbox: the request check refuses a mutating operation, the adapter runs read-only (Codex gets `read_only` with its own nested sandbox disabled under worker isolation), the outer isolation boundary keeps the workspace readable but no longer writable for that run (private CLI state directories stay writable), and a provider whose adapter cannot guarantee read-only is refused instead of run. A read-only run also refuses to start when a workspace policy names an extra runtime write path that overlaps the workspace root — the full policy root, not the Ticket's narrowed cwd — including aliases, case variants and ancestor spellings, so the CLI-state carve-out cannot be used to regain a workspace write grant.

### The policy file is read like the authority boundary it is

The file must be owned by the daemon user, mode 0600 or stricter, must not be a symlink, and must live outside every mount a Worker can write: the workspace, the isolation state root, and every extra runtime write path the workspace policy names. A Worker runs as the same user, so a policy under one of those mounts could be replaced between Tickets to widen the next Ticket's authority. Containment is checked with the case-aware overlap probe the isolation boundary already uses, so a case-variant or aliased spelling of a writable mount is caught. Unrecognised fields are a startup error rather than a silently dropped restriction (`denyy:` must not load as an ignored key).

### The lease is bound to the policy version

`policy_version` is a hash of the normalized policy: comments and key order do not change it, a changed allowlist does. The daemon reports it with every claim, the server stores it on the lease row, and the daemon compares the lease's version against the file on disk before it runs anything. A policy the device replaced after the grant refuses the lease instead of running it under rules the cloud never saw; a lease with no version at all (an older server) is refused too. Once a Ticket is admitted, that one policy snapshot is used for the capability gate, the read-only restriction and the result rule for the whole run; a file replaced mid-run is noticed on the next claim rather than silently pairing rules from two versions.

The lease row is `autonomy_daemon_claims.policy_version`, not the Ticket: `autonomy_tickets.policy_version` already holds the *autonomy* policy version, and the two must not be confused.

### Files

| Area | File | What changed |
|---|---|---|
| Policy model | `src/conductor_cli/daemon_policy.py` (new) | `load_policy`, `parse_policy`, `classify`, `PolicyStore`: the file format (unknown fields refused), its owner/mode/symlink validation, its version, the verdict for one request (`evaluate`), the verdict for an already classified set of operations (`evaluate_operations`), the effective read-only rule (`is_read_only`), and unclassified-executable-content detection (`unclassified_executable_segments`). |
| Policy template | `config/daemon_policy.example.yaml` (new) | A commented policy to copy onto a device. |
| Daemon | `scripts/conductor_local_daemon.py` | Loads the policy on startup (`--policy`, `--require-policy`), reports the policy version with every claim, refuses an out-of-scope operation, a prompt outside the policy rules, unclassified executable content, a policy inside a worker-writable path or a stale lease before the CLI runs, and runs the CLI under the effective read-only restriction. |
| Worker isolation | `src/llms/native_cli/isolation.py` | `worker_isolation(read_only=...)`; a read-only run keeps the workspace readable but drops its write grant (macOS Seatbelt profile and Linux `--ro-bind`), private CLI state stays writable, and a read-only run refuses a runtime write grant that overlaps the workspace. `paths_overlap`: public case-aware containment probe. |
| Daemon start | `src/conductor_cli/daemon.py`, `src/conductor_cli/cli.py` | `conductor daemons start --policy <path>` hands the policy path to the daemon. |
| Lease | `src/server/database/autonomy/daemons.py` | The claim, the resume and the replay all carry `policy_version`. |
| Service | `src/server/services/autonomy/worker.py` | `claim_daemon_ticket` passes the policy version down to the lease. |
| API | `src/server/app/autonomy.py`, `src/server/models/autonomy.py` | `DaemonClaimRequest.policy_version`; the claim response returns the lease's policy version and the Ticket's scope. |
| Schema | `migrations/versions/129_daemon_policy_version.py` (new) | `autonomy_daemon_claims.policy_version`. Follows revision 128 (integrate/r2 owns 125–128; the endpoint chain starts at 129), is reversible, and leaves a single head. |

### Tests

New tests, all with fakes and no network:

- `tests/unit/conductor_cli/test_daemon_policy.py` — 94 tests: loading and validation (missing file, broken YAML, unknown format, unknown top-level/operation/provider fields, empty providers, duplicate operation, an operation both allowed and denied, an unknown operation named by a provider, an invalid pattern), the private policy file (owner-only mode, no symlink, accepted when private), the version (stable across reads, unchanged by comments, changed by a changed allowlist), the verdict (allowed, denied, out of the allowlist, no operation at all, an unconfigured provider, read-only runs, speculative runs), the verdict for an already classified set of operations, the effective read-only rule, classification, the unclassified-executable-content split (prose inert, named command classified, unclassified and shell-chained and list-marked commands reported), the store re-reading a changed file, and the shipped example policy (loadable, refuses a push, allows a read).
- `tests/unit/scripts/test_conductor_local_daemon_policy.py` — 44 tests: an out-of-scope operation never reaches the CLI, an allowed operation does, a forbidden launch prompt never reaches the CLI, a launch prompt outside the allowlist is refused, an unclassified executable prompt and a shell-chained unclassified command never reach the CLI (also with `allow_unclassified: true`), the server's own warning does not block the run, a read-only policy, a read-only provider entry and a speculative Ticket all run the CLI read-only and restrict the outer sandbox, an ordinary Ticket keeps its write access and an ordinary run keeps the outer sandbox writable, a read-only run is refused for a provider that cannot enforce it, a policy inside a worker-writable workspace is refused, a lease without a policy version is refused, a lease granted under an older policy is refused, a policy replaced after the grant refuses the lease, an unreadable policy refuses the Ticket, a speculative Ticket may not mutate the device, the claim carries the version, a daemon without a policy keeps running everything, the policy file resolution (loaded, unusable, missing, absent), and the `--policy` path reaching the daemon process.
- `tests/unit/llms/test_native_cli_isolation.py` — three more tests: the read-only profile drops the workspace write grant and keeps private state writable; `worker_isolation(read_only=True)` carries the flag into the prepared workspace boundary and asserts the exact workspace mount; the read-only write-grant check uses the full policy root, not a narrowed cwd.
- `tests/unit/server/test_autonomy_daemon_lease_policy.py` — 5 tests: the claim endpoint returns the lease's policy version, hands the request's version to the service, and carries the Ticket's scope; the lease row records the version.
- `tests/unit/migrations/test_129_daemon_policy_version.py` — 3 tests: the revision chain, the added column, the reversible downgrade, and the single head.

Existing tests that had to move with the change:

- `tests/unit/scripts/test_conductor_local_daemon.py` — four `claim` stubs now take `policy_version`, because the daemon reports one with every claim.
- `tests/unit/server/services/autonomy/test_daemon_resume.py`, `tests/unit/server/services/autonomy/test_worker_slots.py`, `tests/unit/server/test_autonomy_api.py` — the claim/resume call assertions now include `policy_version`.
- `tests/unit/migrations/test_118_deliberation_groups_continue.py`, `test_119_channel_reply_approval.py`, `test_120_task_close.py`, `test_122_router_shard_peer_channel.py`, `test_refactor_migrations.py` and `tests/unit/scripts/test_refactor_status.py` — they asserted that 124 was the head of the migration chain; after integrate/r2 they assert the chain continues through 128 to 129 (Phase 1), and through 130 (Phase 2).

### Validation

- The server unit suite: `python -m pytest tests/unit/server` — 9179 passed, 2 skipped.
- The other touched modules: `python -m pytest tests/unit/migrations tests/unit/scripts tests/unit/conductor_cli tests/unit/test_provider_breaker.py tests/unit/test_provider_breaker_daemon.py tests/unit/test_provider_breaker_cli.py tests/unit/test_conductor_cli.py tests/unit/test_conductor_cli_operations.py` — all passed.
- New tests: 64 (39 + 17 + 5 + 3).
- The migration chain has one head: integrate/r2 ends at 128 (context scopes), Phase 1's local-policy migration is 129, and Phase 2 continues it at 130.
- Ruff passed on every changed and new Python file.

No credential was read, printed or copied. No network call was made to a real provider or to GitHub; the daemon, the API and the lease row were exercised through injected fakes. The live checkout, the running backend on `:8000` and the live database were not touched.

### Phase 2 — the lease was signed by the real server

A compromised cloud worker can still put a Ticket on the claim API. The device no longer treats that response as proof of origin. When the server is configured with a signing key, the claim returns a grant: key id, daemon identity, policy version, nonce, issued-at, expiry, and an Ed25519 signature over those fields. The device keeps only the public halves, in a file that can list more than one key id. A grant signed by the new key verifies while the old key is still listed, which is the rotation window; a grant signed by any other key is refused.

The private key stays on the server. `CONDUCTOR_DAEMON_GRANT_SIGNING_FILE` points at a JSON file `{"kid", "seed"}` that is readable only by the server user, or `CONDUCTOR_DAEMON_GRANT_SIGNING_KEY` is `kid:` plus the url-safe base64 seed. Neither value is sent to the device. A retried claim returns the grant already stored on the lease, so the signature does not change under a daemon that is checking it. That stored grant keeps the nonce the device consumed the first time it accepted the lease. `claim_ticket` replays a still-running lease for the same claim id, and `attach_lease_grant` returns that same grant, so a daemon that crashed after recording the nonce cannot resume the lease: the replay is a second presentation. The device refuses it. The lease ends at its expiry. Issuing a fresh nonce on replay would be a second signature for one lease, which is the case the stored grant exists to prevent. The refusal report can still land.

Before the native CLI runs, and only when the device has a trusted-key file, the daemon refuses the lease unless all of these hold:

- The grant is present and the signature matches the key id in the trusted-key file.
- `exp` is still ahead of the device clock, and the signed ticket id, lease id, and policy version are the ones in the claim.
- The nonce is not already in the device's replay file (`~/.local/state/conductor/daemon-grant-nonces.json`, or `--nonce-file`). The nonce is recorded only after the revocation poll succeeds, so a poll the device could not finish can be asked again. A second presentation of the same nonce is refused.
- `GET /api/v1/autonomy/daemon/lease/{lease_id}/status` returns `revoked: false`. A revoked lease, a body that does not say that, and a poll that errors are all refusals. The poll is one shot; it is not retried into a success.

The poll happens after the claim, because that is when the lease id exists. The plan's "before claim" is the moment before the device acts on the claim.

The owner withdraws a lease with `POST /api/v1/autonomy/daemon/lease/{lease_id}/revoke`, which sets `autonomy_daemon_claims.revoked_at`. The signed payload lives in `autonomy_daemon_claims.lease_grant` (migration 130, after 129). The column is not named `grant`: that word is reserved in Postgres, and an unquoted column of that name is a syntax error. The payload carries the daemon id and the policy version the lease was claimed under. Those are the lease columns, not `autonomy_tickets.policy_version`, which is still the autonomy policy. A completion on a signed lease is rejected when the daemon id or the policy version in the report differs from the grant and the row, including when the report omits them; the binding and the revocation flag are rechecked under the report transaction's claim-row lock, because the owner can revoke between the endpoint's precheck and the commit. A `failed` or `dropped` report may omit both. The daemon token already names the device, which is how a refusal (expired, replayed, mismatched, or a bad signature) and an early cancel land, including from a daemon that never verified the grant. A revoked lease still accepts only that refusal. The daemon's completion report sends the daemon id and the policy version the grant bound, not the policy file as it stands at report time, so a file replaced during the run does not leave the lease stuck. A device with no trusted keys does not check the signature. On a completion it echoes the daemon id and policy version the claim returned, which keeps the older path working.

The binding is checked under the same clock twice: expiry is verified before the revocation poll and again after it (the poll is an await), and a grant that expired while the poll was in flight is refused and not recorded as accepted. The nonce record is guarded by an interprocess `flock` around read-check-write, because two daemon processes share the replay file and a lost update would reopen the replay window. The record location is resolved and pinned once, and its parent is traversed with `O_NOFOLLOW`, so a parent directory symlink re-pointed later cannot redirect the replay ledger to a fresh one and resurrect a consumed grant. Expiry is checked once more immediately before the CLI starts.

The trusted-key file and the replay record are authority state, so the daemon requires them to live outside every Worker-writable mount — the workspace, the isolation root and the runtime write paths — using the same case-aware, alias-resolving overlap probe as the policy file. A Worker that could rewrite the trusted keys, or reset the replay record, would defeat origin verification.

The daemon optionally binds the signed identity to its own configured id (`--daemon-id`): when set, a grant that names another daemon, and a revocation status response that names another daemon, are refused locally.

Signing is off until a key is configured, and the trusted-key file is optional in the same way the policy file is. A device with no trusted keys does not check origin, and says so in the daemon log. `--require-origin` turns the missing file into a startup failure. A device that has the file refuses an unsigned grant before the CLI runs.

```json
{
  "format": 1,
  "keys": [
    {"kid": "2026-old", "public_key": "<base64url Ed25519 public key>"},
    {"kid": "2026-new", "public_key": "<base64url Ed25519 public key>"}
  ]
}
```

`config/daemon_trusted_keys.example.json` is that shape with a placeholder key.

### Files

| Area | File | What changed |
|---|---|---|
| Grant | `src/conductor_cli/daemon_grant.py` (new) | Sign and verify, the trusted-key file, the nonce file with an interprocess lock and expiry refusal. |
| Key template | `config/daemon_trusted_keys.example.json` (new) | The file format, with a placeholder public key. |
| Daemon | `scripts/conductor_local_daemon.py` | `--trusted-keys`, `--require-origin`, `--nonce-file`. Refuses a bad grant, then polls revocation, before the CLI. |
| Daemon start | `src/conductor_cli/daemon.py`, `src/conductor_cli/cli.py` | `conductor daemons start --trusted-keys <path>` hands the file to the daemon. |
| Signing | `src/server/services/autonomy/daemon_grants.py` (new) | Loads the server key, signs the first time, returns the stored grant after that, and rejects a completion whose identity or policy version diverged. A refusal may omit both. |
| Lease | `src/server/database/autonomy/daemons.py` | Stores the grant, reads it back, reports revocation, revokes an owner's lease. |
| API | `src/server/app/autonomy.py`, `src/server/models/autonomy.py` | The claim response carries `grant` when the server signs. `GET /daemon/lease/{lease_id}/status`, `POST /daemon/lease/{lease_id}/revoke`. The report echoes `daemon_id` and `policy_version`. |
| Schema | `migrations/versions/130_daemon_lease_grants.py` (new) | `autonomy_daemon_claims.lease_grant` and `revoked_at`, plus a unique index on `lease_grant->>'nonce'`. Follows 129 (local policy version), is reversible, and leaves a single head. |

### Tests

New tests, all with fakes and no network:

- `tests/unit/conductor_cli/test_daemon_grant.py` — 13 tests: a valid grant verifies; an expired grant, a wrong key, an unknown key id, and a changed field do not; a rotated key verifies while the previous key is still trusted; a nonce is refused the second time; an unsigned grant is refused; an expired nonce does not block a later grant; a corrupt nonce file fails closed; the trusted-key file holds two keys; a key file other users can write is refused; the shipped example loads.
- `tests/unit/scripts/test_conductor_local_daemon_grants.py` — 20 tests: a valid grant and a grant signed by the rotated key reach the CLI; an expired grant, a wrong key, a replayed grant, a revoked grant, an unsigned grant, and a revocation poll that errors do not. Origin files inside a worker-writable mount are refused before the CLI, a grant for another daemon is refused locally, a revocation status naming another daemon is refused, and a grant that expires during the revocation poll is refused without consuming its nonce. A daemon with no trusted keys still runs an unsigned Ticket, and reports a grant-carrying lease by echoing the claim. A refused grant and an early cancel report with no signed identity, and the server accepts that report. A policy file replaced during the run does not change the failure report. The key file resolves, and a required missing file stops the daemon.
- `tests/unit/conductor_cli/test_daemon_grant.py` — two more tests: an expired grant is refused by the replay store instead of silently discarded; two store instances sharing the replay file serialise read-check-write through the interprocess lock (the second cannot enter the read while the first holds it).
- `tests/unit/server/test_autonomy_daemon_grants.py` — 14 tests: the server signs a lease once and the claim response carries that grant; an unconfigured server does not sign; a completion must match the signed daemon and policy version; a refusal and an early cancel may omit that identity; a revoked lease accepts only the refusal; the status poll and the owner revoke endpoint report revocation.
- `tests/unit/migrations/test_130_daemon_lease_grants.py` — 5 tests: the revision chain, the `lease_grant` and `revoked_at` columns, the nonce index, the reversible downgrade, the single head, and a check that the migration and the lease queries do not use an unquoted reserved column (`grant` is the one Postgres would reject).
- `tests/unit/server/database/test_autonomy_daemons.py` — two more tests: a revoked claim row refuses the report under the commit transaction's claim-row lock before any insert; a matching unrevoked binding still commits.
- `tests/unit/server/test_autonomy_daemon_grants.py` — one more test: a refusal detected at commit time surfaces as a 409 instead of being silently dropped.
- `tests/unit/scripts/test_conductor_local_daemon_policy.py` — one more test: `--trusted-keys` reaches the daemon process.

Existing tests that had to move with the change:

- `tests/unit/server/test_autonomy_api.py` — the report for a Ticket that is not running stubs the lease binding, so an unsigned lease still reaches that check.
- Head-pin tests now name 130 as the migration head: `test_118_deliberation_groups_continue.py`, `test_119_channel_reply_approval.py`, `test_120_task_close.py`, `test_122_router_shard_peer_channel.py`, `test_126_ticket_handoffs.py`, `test_129_daemon_policy_version.py`, `test_130_daemon_lease_grants.py`, `test_128_context_scopes_a2a.py`, `test_refactor_migrations.py`, and `tests/unit/scripts/test_refactor_status.py`.

### Validation

- The default suite: `UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests -q -p no:cacheprovider` — 17406 passed, 49 skipped, 974 deselected, 201 warnings before this fix, and 17417 passed, 49 skipped, 974 deselected, 201 warnings after it.
- New tests: 49 (13 + 16 + 14 + 5 + 1).
- The migration chain has one head after Phase 2: revision 130, down_revision 129.
- On the merged branch (integrate/r2 @8d66c75c3 + Phase 1 @621c8d4a4): targeted tests for `tests/unit/migrations`, `tests/unit/scripts/test_conductor_local_daemon*.py`, `tests/unit/scripts/test_refactor_status.py`, `tests/unit/conductor_cli`, the autonomy API/service modules, `tests/unit/server/database/test_autonomy_daemons.py`, `tests/unit/llms/test_native_cli_isolation.py` and `tests/unit/llms/test_native_cli.py` all passed.
- The endpoint chain adds two migrations after integrate/r2's 128: 129 (local policy version) and 130 (signed lease grants); `alembic heads` reports the single head 130.
- The Phase 1 branch this port syncs against also adds the shell-parser (background separator, assignment prefix, comment laundering, command substitution, prose path reference and the real server prompt), read-only-mount and policy-location (runtime write paths, aliases) regressions, plus the isolation boundary asserting the exact read-only workspace mount; all of them pass on this branch.
- New tests: 130 in the Phase 1 branch (81 + 41 + 5 + 3) plus the isolation-boundary cases — the policy model now covers shell-word normalization and signature-only authorization (quoted, unquoted and argument-only matches), redirection, compound/control keywords and substitutions; Phase 2 adds its 61 grant, replay and revocation tests.
- New tests: 138 in the Phase 1 branch (88 + 42 + 5 + 3) plus the isolation-boundary cases — the policy model now admits only plain, fully resolved command lines (normalized words, signature-only authorization, unsupported substitutions/compound/redirection refused); Phase 2 adds its 61 grant, replay and revocation tests.
- Ruff passed on every changed and new Python file.

No credential was read, printed or copied. No network call was made to a real provider or to GitHub; the daemon, the API and the lease row were exercised through injected fakes. The live checkout, the running backend on `:8000` and the live database were not touched.

## What was implemented (Phase 3)

Phase 3 narrows Phase 1's per-provider allowlist down to one Ticket. The claim carries an operation capability: the operations this Ticket may run, the ticket id, the daemon id, the policy version and an expiry. The daemon does not trust that object on sight. A `GrantVerifier` authenticates it and returns the capability; the daemon then checks the binding itself and fails closed.
- Sync to current integrate/r2 plus the Phase 1 branch: the daemon now keeps workspace isolation, verified-stop and the Phase 1 policy checks together; Phase 2's signed-grant path layers on top. Phase 1's migration arrives as 129 and Phase 2's is renumbered 129 -> 130; `alembic heads` reports a single 130.
- After the second review round: targeted tests for the migrations, daemon, policy model, isolation, autonomy API and autonomy service modules pass; the two `blocking` findings have regression tests (unclassified executable prompt and shell-chained command never reach the CLI; a read-only run restricts the outer sandbox; a policy inside the workspace is refused; unknown policy fields are refused; a policy writable by others is refused).
- The whole suite is deliberately left to the integration owner; this port only runs the modules it changes.
- Ruff (`ruff check`) passes on every changed Python file.

The capability has to be a subset of what this run already allows. An operation the provider's allowlist does not name, an operation the policy denies, and a mutating operation on a read-only or speculative run are all refused, even when the Ticket's own request only asks for something the device would have allowed. The request is classified the same way as in Phase 1: from the Ticket's title and scope, and the launch prompt the cloud sends is checked under Phase 1's rules as well. The capability gate covers both texts and uses the same normalized command operations as Phase 1's executable validator, so `git "status"` counts as `repo.read` and cannot sneak past a capability that only names `test.run`. An operation in the request or prompt that the capability does not name never reaches the native CLI. The result-side rule uses the same normalized operations: a result line such as `git "status"` outside the capability is not reported, and neither is its text. When the result carries shell spelling the daemon cannot resolve (`$(...)`, expansions, redirection, unclosed quoting), the operation set would be incomplete, so the result is refused outright and its text is not forwarded.

After the CLI returns, the daemon classifies the result text with the same operation patterns. A result that implies an operation outside the capability is not reported. The failure that is reported names the operation and does not include the result text. A CLI failure is masked to its exception type by the merged workspace-isolation wrapper before the daemon classifies it, so no CLI failure text is forwarded; the capability rule still refuses a failure summary that names an operation outside the capability. The same check refuses a result when the capability has expired or the policy file on disk is no longer the version the capability was bound to.

No signature is checked here. `GrantVerifier.verify` is the seam Phase 2 implements. The daemon process installs no verifier of its own: `_grant_verifier` returns none, and a policy-backed daemon then refuses the Ticket rather than accept an unsigned grant. Tests inject a double that parses the capability body and does not authenticate it. There is no migration. The server still does not issue or store a capability; the daemon reads `ticket["capability"]` from the claim payload when a later phase puts it there.

A device with no policy file is unchanged. It still runs every operation the cloud asks for. A device with a policy must be started with `--daemon-id` (the `conductor daemons start` path passes the id it already has), because a capability is bound to that id.

### Files

| Area | File | What changed |
|---|---|---|
| Capability | `src/conductor_cli/daemon_capability.py` (new) | `OperationCapability`, `parse_capability`, `authorize`, `refuse_result`, and the `GrantVerifier` interface. Parsing does not authenticate. |
| Policy | `src/conductor_cli/daemon_policy.py` | `allowed_operations`: the set a capability is allowed to name for this run, including the read-only cut for a speculative Ticket. |
| Daemon | `scripts/conductor_local_daemon.py` | Refuses a Ticket outside its capability before the CLI runs, and refuses a result outside it before reporting. `--daemon-id`. No verifier is installed. |
| Daemon start | `src/conductor_cli/daemon.py` | The process is started with the daemon id a capability must name. |

### Tests

- `tests/unit/conductor_cli/test_daemon_capability.py` — the capability body (including a signature field the parser ignores), malformed grants, and each binding failure: expiry at the exact instant, wrong ticket, wrong daemon, the same daemon UUID in another case, a missing daemon id, a policy-version mismatch, an operation the run does not allow, a requested operation outside the capability, a speculative grant of a mutating operation, and a result that must not be reported.
- `tests/unit/scripts/test_conductor_local_daemon_capability.py` — the daemon, with a test double for the verifier: an allowed operation runs and its result is reported, a disallowed operation never reaches the CLI, an expired capability, the wrong ticket, the wrong daemon, a policy-version mismatch, a result outside the capability (the CLI text is not in the report), a CLI failure whose text names an operation outside the capability, a missing capability, a capability wider than the policy, a verifier that rejects the grant, no verifier at all, and a policy with no daemon id refusing to start. The gate also covers the executed prompt (a prompt outside the capability refuses the Ticket) and normalized command words in the request and result (`git "status"` counts as `repo.read`; a normalized result outside the capability is not reported and does not leak).
- `tests/unit/scripts/test_conductor_local_daemon_policy.py` — the Phase 1 cases that reach the CLI now present a capability the test double accepts. A Ticket the policy itself refuses is still refused first, with the same reason as before. The daemon id is on the process command line, and the CLI-reaching cases also satisfy the merged workspace-isolation fixture and the merged shell-parser/policy-location rules.

### Validation

- `UV_CACHE_DIR=$PWD/.uv-cache uv run pytest tests -q -p no:cacheprovider` — 17407 passed, 49 skipped, 974 deselected, 201 warnings.
- New tests: 39 (25 capability model, 13 daemon enforcement, 1 daemon id on the process command). The two Phase 1 cases that already reached the CLI now carry a capability.
- Alembic still has one head: 129 (Phase 1's local policy version migration). This phase adds no migration.
- Ruff passed on every changed and new Python file.

No credential was read, printed or copied. No network call was made to a real provider or to GitHub. The daemon was exercised through injected fakes. The live checkout, the running backend on `:8000` and the live database were not touched.


### What is not in this port

- **The server does not issue or store a capability yet.** The daemon reads `ticket["capability"]`; nothing in this branch's server writes that field, signs it, or stores it.
- **Origin verification is optional until the device has trusted keys and the server has a signing key.** A device with no trusted-key file keeps accepting unsigned leases, and says so in the daemon log. `--require-origin` turns the missing file into a startup failure. A server with no signing key returns no grant.
- **The policy is optional.** A device with no policy file keeps running every operation the cloud asks for, and says so in the daemon log. `--require-policy` turns the missing file into a startup failure for operators who want the stricter posture. A device that has a policy also needs `--daemon-id` and a verifier before it will run a Ticket.
- **Classification is pattern-based.** The daemon knows which operations a Ticket implies from the Ticket's own text and from the launch prompt, and which a result implies from the result text. Executable segments are refused when the policy cannot classify them (comments dropped, shell separators, assignment prefixes and command substitutions parsed), but natural-language instructions that hide their intent from every pattern are still read as the operations they name; the OS isolation boundary is what contains that residue, and a read-only run additionally cannot write the workspace. A result that mentions an operation outside the capability is not reported, including a mention that was only there to say the operation was not done.
