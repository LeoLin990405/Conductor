# MemFW integration (contract `memfw-http/2`)

Status: **integrated and available behind explicit configuration and the
`memory_federation` user feature**. The tracked defaults connect no external
source and make no HTTP request. Conductor Postgres (PG) remains the memory and
proposal authority; MemFW is an additional read-only recall source and an
optional candidate-staging destination.

This client implements the **`memfw-http/2`** contract. The file name is
historical; the wire contract is version 2 and is negotiated at runtime (see
"Version negotiation" below).

## Contract version

Every MemFW response carries `"contract": "memfw-http/2"`. Before Conductor
builds a remote source it calls `GET /health` (no authentication, one second
timeout) and checks the **major** version:

| Probe result | What Conductor does |
|---|---|
| major version is 2 | The remote source is built and used. |
| major version is not 2 (or `contract` is missing) | One warning per endpoint, then the remote source is **not** built and recall falls back to PG. |
| The request fails | The endpoint is treated as temporarily unavailable: no remote source, PG recall. The failure is **not** cached, so the next recall probes again. |

A successful probe (including a version mismatch) is cached for **five minutes**
per endpoint. A deployment with **no read token configured** makes no HTTP request
at all: a missing credential is a local configuration fact, so the client falls
back to PG without probing and logs a debug line, never a warning. An operator who
never configured MemFW is not warned about it on every turn.

## One address per endpoint: root addresses only

A configured MemFW endpoint — `memfw_url`, `MEMFW_READONLY_URL` or
`MEMFW_WRITE_URL` — may only be a **root address**: `scheme://host[:port]` with an
empty path or `/`. Anything else is refused **before a single request is made**,
and the stable reason is `bad_config`:

| Configured form | Result |
|---|---|
| `http://memfw.example.internal:18080` | accepted, reduced to that base |
| `http://memfw.example.internal:18080/` | accepted; the trailing slash is normalised away |
| `http://[::1]:8765` | accepted; an IPv6 literal keeps its square brackets |
| `http://user:secret@memfw.example.internal:18080` | `bad_config` — a userinfo is refused |
| `http://memfw.example.internal:18080/v1?token=…` | `bad_config` — a query is refused |
| `http://memfw.example.internal:18080/v1#frag` | `bad_config` — a fragment is refused |
| `http://memfw.example.internal:18080/v1` | `bad_config` — a path of its own is refused |
| `not a url at all` | `bad_config` — it cannot be read into a scheme, a host and a port |

A configured path has nowhere to go: dropping it would silently retarget the
request, and keeping it would ask a URL the service does not serve. It is refused
instead, so a misconfigured endpoint cannot pass `/health` and then have its
`/search` or `/related` calls land somewhere else. `bad_config` is a local
configuration fact, not an outage: nothing is sent, no warning is emitted, and a
log line names only the reduced scheme, host and port.

After validation there is exactly **one** normalized base per endpoint, and every
request line is rebuilt from that base plus the one fixed contract path of its own
call — `/health`, `/search`, `/related`, `/stage`. There is no exception to that
rule, and the raw configured string reaches neither a request nor a log line.

### Redirects are never followed

A redirect is not an answer to the request that was sent: following it would make
a second request to whatever host the `Location` header names, with the read token
still attached to it. No client here follows one, so the ruling above holds with
no exception for a 3xx:

- the Conductor httpx clients are constructed with `follow_redirects=False`
  (the httpx default, stated explicitly at each construction site);
- the Notes MCP script sends every request through a **private** `urllib` opener
  whose redirect handler refuses — `redirect_request` returns `None` — and it
  never installs a process-wide opener, so no other HTTP user in the interpreter
  changes behaviour.

The Notes MCP script reports a 3xx as the stable reason `redirect_refused`: no
request is made to the `Location` target, so the read token is never forwarded to
a host the configuration did not name. When `/health` is the call that got the
3xx, the probe **fails**: the answer is **not** cached (the next call probes
again) and no `/related` request follows it. The Conductor clients keep the
contract's own vocabulary and map a 3xx through the status table, where 3xx has
no entry, so a refused redirect surfaces there as `internal` — still with no
second request and no token forwarding.

