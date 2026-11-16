# ob-handoff — verified file handoffs between agents

Clean-room port. No external repository was read, fetched, or copied; the
design comes only from the idea statement: agents hand files to each other
through a handoff record — a manifest (paths, sizes, sha256 checksums,
producer, consumer, purpose) stored with the Ticket; the consumer verifies
checksums before use and rejects tampered/missing files; paths confined to
the workspace root; no secrets in manifests.

Design note: `docs/design/ob-handoff.md` (committed first).

## What was ported

There is no single legacy source file for this one — the idea was carried as a
line item, so everything here is new Conductor code designed from the idea
alone, following the conventions of the surrounding autonomy layer (raw-SQL db
module, service module, wire models, daemon-authenticated routes).

| Piece | File | Notes |
|---|---|---|
| Migration `120` | `migrations/versions/120_ticket_handoffs.py` | `autonomy_ticket_handoffs`, chained after `119` (the current head), reversible (drops one table) |
| Row store | `src/server/database/autonomy/handoffs.py` | `create/get/list` + `settle_handoff` fenced by `status = 'registered'`; `insert_handoff` cursor-local for future atomic ticket transitions |
| Service | `src/server/services/autonomy/handoff.py` | `confine_path`, `secret_looking_path`, streaming `sha256_file`, `build_manifest_entry`, `validate_manifest`, `register_handoff`, `verify_handoff` |
| Wire models | `src/server/models/autonomy.py` | `HandoffFileView`, `HandoffRegisterRequest` (daemon, paths only), `HandoffManifestRequest` (user, full manifest), `HandoffVerifyRequest`, `HandoffView`, `HandoffList`, `HandoffVerifyResponse` |
| API | `src/server/app/autonomy.py` | `GET/POST /api/v1/autonomy/tickets/{id}/handoffs` (feature-gated, user-owned); `GET/POST /api/v1/autonomy/daemon/tickets/{id}/handoffs`, `POST .../handoffs/{hid}/verify` (daemon-token gated) |

### Behavior

- **Register is all-or-nothing.** Any unconfinable, secret-looking, missing,
  or non-regular path refuses the whole handoff; nothing is stored.
- **The manifest is computed, not trusted.** The daemon route takes only
  workspace-relative paths and computes size + sha256 from the real files. The
  user-facing route accepts a supplied manifest (it has no filesystem access)
  but validates shape, caps, duplicate paths, and secret-looking paths.
- **Verify settles once.** `registered → verified | rejected`, fenced by the
  current status; a retry or a stale verify returns the settled row instead of
  flipping it. Rejection records the first reason (size mismatch, sha256
  mismatch, missing/symlinked file) without any file content.
- **Confinement.** Absolute paths and `..` are refused by shape; a symlink is
  refused before resolution (both at register and verify) so a manifest can
  never name an alias; resolved paths must stay under the workspace root. The
  daemon's `workspace_root` goes through the same boundary as a native CLI cwd
  (`resolve_native_cwd`: home, `/tmp`, or `NATIVE_CLI_WORKSPACE_ROOT`).
- **No secrets in manifests.** Names matching token/secret/password/passphrase/
  credential/api-key patterns, `.env`/`.pem`/`.key`/`.p12`/`.pfx`/`.kdbx`
  suffixes, identity files (`id_rsa`, `authorized_keys`, `.netrc`, ...), and
  `.git`/`node_modules`/`__pycache__`/`venv` parts are refused at registration
  — checked on both API paths and re-checked on verify.
- **Caps.** ≤ 256 files, purpose ≤ 500 chars.

## Tests run (all with fakes; no Postgres, no network, no providers)

New:

- `tests/unit/server/services/autonomy/test_handoff.py` — 43 passed
  (confinement incl. symlink escape, secret patterns incl. benign lookalikes
  like `tokenizer.py`, missing/dir/symlink entries, manifest validation caps,
  register all-or-nothing ×3, verify happy/tamper-same-length/missing/resize/
  symlink-swap/settle-once, streamed hashing of a >3-chunk file)
- `tests/unit/server/database/test_autonomy_handoffs.py` — 6 passed
  (insert shape + `Json`-wrapped params, get/list SQL + params, fenced settle
  for both outcomes, invalid settle target, row normalization)
- `tests/unit/server/test_handoff_api.py` — 11 passed (feature off → 404,
  other user's ticket → 404, register with manifest, secret path → 400,
  daemon register computes checksums, escape → 400, missing `workspace_root`
  → 400, verify happy/tampered/other-ticket)
- `tests/unit/migrations/test_120_ticket_handoffs.py` — 3 passed (chain after
  119, single-settle check constraint, cascade, reversible downgrade,
  single AST head `120`)

Existing suites re-run for touched modules:

- `tests/unit/server` — 8844 passed, 2 skipped (includes the autonomy API,
  services, db, models and migration suites)
- `tests/unit/migrations` + `tests/unit/scripts/test_refactor_status.py` —
  258 passed, 1 warning (head assertions bumped from `119` to `120`, matching
  the repo's pattern from the previous head bump)

Toolchain: repo venv absent; fallback
`../../Documents/deepseek-harness/default-workspace/AlphaCN/.venv/bin/python`
per instruction. `ruff check` and `ruff format` clean on all touched files.

## Gaps / next steps (not done here)

- **No CLI.** The natural callers are agents in the daemon/worker loop, which
  already speak HTTP; a `conductor handoff` command can wrap the user-facing
  routes later without a schema change.
- **Worker-loop integration.** The daemon does not yet call register/verify on
  its own; the routes are ready for `worker.py` to offer them to agents as a
  tool/protocol step.
- **Manifest does not pin the producer's identity** beyond the free-text
  `producer` field; signing (like the receipts work in the survey) would be
  the next hardening step.
- Verify checks files only; it does not check that the consumer actually used
  them (a `consumed_at`/receipt step would close that loop).
