# ob-handoff — verified file handoffs between agents

Clean-room design (no external repo consulted). Source of the idea: a line item
in the legacy survey handoff list — agents hand files to each other through a
handoff *record*: a manifest (paths, sizes, sha256 checksums, producer,
consumer, purpose) stored with the Ticket; the consumer verifies checksums
before use and rejects tampered/missing files; paths confined to the workspace
root; no secrets in manifests.

## Goal

When one agent (a Ticket worker, a deliberation participant, the router)
produces files another agent must consume, the handoff must be **verifiable and
durable**:

1. The producer registers a manifest with the Ticket: file paths (relative to
   the workspace root), sizes, sha256 checksums, plus `producer`, `consumer`
   and `purpose` metadata.
2. The consumer resolves the manifest, re-hashes every file, and refuses the
   whole handoff on any mismatch, missing file, or path outside the workspace
   root. A partial/verified-until-failure handoff is never usable.
3. Manifests carry no secret material: names that look like credentials
   (`.env`, `id_rsa`, `*.pem`, `*token*`, `*secret*`, `*credential*`,
   `.git/`, `node_modules/`, ...) are rejected at registration, not at use.
4. Verification results are recorded with the handoff row (who verified, when,
   outcome) so an operator can audit the chain of custody without reading the
   files.

Scope of this port: the manifest model, the pure verify/path-confinement
service, durable storage (table + raw-SQL module), a daemon-authenticated API
surface for register/verify, and unit tests with fakes (no real Postgres, no
network, no real providers).

## Data model

New table `autonomy_ticket_handoffs` (migration `120`, chained after `119`):

| column | type | notes |
|---|---|---|
| `handoff_id` | UUID PK | `gen_random_uuid()` |
| `ticket_id` | UUID NOT NULL → `autonomy_tickets(ticket_id) ON DELETE CASCADE` | the owning Ticket |
| `user_id` | TEXT NOT NULL | denormalized for listing/ownership, as elsewhere |
| `producer` / `consumer` | TEXT NOT NULL | agent names, e.g. `worker:glm`, `deliberation:summarizer` |
| `purpose` | TEXT NOT NULL DEFAULT '' | short human reason, ≤ 500 chars |
| `status` | TEXT CHECK in (`registered`, `verified`, `rejected`) | set by the verify step |
| `files` | JSONB NOT NULL | `[{path, size, sha256}]`, path relative to the workspace root, forward slashes |
| `verify_error` | TEXT NOT NULL DEFAULT '' | first failure reason when rejected |
| `created_at` / `updated_at` | TIMESTAMPTZ | |
| `verified_at` | TIMESTAMPTZ NULL | set on success only |

Indexes: `(ticket_id, created_at)`, `(user_id, updated_at DESC)`.

Migration is reversible: `downgrade()` drops the table.

Manifest JSON (also the API wire shape):

```json
{
  "producer": "worker:glm",
  "consumer": "deliberation:summarizer",
  "purpose": "draft sections for review",
  "files": [
    {"path": "out/sections.md", "size": 1234, "sha256": "<64 hex>"}
  ]
}
```

## API

Mounted on the existing autonomy router (feature-gated like every other
route), plus daemon-authenticated endpoints — the daemon is the thing that
actually touches the workspace filesystem:

- `POST /api/v1/autonomy/tickets/{ticket_id}/handoffs` — user-facing register:
  validates the manifest (path confinement is checked syntactically here;
  file existence is not, the user-facing route has no filesystem access).
- `GET  /api/v1/autonomy/tickets/{ticket_id}/handoffs` — list handoffs of a
  Ticket (manifests included, secrets already impossible by construction).
- `POST /api/v1/autonomy/daemon/tickets/{ticket_id}/handoffs` —
  daemon-authenticated register used by the producing agent: computes
  size/sha256 from the real workspace file and rejects secret-looking paths
  *before* storing.
- `POST /api/v1/autonomy/daemon/tickets/{ticket_id}/handoffs/{handoff_id}/verify`
  — daemon-authenticated verify for the consuming agent: re-hashes every file
  against the stored manifest, marks `verified` or `rejected`, returns the
  outcome. Only `registered` handoffs can transition.
- `GET  /api/v1/autonomy/daemon/tickets/{ticket_id}/handoffs` — the daemon
  claims pending handoffs for its ticket.

There is deliberately **no CLI** in this port: the natural callers are agents
inside the daemon/worker loop, which already speak HTTP to the backend. A CLI
can wrap the user-facing routes later without a schema change.

## Service layer (`src/server/services/autonomy/handoff.py`)

Pure, filesystem-injected functions (async, but no I/O of their own beyond the
injected reader), so tests never touch a real workspace:

- `sha256_file(path)` — streaming hash (64 KiB chunks).
- `confine_path(root, rel)` — resolve `root/rel`, require the result to stay
  inside `root` (rejects `..`, absolute paths, symlinks that escape), and
  return the resolved path. Rejects secret-looking basenames/relpaths.
- `build_manifest_entry(root, rel)` — stat + hash one file into a manifest
  entry.
- `register_handoff(root, ticket_id, user_id, producer, consumer, purpose, paths)`
  → the stored row; rejects the whole batch if any path is unconfinable,
  secret-looking, missing, or a directory.
- `verify_handoff(root, handoff_row)` → outcome row; re-reads and re-hashes
  every file, single transition `registered → verified|rejected`.

Error types: `HandoffError` (base, safe message), `PathEscapeError`,
`SecretPathError`, `MissingFileError`, `ChecksumMismatchError` — all carry a
display-safe message with no file *content*.

## Gap mapping (idea → implementation)

| Idea clause | Where |
|---|---|
| manifest with paths, sizes, sha256 | `autonomy_ticket_handoffs.files` JSONB + `models.autonomy.HandoffFileEntry` |
| producer, consumer, purpose | columns on the same row |
| stored with the Ticket | FK to `autonomy_tickets`, listed under `/tickets/{id}/handoffs` |
| consumer verifies checksums before use | `verify_handoff` + daemon `verify` endpoint; only `verified` rows are consumable |
| rejects tampered/missing files | `ChecksumMismatchError` / `MissingFileError` → status `rejected` + `verify_error` |
| paths confined to workspace root | `confine_path` (resolve + containment + no-abs + no-`..`) |
| no secrets in manifests | `secret_looking_path()` checked at register time (both API paths) |

## Tests (all with fakes; no Postgres, no network)

- `tests/unit/server/services/autonomy/test_handoff.py` — confinement
  (`..`, absolute, symlink escape, symlink-to-inside is fine), secret-path
  rejection, missing file, checksum mismatch, happy path, no partial writes,
  manifest size caps, verify transitions.
- `tests/unit/server/database/test_autonomy_handoffs.py` — raw-SQL module
  against a fake connection/cursor (SQL shape, params, row mapping).
- `tests/unit/server/test_handoff_api.py` — register/list/daemon register/
  verify routes with a fake store and a fake filesystem root under
  `tmp_path`: 404 feature off, 404 wrong user, 400 bad manifest, 409 wrong
  state, verify happy/tampered/missing.
- Migration check: `120` chains after the current head `119` and both
  directions are plain SQL (covered by inspection + the schema-head test that
  the repo already runs).

Existing tests for touched modules are re-run alongside the new ones.
