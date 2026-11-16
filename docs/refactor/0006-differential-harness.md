# 0006 — Differential harness: proving old and new behavior agree

Purpose: the refactor is only safe if the old and new implementations can be
compared on the same inputs. This document defines the capture, replay and
comparison design. The harness is built incrementally; Phase 1 starts with the
login/connection and normal-turn flows, Phase 4 requires the full matrix.

## Scope: flows under comparison

| id | Flow | What is compared |
| --- | --- | --- |
| F1 | Model connection: OAuth login (device/paste/redirect/mint) + API-key connect/status/disconnect | HTTP requests/responses (redacted), credential presence, status payloads, UI state |
| F2 | New thread turn with tool calls | request payload, SSE event sequence/types/payload shape, tool call/result ids, DB rows |
| F3 | Subagent delegation + merge | delegation request, subagent run rows/status transitions, merge/result delivery, UI lane state |
| F4 | Workspace file edit + artifact view | file tree, blob hashes, viewer payload, provenance rows |
| F5 | Memory baseline + injection | baseline index rendering, injected entries count/tokens (Phase 1+), read behavior |
| F6 | Compaction + resume | summary/checkpoint ids, reconstructed context, SSE reconnect replay, post-resume turn |
| F7 | Review apply/rollback (Phase 3) | diff payload, accept/reject state, file hash before/after |
| F8 | Narrow-screen/desktop shell | UI layout at 390px and 1440px, URL classification behavior |

## Capture (recording)

- **Runtime**: M2 app stack (backend 8000, frontend 5173) against a dedicated test
  user, plus the Homelab deployment for a read-only smoke capture.
- **Network**: HTTP + SSE recorded through Playwright (`page.route`) or the
  frontend fetch layer; store as HAR-like JSON:
  `{method, url, status, request_headers(redacted), request_body(redacted), response_body(redacted), sse_events[]}`.
- **Redaction rules (mandatory at capture time)**: bearer/cookie/api-key/oauth
  tokens → `[REDACTED]`; provider keys of the form `sk-*`/`sk-cp-*`/`sk-api-*`
  → `[REDACTED]`; user ids/thread ids/uuids normalized to stable tokens
  (`<thread:1>`, `<user:1>`); timestamps normalized to `T0+<ms>`; memory content
  replaced by content hashes unless the fixture already uses synthetic data.
- **DB snapshot**: for the thread(s) under test, selected tables
  (`conversation_threads`, `conversation_responses`, `subagent_tasks`,
  `subagent_runs`, `turn_slices`, `thread_transcripts`, `provenance_records`,
  new `agent_*`/`memory_entries` when enabled) exported as normalized JSON
  (ordered by a stable key; volatile columns replaced by `<volatile>`).
- **Checkpoint snapshot**: LangGraph checkpoint id chain + the rendered
  messages/context hash (not the raw checkpoint bytes, which contain env noise).
- **UI snapshot**: Playwright screenshot + accessibility snapshot for the two
  viewports; screenshots are only golden-compared for stable surfaces (layout
  shell, panel frames), not for streaming text.
- **Fixture location**: `tests/e2e/refactor/fixtures/<flow>/<revision>/`.
  Revisions are timestamped directories; only the accepted baseline is committed.

## Replay and comparison

- **Backend replay**: `respx` mocks upstream provider/MCP HTTP; Redis seeded with
  the recorded coordination state; Postgres seeded/migrated to the recorded
  schema; LangGraph checkpointer restored from a fixture checkpoint where
  practical, otherwise the flow is re-run from a synthetic seed.
- **SSE comparison**: events ordered by sequence; compare `type` and a normalized
  payload projection. Tolerated diffs: heartbeat/timing events, ids, token counts
  within ±5%, free-text deltas (flagged, not failed).
- **DB comparison**: row keys and normalized values must match for the listed
  tables; new nullable columns are ignored when the flag is off.
- **UI comparison**: screenshot diff with a pixel threshold for stable frames;
  DOM/a11y structure compared for panel/session/timeline contracts.