## Read and approval paths

A user binding selects exactly one source and assigns it to exactly one owner:

```yaml
memory_federation:
  user_bindings:
    local-dev-user:
      memfw_url: http://memfw.example.internal:18080
      memfw_tenant: team-alpha
```

Use `memfw_path: /absolute/path/to/vault` instead of `memfw_url` for a local
vault. Supplying both or neither is invalid, and two users cannot share one path
or URL. URL identity is normalized before this check: scheme and host are
case-folded, default ports are explicit, and `localhost`, `127.0.0.1` and `::1`
are treated as the same loopback peer. Different ports remain different sources.
`local-dev-user` is the OSS local user. The user must also opt in:

```bash
curl -X PUT http://127.0.0.1:8000/api/v1/features/memory_federation \
  -H 'Content-Type: application/json' \
  -d '{"enabled": true}'
```

Restart Conductor after changing its YAML binding. Local-vault reads are bounded,
read-only and event-chain verified. Remote reads use `POST /search`, return at
most **50** hits and truncate each injected text to 500 characters. Hits retain
`source=memfw` and source metadata. The server's `score` is used directly
(clamped to `[0, 1]`); the raw bm25 is kept in `provenance["raw_score"]`.

MemFW's storage tiers (`hot` / `cold`) are not Conductor recall tiers. Every
remote hit enters Conductor as `tier="user"`; the original value remains in
`provenance["memfw_tier"]` and `memory_metadata.tier`. The remote provider
answers only the `user` tier, so other Conductor tiers make no HTTP request. A
per-turn cache shares one remote search across tier selection and repeated user
queries; a later smaller limit reuses the cached rows instead of calling again.

Current recall excludes expired and superseded documents. It reads the canonical
MemFW fields `valid_to` and `superseded_by`, plus Conductor's aliases
`valid_until` and `superseded`; V1 does not expose `as_of` or history queries.

`memfw_tenant` is optional and may only be combined with `memfw_url` (a local
vault has no tenant). It is at most 120 characters. When it is set, it travels
in the `/search` request body; the approve hook also uses it as the tenant of a
staged candidate when the proposal itself carries none.

By default, MemFW and PG recall are federated. With `MEMFW_PRIMARY=1`, a
successfully constructed MemFW source is the only recall provider. If that
source cannot be constructed, PG recall remains active, and the reason decides
whether a warning is emitted: no read token configured or an unreachable
endpoint is a **debug** line only, an endpoint that speaks another contract
major is **one warning per endpoint** (from the version negotiation), and an
unexpected failure while building the source is **one warning per process**. A
deployment that never configured MemFW is therefore not warned about it on
every turn.

If a primary remote request times out or returns a non-2xx response, that
recall falls back to PG; request-failure warnings are limited to one per
process every five minutes and carry the contract's error code but no query
text, token or URL. A normal empty remote result is not a failure and does not
trigger PG fallback. PG always remains the proposal writer regardless of this
switch.

The optional write bridge runs only after PG approval succeeds:

```text
Conductor proposal -> PG human approval and projection
  -> MemFW POST /stage -> _candidates/
  -> separate MemFW human confirmation -> authoritative Markdown + event
```

A staging error is returned in `memfw_staging`; it does not roll back the
approved PG memory. The bridge never writes authoritative MemFW Markdown or
approves a candidate. Staging requires both `MEMFW_WRITE_BRIDGE=1` and the
current user's explicit `memfw_url` binding. A missing binding or a local-only
`memfw_path` disables remote staging. A proposal marked `sensitivity: secret`
is refused with `secret_forbidden` before any request.

## Configuration

Credentials and URLs belong in `.env` or the process environment. Behavioral
bindings belong in YAML. Every variable is optional; empty means the associated
external capability is not configured.

