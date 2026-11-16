# CodexBar → Conductor bridge

[CodexBar](https://codexbar.app) is a macOS menu-bar app that keeps the quota
windows of Codex, Claude, GLM, Kimi, MiniMax, DeepSeek and other AI coding
plans visible. Conductor can show those windows in **Settings → 数据来源** so the
quota you plan around while coding is also visible next to the model/routing
configuration.

CodexBar's credentials, cookies and provider sessions belong to the host. The
Conductor backend runs in a Linux container and must never receive them. The
bridge keeps that boundary:

```
macOS host                                   Docker / Conductor backend
┌──────────────────────────────┐   HTTP     ┌─────────────────────────────┐
│ CodexBar widget snapshot     │  /usage    │ CODEXBAR_BRIDGE_URL=/usage  │
│ (or `codexbar usage --json`) │ ─────────▶ │ or CODEXBAR_DATA_FILE       │
│ scripts/codexbar_bridge.py   │  sanitized │ Settings → 数据来源          │
└──────────────────────────────┘            └─────────────────────────────┘
```

The bridge reads CodexBar's **widget snapshot** first
(`~/Library/Group Containers/*.steipete.codexbar/widget-snapshot.json`), which
the app rewrites on every refresh. That path is fast and needs no extra CLI
run. If the snapshot is older than 15 minutes, the bridge falls back to
`codexbar usage --format json`. That command walks every enabled provider and
can take a while on a cold cache, so the settings request may time out and the
section reports `bridge_unreachable` until the bridge has produced its first
snapshot; the normal path uses the widget snapshot and answers immediately.

## Start

Host (macOS):

```bash
# Reads the fresh widget snapshot; no CodexBar login is performed here.
python3 scripts/codexbar_bridge.py --serve --host 0.0.0.0 --port 8765
```

For a one-shot file (useful for scheduled jobs or tests):

```bash
python3 scripts/codexbar_bridge.py --output ~/.local/share/conductor/codexbar.json
```

Local `.env`:

```dotenv
CODEXBAR_BRIDGE_URL=http://host.docker.internal:8765/usage
# or, instead of the HTTP bridge:
# CODEXBAR_DATA_FILE=/path/to/codexbar.json
```

Restart the backend after changing `.env`:

```bash
docker compose up -d backend
```

Open **Settings → 数据来源 → CodexBar 订阅用量**. The page refreshes with the
normal refresh button; the backend caches the snapshot for 30 seconds and the
bridge caches it for 60 seconds.

The same snapshot also powers the composer: pick a model whose provider maps to
a CodexBar provider (GLM → zai, Kimi → kimi, DeepSeek → deepseek, MiniMax,
StepFun, Grok, Cursor, and so on) and a compact quota badge appears next to the
model picker, for example `GLM-5.3 5-hour 12%`. The tooltip carries every
window and reset time; models without a matching, connected CodexBar provider
(including Auto) show no badge. The mapping lives in `web/src/lib/codexbar.ts`.

## Data exposed

Only the fields the UI needs are forwarded:

- provider id, source (`widget-snapshot` or `codexbar-cli`);
- `status` — `ok` when CodexBar returned a usable reading for the provider,
  `unknown` when it could not be read;
- quota window label, `usedPercent`, `resetsAt`, `resetDescription`,
  `windowMinutes`;
- login method / plan label, optional balance text and up to four detail rows;
- a sanitized error summary for providers that are not connected.

Account email, account id, identity objects, cookies, tokens and other
credential-shaped keys are removed by **both** the bridge and the backend as
defence in depth. Strings are scrubbed for email addresses and account ids.
The bridge does not read Keychain or browser cookies; it only reads CodexBar's
own snapshot file or asks CodexBar's CLI for its already-resolved usage.

## Unreadable providers

CodexBar reports a provider it cannot fetch as an error entry instead of a
reading — for example `No available fetch strategy for claude.` — while every
other provider is still readable. The CLI **exits 1 as soon as one provider
fails**, but its JSON payload still carries the providers it did read, so the
bridge keeps the payload and never discards it over the exit code.

The bridge therefore emits one row per provider, each with an explicit
`status`:

- `ok` — CodexBar returned a reading; the row carries the quota windows.
- `unknown` — CodexBar could not read the provider; the row carries no windows
  and a sanitized reason, and the provider is also listed in the top-level
  `errors` array (the "not connected" count in the UI).

An unreadable provider therefore reports `unknown` instead of disappearing:
the quota and balance states of its neighbours survive, and a provider with no
reading is never mistaken for a provider with headroom. The same rule applies
to the widget-snapshot path, where a provider entry without any window is
reported as `unknown`.

## Findings: `No available fetch strategy for claude`

`No available fetch strategy for <provider>.` is **CodexBar's own message**, not
a Conductor error and not a broken install. It means CodexBar resolved the
provider's *usage source* to a mode that has no usable credential, so it had no
strategy left to fetch with. Read on this host (CodexBar 0.83.0,
`/opt/homebrew/bin/codexbar` → `/Applications/CodexBar.app/Contents/Helpers/CodexBarCLI`):

| Command | Result |
|---|---|
| `codexbar usage --provider claude` | exit 0 — Session 94% left, Weekly 97% left (source: Claude Code CLI) |
| `codexbar usage --provider claude --source cli` | exit 0 — the Claude Code CLI credentials are present |
| `codexbar usage --provider claude --source api` | exit 1 — `No available fetch strategy for claude.` |
| `codexbar usage --provider claude --source oauth` | exit 1 — `Claude Code credentials changed (Updated 34m ago). Click Refresh to re-grant CodexBar access.` |

The Claude OAuth grant on this host is stale: the app's own defaults record
`ClaudeOAuthLastCredentialFailure` at 2026-11-02 15:03 UTC, and `~/.codexbar/`
holds only `claude-oauth-cache.lock` with no cached credential beside it. The
message therefore appears whenever Claude's usage source is set to *API* (no
Anthropic key configured) or to *OAuth* (grant expired or revoked by Claude
Code's token rotation) — while *Auto*, *CLI* and *Web* keep working.

In text mode the same failure prints `Error: No available fetch strategy for
claude.` on stdout and exits 1; in `--format json` it is an error entry
(`{"provider":"claude","error":{"kind":"provider","message":"No available fetch
strategy for claude.","code":1}}`) and the exit code is still 1.

### User-side step

In the CodexBar app: **Settings → Providers → Claude → Usage source**, set it
to **Auto** (or **CLI**), and press **Refresh**. If OAuth is the source you
want, press **Refresh** next to it instead — that re-grants CodexBar access to
the rotated Claude Code credential.

Verify on the host:

```bash
codexbar usage --provider claude --format json --no-color; echo "exit=$?"
```

Exit `0` with a `usage` object means the provider is readable again. Until
then Conductor reports `claude` as `unknown`; every other provider keeps its
quota and balance state, and no credential is needed to make that happen.

## Auto-dispatch: quota-aware model selection

The backend also uses the same snapshot to decide **which model a dispatch
line gets**. `src/tools/auto_dispatch/headroom_codexbar.py` implements the
`HeadroomPort` protocol of `src/tools/auto_dispatch/selection.py`: it answers
how much of a model's quota is left, as a fraction in `[0, 1]`.

```python
from src.tools.auto_dispatch.headroom_codexbar import CodexBarHeadroom
from src.tools.auto_dispatch.selection import QuotaAwareModelSelector

selector = QuotaAwareModelSelector(headroom=CodexBarHeadroom())
```

The adapter reads **one local file** — the sanitized snapshot Conductor writes
— and nothing else: it never calls the `codexbar` CLI and never reaches the
network. It reads the provider id and the window percentages, and nothing else
in the snapshot: no account e-mail, no token, no balance is read, logged or
returned.

- **Provider map.** CodexBar's provider id per Worker: `kimi→kimi`, `glm→zai`,
  `grok→grok`, `cursor→cursor`, `stepfun→stepfun`, `deepseek→deepseek`,
  `minimax→minimax`, `doubao→doubao`, `codex→codex`, `claude→claude`.
  Doubao uses CodexBar's own provider id `doubao`. A snapshot that has no
  Doubao row still reads as full (`1.0`). CodexBar knows providers Conductor
  has no Worker for (`manus`, `mimo`); they are never asked about.
- **The reading.** `remaining(model)` = `1 − max(usedPercent) / 100` over that
  provider's windows. The smallest remaining window wins: a provider whose
  5-hour window is spent but whose weekly window is wide is still nearly out of
  quota.
- **No reading is not no quota.** A missing file, an unreadable file, a
  provider row that was observed more than 30 minutes ago or dated in the
  future, a provider with no window and an unknown model all answer `1.0` with
  one debug line. A selector that skipped a model because nobody had read its
  usage would silently drop workers off the roster. The debug line carries a
  stable reason code (`reason=unreadable`, `reason=no-fresh-window`,
  `reason=unusable-clock`) and never the configured path, so the expanded path
  — which contains the account's home directory — does not end up in the logs.
- **The row's own clock is the only clock.** Each provider row's `updated_at`
  says when that provider's quota was actually *seen*. The file's root
  `updatedAt` says when the snapshot was *written* and takes **no part** in
  the freshness decision: the producer fixes it when collection starts and then
  reads the providers one by one, so a root can already be older than the
  window while the row next to it is still fresh — judging the root would throw
  that fresh row away. A row is only read when its own observation is at most
  30 minutes old **and not in the future**: a row dated later than now is
  unknown data, not fresh data, so it is skipped like a malformed one. A row
  that does not say when it was seen is not a reading either, and a row whose
  timestamp cannot be parsed or normalised is skipped the same way.
- **How this differs from the native-agent routing.** Both readers judge the
  same file by the same clock — the row's own observation time — so they no
  longer disagree about *which* clock decides. They still differ twice. Their
  windows differ (routing 15 minutes, this adapter 30 minutes), so a row aged
  between the two is read by this adapter and not by the routing; align the two
  windows if they are meant to agree. And the routing allows 60 seconds of
  clock skew while this adapter allows none, so a row 1 to 60 seconds ahead is
  read by the routing and skipped here.
- **One read per refresh window.** The selector asks for the headroom of every
  eligible model, so the file is read at most once every 30 seconds and the
  reading is reused in between.

**The snapshot path.** The adapter defaults to
`~/.conductor/usage/codexbar-snapshot.json` — the same file the native-agent
routing reads, configured in `config.yaml` as
`native_runtime.usage_headroom.snapshot_path`. A path in that file may start
with `~`; it is expanded against the home directory of the machine that runs
it, so the tracked config carries no machine-specific path. To read a snapshot
from somewhere else, give an absolute path in `config.yaml`, or pass a path to
the constructor:

```python
CodexBarHeadroom("/run/secrets/codexbar-snapshot.json")
```

**Wired into live assignment.** When `DispatchService` is not handed a port,
`default_headroom()` in `services/dispatch.py` builds
`MostConstrainedHeadroom(CodexBarHeadroom(), SnapshotOrUnlimitedHeadroom())`:
this adapter, plus the native-runtime usage snapshot (the only reader that sees
a spent DeepSeek balance; it answers `1.0` when it is not configured). The more
exhausted reading wins. The same port goes to the planner's own DAG assignment
(`LLMPlanner(headroom=...)`, which also counts only non-exhausted developers as
places) and to `QuotaAwareModelSelector`, which reassigns a failed line, so a
plan and a reassignment read one quota. An injected port, including
`UnlimitedHeadroom`, is used as given.

**Aliases are one Worker.** `remaining(model)` folds the name through
`roles.canonical_provider`, the one alias table, so `glm`, `zcode`, `zhipu`,
`zai` and `zcode-cli` all reach the `zai` row (and `claudedev`, `dsh`,
`arkcli`, `moonshot`, `mcode`, `cursor-agent`, `step` reach theirs). The
native-runtime reader and router fold through the same table.

**Task size.** A planner line may carry `estimated_size` (`small`, `medium`,
`large`); a `risk_bearing` line counts as `large`. A large line ranks by the
usage-plan headroom tier first (high ≥ 0.50, medium ≥ 0.20, low ≥ 0.05), so
it is not seated on a nearly spent Worker while a healthier one is open; inside
a tier the least-assigned spread and then the remaining fraction decide. On a
live reassignment the model the line is moving off ranks last.

**How W3 gets the path and the window.** The operator settings are one call
away:

```python
from src.config.settings import get_native_runtime_usage_headroom_config

settings = get_native_runtime_usage_headroom_config()
headroom = CodexBarHeadroom(
    settings.snapshot_path,                 # None -> the default path
    max_age_seconds=settings.max_age_seconds,   # the config's own window
)
```

Two things to decide there rather than by accident. `max_bytes` in the config
is already the same 256 KiB this adapter allows, so that one cannot go wrong.
`max_age_seconds` is not: the config default is 900 (15 minutes, the
native-agent routing's window) while this adapter's own default is 1800
(30 minutes). Pick one on purpose — the adapter takes the window as a
constructor argument, so aligning the two readers needs no change here.

## Security notes

- The bridge has no authentication. Bind it to `127.0.0.1` when only the host
  needs it; use `0.0.0.0` only when Docker Desktop needs
  `host.docker.internal` access, and keep port 8765 off the public internet.
- Conductor never writes CodexBar credentials or usage back to its database.
- Platform/SaaS deployments do not have the host's CodexBar data; the section
  stays in its "not configured" state. Set `CODEXBAR_DATA_FILE` to a snapshot
  that a host-side job exports if a hosted deployment should show it.

## Troubleshooting

| Symptom | Check |
|---|---|
| `CodexBar 尚未接入` | `CODEXBAR_BRIDGE_URL` / `CODEXBAR_DATA_FILE` is not set in the backend env; restart after editing `.env` |
| `bridge_unreachable` | `curl http://host.docker.internal:8765/usage` from inside the backend container; make sure the bridge listens on `0.0.0.0` for Docker Desktop |
| Providers list is empty | CodexBar has no enabled providers or its widget snapshot is stale; open CodexBar once, then refresh |
| A provider shows "not connected" | That provider needs a login/API key in CodexBar; Conductor intentionally does not bypass it |
| A provider shows `No available fetch strategy` | CodexBar's usage source for that provider has no usable credential — see the findings section above; Conductor reports the provider as `unknown` |
| Numbers are older than expected | CodexBar writes the snapshot on its own refresh interval; open the menu bar popover and refresh |