- **Flags**: old and new paths are selected per run:
  `REFACTOR_GOALS_ENABLED`, `REFACTOR_EVENTS_ENABLED`,
  `REFACTOR_MEMORY_INDEX_ENABLED`, `REFACTOR_TASK_TREE_ENABLED`,
  `REFACTOR_SUBAGENT_CONTRACT_ENABLED` (backend) and
  `VITE_REFACTOR_SHELL` (frontend). Default off; the harness runs both sides.
- **Triage**: every difference is classified as `protocol`, `data`, `ui`,
  `timing` or `intentional`; intentional changes require an entry in the PR
  evidence table and a compatibility note in the charter.

## Gates

| Phase | Required flows |
| --- | --- |
| Phase 1 | F1, F2, F3 (contract/event/goal slides only), F5 baseline rendering |
| Phase 2 | F1–F6, memory eval integration |
| Phase 3 | F2–F5, F7, F8 |
| Phase 4 | F1–F8 on M2 app + Homelab smoke; rollback drill recorded |

A slice is not "done" until its flows are recorded, replayed against both paths
and the diff report is attached. The full suite must run headlessly
(`make refactor-diff`, target added in Phase 1) so a third party can reproduce it.

## Security & privacy

- Capture only synthetic/test-user data; never record production credentials or
  real user memory/transcripts.
- Redaction is applied before writing fixtures; a fixture containing a token
  shape fails a validator and the commit is rejected.
- The harness never contacts real providers in CI; live-provider checks are a
  separate, explicitly approved acceptance step (see runbook style in
  `docs/runbooks/`).
- Screenshots of fixture data only; no chat content from real accounts.

## Phase 1 skeleton (implemented)

The pure-comparison half is in `scripts/refactor_diff/` and runs without a
database, a provider or Playwright:

| Piece | File | What it pins |
| --- | --- | --- |
| Fixture schema + loader/validator | `schema.py` | flow set (F1–F8), HTTP/SSE/DB shapes, row identity keys, `synthetic` flag |
| Normalization | `normalize.py` | UUID/ISO-timestamp → markers, capture metadata cleared; redaction backstop (secret fields must be `[REDACTED]`, key/bearer shapes rejected) |
| Comparison policy | `compare.py` | protocol exactness; 5% (or ±1) tolerance for token/latency numerics; free-text flagged and tolerated; Markdown + JSON reports |
| CLI | `cli.py` | `validate` (schema+redaction gate) and `compare` (exit 1 on untolerated diff); `make refactor-diff` / `make refactor-diff-validate` |
| Baselines F1–F3 | `fixtures/F{1,2,3}/` | synthetic fixtures; F2 is the PASS-with-flags case (UUID renumbering, in-tolerance latency/tokens, text deltas) |
| HTTP capture | `capture.py` + `cli capture` | executes a live capture plan, redacts at capture time, validates the result; committed live F1 capture from the M2 app (2026-10-25, `app_revision 12d17232`, `synthetic: false`). Operator proxy is bypassed so a loopback capture stays loopback. |

HTTP capture is wired (`capture.py`); the live F1 fixture is committed with
`synthetic: false` and passes the same schema+redaction gate. Playwright/SSE
capture and live F2/F3 turns remain Phase 3/4: the M2 stack has no valid model
credential today (backend logs show provider 401s), so a real turn cannot be
captured yet. Synthetic baselines stay alongside the live one until then;
nothing in the comparison policy changes.

Verification: `tests/unit/scripts/refactor_diff/` (33 tests) covers schema
errors, normalization idempotence, redaction rejection, protocol/numeric/text
policy, DB row-order insensitivity, report rendering and CLI exit codes; the
committed fixtures validate clean and F1–F3 compare PASS.

## Deliverables (Phase 1)

1. ~~Fixture schema~~ → `scripts/refactor_diff/schema.py` (done; layout lives here, not `tests/e2e/`, until capture lands).
2. ~~`make refactor-diff` runner~~ → done (compares committed fixtures; both-path *orchestration* against a live app is Phase 4).
3. ~~Redaction validator~~ → done (`validate` command + unit tests); a dedicated CI job is still open — the unit suite runs it in the default test job.
4. ~~Baseline fixtures for F1–F3 + diff report format~~ → done (synthetic; Markdown/JSON reports).