| Variable | Default | Purpose |
|---|---|---|
| `MEMFW_PRIMARY` | `0` | Use MemFW as the only recall provider when it constructs successfully; PG remains the proposal writer. |
| `MEMFW_READONLY_TOKEN` | empty | Inline read token for the backend process itself (remote `/search`); the backend read resolver tries it before the file. It is not forwarded to the Notes MCP subprocess, and it is **not** accepted by `/stage`. |
| `MEMFW_READONLY_TOKEN_FILE` | empty | File containing the read token. This is the only token source supplied to the Notes MCP subprocess for `/related`. |
| `MEMFW_READONLY_URL` | empty | Read-only service base URL for the Notes MCP `memory_related` tool. The backend remote binding comes from `memfw_url`, not this variable. |
| `MEMFW_WRITE_BRIDGE` | `0` | Enable candidate staging after PG approval. |
| `MEMFW_WRITE_URL` | ignored | Legacy setting with no effect. The `/stage` target is always the current user's `memfw_url` binding. |
| `MEMFW_WRITE_TOKEN` | empty | Inline `/stage` token. It is the **only** token `/stage` accepts; there is no fallback to the read token. |
| `MEMFW_WRITE_TOKEN_FILE` | empty | File containing the `/stage` token; takes effect after the inline write token. |
| `MEMFW_CLI` | empty | MemFW CLI executable used by Notes MCP `notes_related`. |
| `MEMFW_RELATED_DB` | empty | SQLite related-note projection used by Notes MCP `notes_related`. |

`FLEET_MEMORY_HOST` is inherited by the `memfw-fleet notes search` subprocess.
`MEMFW_READONLY_TOKEN[_FILE]` is used for reads; `MEMFW_WRITE_TOKEN[_FILE]` is
used **only** for staging. No token value is stored in tracked configuration.

### Read and write tokens are separate — and there is no fallback

The contract splits the two tokens: `/search` and `/related` accept either, while
`/stage` accepts **only** the write token. A deployment that relied on the old
"no write token means use the read token" behaviour stops staging after this
upgrade, and it must say so rather than write with the read token (which the
service answers with `403 forbidden`).

**Operational impact: add `MEMFW_WRITE_TOKEN_FILE` to `.env`.** Without it the
bridge returns `error_code: "write_token_missing"` and sends **no request at
all**:

```dotenv
MEMFW_WRITE_TOKEN_FILE=/absolute/path/to/memfw-write-token
```

The read token is never substituted, so a misconfigured deployment cannot write
with a credential that was only meant for reading.

The token resolver itself still checks `MEMFW_READONLY_TOKEN` and then
`MEMFW_READONLY_TOKEN_FILE` in each process. However, the tracked `memfw-notes`
entry declares only `MEMFW_READONLY_TOKEN_FILE`: an inline token set for the
backend is therefore absent from the Notes MCP subprocess environment and cannot
authenticate `memory_related`. Configure the token file for Notes MCP even when
the backend uses an inline token; do not add `MEMFW_READONLY_TOKEN` to the MCP
entry or to the connector's global environment allowlist.

### Error codes

Failures are reported with the contract's vocabulary, not with an exception
class name. The server sends `{"contract": "memfw-http/2", "error": {"code": ...,
"message": ...}}`. **Only a valid v2 envelope passes its `code` through**: a body
with no `contract`, or with a v1 or a v3 contract, is not a MemFW v2 error
envelope at all, so its code is ignored and the HTTP status maps through the
status table instead. A transport failure has no envelope and never had a
message worth passing on, so it is reported as the client's own `timeout` or
`unreachable`, with the text of the failure dropped.

