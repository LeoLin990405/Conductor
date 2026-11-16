# Differential harness (Phase 1 skeleton)

Compares two behavior fixtures from the deep refactor: the old path and the new
path, on the same normalized inputs. Design: [`docs/refactor/0006-differential-harness.md`](../../docs/refactor/0006-differential-harness.md).

**Status:** schema, normalization, redaction gate, comparison policy, CLI and
**HTTP capture** are implemented and unit-tested. A real F1 capture from the M2
app (`fixtures/F1/login-live-20261025.json`, `synthetic: false`,
`app_revision: 12d17232`) is committed as the live-capture baseline; the other
F1–F3 fixtures are synthetic baselines built from the documented wire contracts.
Playwright/SSE capture and live F2/F3 turns are Phase 3/4 work (the M2 stack
currently has no valid model credential, so a real turn cannot be captured
yet).

## Commands

```bash
# Capture live HTTP exchanges into a fixture (run on a host that reaches the app;
# operator HTTP(S)_PROXY is bypassed so loopback stays loopback)
uv run python -m scripts.refactor_diff.cli capture --flow F1 --revision live-... \
  --app-revision <sha> --base-url http://localhost:8000 \
  --request GET:/api/v1/oauth/providers --out /tmp/login.json

# Schema + redaction gate over every committed fixture (fails on a token shape)
uv run python -m scripts.refactor_diff.cli validate scripts/refactor_diff/fixtures/*/*.json

# Compare two fixtures (Markdown report; exit 1 on untolerated differences)
uv run python -m scripts.refactor_diff.cli compare \
  --old scripts/refactor_diff/fixtures/F2/turn-baseline.json \
  --new scripts/refactor_diff/fixtures/F2/turn-candidate.json

# Same, with machine-readable evidence for a PR
uv run python -m scripts.refactor_diff.cli compare ... --markdown /tmp/diff.md --json /tmp/diff.json
```

Make targets:

```bash
make refactor-diff OLD=<baseline.json> NEW=<candidate.json>
make refactor-diff-validate
```

## Comparison policy

| Difference | Verdict |
| --- | --- |
| HTTP method/path/status, dict keys, list lengths, SSE event types, DB row counts | **fail** |
| token counts / latencies within 5% (or ±1) | tolerated, reported |
| free-text (`content`, `summary`, `message`, …) | flagged, tolerated for reviewer |
| everything else | **fail** |

UUIDs, ISO timestamps and capture metadata are normalized before comparison.
Each fixture carries `revision`, `app_revision`, `captured_at` and `synthetic`.

## Fixtures

`fixtures/F1/login-live-20261025.json` — real HTTP capture from the M2 app
(providers catalog, Kimi status, API-key presence, readiness). Host that reaches
the app captures directly; no browser needed for HTTP flows.
`fixtures/F1/login-{baseline,candidate}.json` — connection status/API-key shape.
`fixtures/F2/turn-{baseline,candidate}.json` — a turn with a tool call; the
candidate changes UUIDs, a latency/token count inside tolerance and one text
delta (the expected PASS-with-flags case).
`fixtures/F3/subagent-{baseline,candidate}.json` — subagent streams and run rows;
identical behavior, different UUIDs.

## What a capture must add (Phase 3/4)

1. Capture HTTP + SSE through Playwright; store DB snapshots read with normalized
   rows; redact at capture time (this tool validates afterwards).
2. Mark real captures `synthetic: false` and keep them under the same schema.
3. Run old/new against the same test user and commit only the accepted baseline.