| Code | Meaning |
|---|---|
| `bad_request` | The request violated the contract (HTTP 400); also a client-side `proposal_id` that does not match `[A-Za-z0-9_-]{1,128}`. The id is matched as a **whole string**: it is never trimmed and never shortened, so an id with a space, a tab or a newline around it is refused and **no request is sent**. |
| `unauthorized` | HTTP 401 — no token, or the wrong one. |
| `forbidden` | HTTP 403 — the token may read but not write. |
| `not_found` | HTTP 404. |
| `conflict` | HTTP 409 — the same `proposal_id` already exists with different content. |
| `too_large` | HTTP 413; also a client-side size check that refused to send. |
| `internal` | HTTP 500, and any status outside the contract table. |
| `bad_config` | Client-side: the configured endpoint was not a root address (a userinfo, a query, a fragment or a path of its own); **no request was sent**. See "One address per endpoint". |
| `redirect_refused` | Client-side (Notes MCP): the request was answered with a 3xx, which is not followed, so the `Location` target was never requested and the read token was never forwarded to it. |
| `timeout` | Client-side: the request exceeded its timeout, or the transport timed out. |
| `unreachable` | Client-side: the endpoint could not be reached (a refused connection, or any other transport error), or no write URL is configured. |
| `invalid_response` | Client-side: the body was not the contract shape, or the `contract` field was missing or had the wrong major version. |
| `write_token_missing` | Client-side: `/stage` was requested without a write token; no request was sent. |

Where each one surfaces:

- **recall** — the code is written to the log line and the recall falls back to PG.
  The line carries the code and nothing else: no query text, no token, no
  exception text, and no endpoint URL beyond its scheme, host and port.
- **write bridge** — the result carries `error_code`; `memfw_staging.error_code`
  in the approve result keeps it verbatim. A staging failure never rolls back the
  approved PG memory.
- **Notes MCP** — the tool answers `{"ok": false, "error_code": ...}`.

### Size limits are checked before sending

The client refuses to send an oversized request; it never truncates the content
and then sends it.

| Item | Limit |
|---|---|
| `content` | 64 KiB (65536 UTF-8 bytes) |
| whole `/stage` JSON body | 128 KiB (131072 bytes) |
| `/search`, `/related` body | 8 KiB |
| `title` | 200 characters |
| `proposal_id` | `[A-Za-z0-9_-]{1,128}` — matched as a whole string, never trimmed and never truncated |

An oversized request returns `error_code: "too_large"` and logs one warning that
contains the byte counts only, never the content.

### Scores, limits and timeouts

| Item | Value |
|---|---|
| `/search` `limit` | 1..50 (clamped, never an error) |
| `/related` `limit` | 1..50 (clamped) |
| `/search` `query` | 1..512 characters (truncated client-side) |
| `/health` timeout | 1 s |
| `/search`, `/related` timeout | 1 s |
| `/stage` timeout | 3 s |

These are deadlines for the **complete request**, not only connect/read
operations. `asyncio.wait_for` bounds the whole health, search and stage calls,
so a peer that drips bytes cannot hold a turn beyond the stated deadline.

### Remote forget is explicitly unsupported

The contract does not offer a forget endpoint. Conductor therefore answers with
the explicit status `unsupported` and a separate reason key, never
`unavailable` (which would suggest a temporary failure) and never `read_only`
(which would suggest a policy):

```json
{"memfw": "unsupported", "memfw_reason": "remote memfw has no forget endpoint"}
```

The status survives the whole way. The forget result carries `unsupported` for
the memfw provider, the provider notice that is written carries `unsupported` as
well, and the stored notice keeps it: revision 142 widened the CHECK on
`memory_forget_provider_notices.status` with the new status, and its downgrade
rewrites the rows that carry it back to `unavailable` before the previous CHECK
returns, because the previous build cannot read them.

The reason is a note about the provider, not a provider of its own. It rides
with the record of the provider it explains — in the result as
`federation:memfw:<entry>:reason` — and it is never counted or shown as a
provider of its own.

The reason also survives the round trip through the database. Revision 142 adds
a nullable `memfw_reason` column to `memory_forget_provider_notices`, with a
CHECK that admits only the three forget reason codes
(`no_forget_endpoint`, `not_supported`, `remote_rejected`) or `NULL`, so a
reason no forget ever gives is stored as `NULL` rather than as free text; the
write logs only the stable code `reason_not_in_vocab`, never the value. Those
three are the *forget* vocabulary: `no_forget_endpoint` is what a URL-bound
MemFW answers, because v2 offers `/search`, `/stage` and `/related` and no
forget endpoint at all; `not_supported` is the remote saying so itself;
`remote_rejected` is the remote refusing. They are not the `/related`
`reasons` vocabulary — `link`, `lexical` and the rest say why one memory sits
next to another and have nothing to do with a forget.

The column holds a code, not the remote's wording: the write path maps the
federation's reason string onto one of the three and persists only that, so a
read-back is stable whichever words the remote used. Reading a notice back
returns the reason beside the status — an owner-scoped SELECT of
`memory_forget_provider_notices` for the entry, returning `provider`, `status`
and `memfw_reason` — and it returns `NULL` where the ruling was outside the
vocabulary. The downgrade drops the column before it rewrites `unsupported`
and restores the previous CHECK, because the previous build has no such column.

A forget whose provider cannot forget is reported as `partial`, not
`completed`: `completed` means every copy of the memory is gone, and a provider
that cannot forget is not a provider whose copy is gone.

A locally bound vault keeps its current forget behaviour.

### Local overlays and post-merge operation

`APP_ENV` layers an ignored sibling YAML file over each base config. For example,
`APP_ENV=production` loads `config.production.yaml` and
`agent_config.production.yaml`. Nested maps merge; lists replace. These overlay
files are gitignored. Restart the backend after creating or changing them; a dev
Compose deployment that bakes overlays into its image must rebuild the backend
image instead.

The tracked `memory_federation.user_bindings` is intentionally empty. **The one
required post-merge operation for an existing remote-recall deployment is to
move its binding into the active local overlay before restarting.** To reproduce
the current machine's remote binding with `APP_ENV=production`. This command is
only for creating a new overlay: it stops if the file already exists rather than
overwriting it. Edit an existing overlay manually instead.

```bash
cd /path/to/conductor
test ! -e config.production.yaml || {
  echo "config.production.yaml already exists; edit it manually" >&2
  exit 1
}
cat > config.production.yaml <<'YAML'
memory_federation:
  user_bindings:
    local-dev-user:
      memfw_url: http://100.64.0.101:18080
YAML
```

Ensure the existing `.env` selects that overlay and retains the read token file;
do not add a token value to these configuration files:

```dotenv
APP_ENV=production
MEMFW_READONLY_TOKEN_FILE=/Users/you/.config/conductor/memfw-token
```

For the current machine's Notes MCP behavior, set the local environment as
follows, adjusting only the Conductor checkout used by the merged deployment:

```bash
export FLEET_MEMORY_HOST=nas
export MEMFW_CLI="$HOME/Documents/Codex/memfw-core/.venv/bin/memfw"
export MEMFW_RELATED_DB="$HOME/Documents/Codex/memfw-data/qmd-notes/related-qmd.sqlite"
export MEMFW_READONLY_URL=http://100.64.0.101:18080
export MEMFW_READONLY_TOKEN_FILE="$HOME/.config/conductor/memfw-token"
```

The tracked MCP entry runs `python3 scripts/memfw_notes_mcp.py` from the
Conductor checkout. If the deployment needs the original interpreter, use an
ignored `agent_config.production.yaml` to replace `mcp.servers` with the same
`memfw-notes` entry but with:

```yaml
      command: "/Users/you/conductor-wt/int-r2/.venv/bin/python"
      args: ["/absolute/path/to/merged-conductor/scripts/memfw_notes_mcp.py"]
```

Keep the entry's description, instruction, `tool_exposure_mode: summary` and
`transport: stdio`, and pass the environment values above in its `env` map.
To enable the optional staging bridge on the same service, set
`MEMFW_WRITE_BRIDGE=1`. The target remains the current user's `memfw_url`
binding; the legacy `MEMFW_WRITE_URL` is not consulted. Configure a dedicated
`MEMFW_WRITE_TOKEN[_FILE]`. Without a write token the bridge refuses to stage
and reports `write_token_missing`; it never falls back to the read token.

## Notes library (independent read-only source)

Notes are evidence, not memory. `scripts/memfw_notes_mcp.py` registers three
read-only tools before starting stdio transport:

- `notes_search(query, limit=5, source=qmd)` calls `memfw-fleet notes search`;
- `notes_related(path, limit)` uses `MEMFW_CLI` and `MEMFW_RELATED_DB`;
- `memory_related(memory_id, limit)` calls `MEMFW_READONLY_URL + /related` with
  the read token.

`memory_related` negotiates the contract version with `/health` before its first
use (five-minute cache per endpoint), asks for at most 50 results, and allows
the server one second to answer. It passes `mode`, `degraded` and each result's
`reasons` through; `reasons` is limited to the contract's vocabulary (`link`,
`related_field`, `shared_tag`, `shared_course`, `shared_entity`, `lexical`,
`semantic`, `section`), and a value outside it is dropped.

Both of its request lines are rebuilt from the one validated base
(`MEMFW_READONLY_URL` reduced to a root address) plus the fixed path of the call,
and both are sent through a private `urllib` opener that refuses to follow a
redirect: a 3xx is the stable failure `redirect_refused`, the `Location` target
is never requested, the read token is never forwarded to it, and a `/health`
that got one is not cached and is not followed by a `/related` request. See
"Redirects are never followed".

Missing Notes MCP settings produce a tool error naming the missing variable.
No note is written into memory and no proposal is created automatically.

## What this integration does not do

- It does not replace Conductor's PG proposal, approval or projection lifecycle.
- It does not write approved Conductor memories into MemFW authority; staging
  creates `_candidates/` entries only and still requires MemFW confirmation.
- It does not migrate the authoritative remote Fleet Memory into a local vault.
- It does not expose historical/`as_of` recall or permit cross-user source
  sharing.
- It does not turn notes into memories automatically.
- It does not forget or delete anything on the remote side: v2 has no such
  endpoint, and Conductor says so explicitly.

## Review history

The W1 work was reviewed in rounds, and every commit on `feat/memfw-client-v2`
was inside at least one reviewed scope. A delivery report once stated that "the
first 13 commits were never reviewed"; the review records do not say that, so the
record is corrected here:

| Round | Reviewed commit | What that round covered |
|---|---|---|
| Round 0 (first review) | `4808ed645` | Pinned to that commit only — 10 commits / 46 files from baseline `97857b7b5`. The branch had already advanced to `736a603ba`, which that round named as unreviewed at the time. |
| Fix round 1 | `749646a5f` | The full baseline scope (20 commits / 69 files), all 10 commits of `4808ed645..749646a5f`, and a re-review of the earlier B1–B5 findings. |
| Fix round 2 | `87a366b46` | 69 files, all within the W1 PRD scope; `4808ed645..HEAD` was 11 commits by then. |
| Replan round 0 (new cycle) | `f4ceb0c0a` | Baseline to HEAD: 22 commits / 69 files. |
| New-cycle fix round 1 | `93ffa6080` | Baseline to HEAD: 23 commits / 70 files; the whole approved range was re-checked, and the `f4ceb0c0a` report counted as review history only. |
| New-cycle fix round 2 | `45de7ba38` | Approved baseline to HEAD: 24 commits / 70 files; this round 1 commit / 10 files. |

The thirteen commits `4808ed645..93ffa6080` — `736a603ba`, `a524e9a87`,
`1bd9a7dd4`, `2409fab07`, `749051742`, `bedd75226`, `098401a71`, `621bc8713`,
`d478f8c1d`, `749646a5f`, `87a366b46`, `f4ceb0c0a` and `93ffa6080` — were
therefore each reviewed, and every later round re-verified the whole approved
baseline-to-HEAD range rather than only its own commit. The new-cycle fix round 2
review (`45de7ba38`) recorded the "first 13 commits never reviewed" sentence as a
non-blocking historical inaccuracy in the delivery report, not as a blocker.

## Verification

All HTTP tests are mocked; the test suite does not contact NAS or MemFW:

```bash
PYTHONPATH=$PWD:$PWD/src:$PWD/libs/ptc-cli LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 \
  /path/to/venv/bin/python -m pytest -q \
  tests/unit/server/services/refactor \
  tests/unit/scripts/test_memfw_notes_mcp.py tests/unit/config
```

The shared contract samples live in `tests/fixtures/memfw_contract_v2/` and are
copied byte for byte from the MemFW repository, which owns them.
