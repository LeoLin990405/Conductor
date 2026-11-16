# sa-isolation: review hardening for workspace worker isolation

Status: complete (2026-11-03). Branch `feat/sa-isolation` (this worktree, base `6cff25f03` live).

## What this round is

The branch carries eight commits isolating local autonomous native Workers (`7ffc9b54e..HEAD`, documented in [`worker-isolation.md`](worker-isolation.md)). Two earlier hardening rounds fixed BLOCK reviews (log: `/Users/you/conductor-wt/logs/grok--sa-isolation--rereview-sa-isolation.log`). This round fixes the **sixth review (rr6)** of the branch, which returned BLOCK with 1×P1 + 2×P2 (log: `/Users/you/conductor-wt/logs/codexrev--sa-isolation--rr6-sa-isolation.log`; earlier rounds rr3–rr5 are recorded in this file's history). Nothing was ported from legacy sources in any round; the legacy survey (an internal survey of the legacy projects (not published)) remains architectural context only.

## Findings fixed in this round

| Finding | File | Fix |
|---|---|---|
| P1: the macOS Seatbelt profile granted `(allow network*)`, which also covers Unix-domain sockets. Seatbelt mediates Unix sockets only through network rules, so a worker could connect to same-user host services (ssh-agent, the Docker socket, credential agents) with no network hop — the reviewer reproduced a sandboxed worker reading test data from a sibling directory's socket while direct file access was denied | `src/llms/native_cli/isolation.py` | `_mac_profile()` now denies `(network-outbound (to unix-socket))` and re-allows only connects to the three sandbox mounts (workspace, private home, private tmp) plus `/private/var/run/mDNSResponder`, which every macOS DNS lookup dials; TCP/UDP stay on the broad grant so provider networking is unchanged. Verified empirically with `sandbox-exec`: sibling socket → EPERM, in-mount socket → reachable, loopback TCP → reachable, mDNS connect → reachable. Also added narrow device rules (`/dev/null` write, `/dev/null`/`/dev/zero`/`/dev/urandom`/`/dev/random` read) — `>/dev/null` redirected failed EPERM under the old profile too, which a shell probe surfaces immediately. Known Seatbelt limitation, documented in the profile: any unix-socket deny also breaks `connect()` to socket files the sandbox itself created, so a worker's internal IPC must use a pre-existing in-mount socket path or TCP loopback (fails closed, never open) |
| P2: the Seatbelt profile emitted paths through `json.dumps` with `ensure_ascii=True`, producing `\u00e9`-style escapes Seatbelt does not decode — a workspace named "café" lost all file access (`Operation not permitted` reading its own files). Separately, `_path_contains()`/`_path_eq()` compared raw strings, so the NFD spelling of a directory was not recognized as the NFC ancestor: an isolation-state root nested under the Unicode-spelled workspace was accepted as disjoint and one worker read another worker's state | `src/llms/native_cli/isolation.py` | `literal()` now emits UTF-8 (`ensure_ascii=False`; NFC and NFD literals both match on APFS). Containment gained NFC normalization (`_normalized_spelling()`) applied before every string comparison, alongside the existing inode-identity and case-fold tiers; on volumes that truly distinguish spellings (Linux ext4) the merge only denies a launch, the fail-closed direction |
| P2: the Linux `bwrap` namespace mounted only system library dirs — no resolver files (`/etc/resolv.conf`, `/etc/hosts`, `/etc/nsswitch.conf`, `/etc/host.conf`) and no TLS trust store (`/etc/ssl/certs`, `/etc/ca-certificates`), so provider HTTPS clients inside the worker had no DNS and no certificates | `src/llms/native_cli/isolation.py` | `prepare_process()` now read-only-binds those files when present (new `_LINUX_NETWORK_CONFIG` + `_network_config_bind()`); symlinked files (the systemd-resolved `resolv.conf` case) bind their resolved target at the canonical `/etc` path, which works because bwrap opens sources in the outer namespace |

## Files changed

- `src/llms/native_cli/isolation.py` — `_mac_profile()`: UTF-8 path literals, unix-socket deny + scoped re-allows + mDNS exception, device rules; `_path_eq()`/`_path_contains()`: NFC normalization via `_normalized_spelling()`; Linux branch: `_LINUX_NETWORK_CONFIG`/`_network_config_bind()` ro-binds.
- `tests/unit/llms/test_native_cli_isolation.py` — 8 new regression tests: profile unix-socket deny shape + ordering + device rules, UTF-8 profile literals, NFC/NFD `_paths_overlap`, Unicode-variant state root rejection, bwrap resolver/TLS mounts, symlink bind mapping, and two real-`sandbox-exec` probes (host unix socket unreachable while in-mount socket + TCP loopback + `/dev/null` work; Unicode workspace file read).

No DB migration, no web/desktop change.

## Round rr3 (history)

### Findings fixed in rr3

| Finding | File | Fix |
|---|---|---|
| P1: `_case_insensitive()` probed only the nearest existing ancestor. A name unchanged by `swapcase()` (e.g. all digits, `123`) was taken as proof of a case-sensitive volume, so on native APFS `PROJECT/123` and `project/123/456` were accepted as disjoint workspace roots, and a case-variant isolation-state root nested under the numeric workspace directory was also accepted — exposing supposedly separate workspace/state files | `src/llms/native_cli/isolation.py` | The probe now keeps walking up to the first ancestor whose name actually differs under case swap; only a definitive "case-variant spelling absent" (`FileNotFoundError`) reports case-sensitive, and reaching the volume root without classifying fails closed (case-insensitive, which only denies the launch) |
| P2: the macOS Seatbelt profile granted the private `TMPDIR` write but not read — a real `sandbox-exec` probe wrote a temp file and could not read it back | `src/llms/native_cli/isolation.py` | The `file-read*` rule now covers the private temp subpath alongside workspace and home; the `sandbox-exec` child-probe test reads the temp file back |
| P2: `redact_diagnostic()` redacted every allowlisted env value, so ordinary configuration such as `NATIVE_CLI_DSH_ACCOUNT=1` replaced every `1` in stdout/stderr — including numeric JSON usage fields — before the adapter parsed it, turning valid provider JSON invalid | `src/llms/native_cli/isolation.py` | Redaction is now name-classified: only values whose env name declares a credential (`*_API_KEY`, `*_TOKEN`, `*_SECRET`, `*_PASSWORD`, `*_AUTH*`, … by `_CREDENTIAL_NAME_PARTS` component match) are scrubbed; ordinary configuration values pass through untouched. Credential-named values still redact (existing short-secret test stays green) |
| P2: a concurrent input delivery bumps the ticket `revision`, so the CAS-guarded `fail_unisolated_cloud_worker()` missed; the handler logged and returned normally, the effect runner acknowledged the launch effect, and the deduplicated launch key never fired again — the ticket sat queued with no retry | `src/server/services/autonomy/worker.py` | `launch_worker_effect()` now re-reads the ticket and retries the guarded rejection (up to 3 fresh reads) while the ticket remains an eligible cloud launch, and stops quietly once a re-read shows it no longer eligible (e.g. a daemon claimed it). If every read is still contested it raises, so the runner nacks the effect back to pending and it retries with backoff instead of being acknowledged unsettled |

### Files changed in rr3

- `src/llms/native_cli/isolation.py` — `_case_insensitive()` walks case-invariant ancestor names; `_mac_profile()` grants temp-dir read; `redact_diagnostic()` redacts only credential-classified names via new `_is_credential_name()` / `_CREDENTIAL_NAME_PARTS`.
- `src/server/services/autonomy/worker.py` — `launch_worker_effect()` re-read/retry loop with `_MAX_REJECTION_READS`; shared `_CLOUD_WORKER_REJECTION_ERROR`.
- `tests/unit/llms/test_native_cli_isolation.py` — 5 new regression tests (numeric-ancestor probe, numeric case-variant policy roots, numeric case-variant state root, macOS profile temp read rule, JSON-preserving redaction + credential redaction) and a temp-readback step in the `sandbox-exec` probe.
- `tests/unit/server/services/autonomy/test_worker_launch_context.py` — 3 new tests (retry on raced revision, stop when no longer eligible, raise for nack when still contested).

## Tests run (rr6)

Focused suite via `../../Documents/deepseek-harness/default-workspace/AlphaCN/.venv/bin/python -m pytest` (repo has no local `.venv`):

```sh
env -i PATH=/opt/homebrew/bin:/usr/bin:/bin PYTHON_DOTENV_DISABLED=1 \
  HOME=…/.test-home TMPDIR=…/.test-tmp .venv…/python -m pytest \
  tests/unit/llms tests/unit/scripts/test_conductor_local_daemon.py \
  tests/unit/server/services/autonomy tests/unit/server/services/native_runtime \
  tests/unit/server/database/test_autonomy*.py tests/unit/server/test_autonomy_api.py \
  tests/unit/server/test_autonomy_daemon_followup.py \
  tests/unit/server/test_autonomy_ticket_replies.py tests/unit/server/test_import_layering.py \
  --basetemp=…/.test-tmp/root-rr6-full -q --tb=short
```

- Touched module first: `tests/unit/llms/` — **1,371 passed, 20 skipped, 0 failed** (+8 new isolation regression tests).
- Full focused suite: **1,953 passed, 20 skipped, 0 failed** (previous round: 1,945 passed, 20 skipped; +8 new tests).
- Revert/vacuity check: with `_mac_profile()` restored to the old `(allow network*)`-only shape, the new host-socket probe's sibling socket returns data (leak reproduced) and the test fails; with the fix, all pass. The Unicode workspace test fails with `Operation not permitted` when the profile literals are ASCII-escaped, matching the reviewer probe.
- Ruff check clean on both changed files (pre-existing `ruff format` drift on untouched lines left as-is); `git diff --check` clean.

No live provider, GitHub, database, or running-backend calls were made; all tests use fakes/mocks and synthetic filesystems, plus real `sandbox-exec` probes confined to loopback TCP and socket files under `.test-tmp` (no external network).

### Fix verification (this pass)

Re-verified the committed fixes (`7ffeaa55e`) on 2026-11-03 with the repo's own env:

```sh
uv run pytest tests/unit/llms tests/unit/scripts/test_conductor_local_daemon.py \
  tests/unit/server/services/autonomy tests/unit/server/services/native_runtime \
  tests/unit/server/database/test_autonomy_ticket_launch_guard.py \
  tests/unit/server/test_autonomy_api.py tests/unit/server/test_autonomy_daemon_followup.py \
  tests/unit/server/test_autonomy_ticket_replies.py tests/unit/server/test_import_layering.py -q
```

- **1,922 passed, 20 skipped, 0 failed**; `tests/unit/llms/test_native_cli_isolation.py` alone: 39 passed. Ruff clean on both changed files (ruff 0.15.14, resolved from the repo spec via `uv run --with ruff`); `git diff --check` clean.
- Reviewer repros re-run live against the fix: sibling unix socket → connect refused (listener never accepted); sibling file → `READ_DENIED`; in-mount socket, loopback TCP, and `/dev/null` → work; Unicode (`café`) workspace reads its own file; NFC workspace + NFD-spelled nested state root → rejected as overlapping; mDNSResponder socket reachable, other host socket (`usbmuxd`) denied.
- `bwrap` is unavailable on this macOS host, so the Linux DNS/TLS mount fix is verified by the argv-construction regression test (`test_bwrap_mounts_resolver_and_tls_config`).

## Tests run (rr3, history)

- Touched files first: **47 passed** (`test_native_cli_isolation.py` + `test_worker_launch_context.py`).
- Full focused suite: **1,945 passed, 20 skipped, 0 failed** (previous round: 1,936 passed, 20 skipped; +9 new tests).
- Revert check: with only `isolation.py` and `worker.py` reverted to the previous commit, the 9 new/modified regression tests fail (9 failed, 38 passed); with the fixes, all pass.
- Ruff clean on all four changed files; `git diff --check` clean.

No live provider, GitHub, database, or running-backend calls were made; all tests use fakes/mocks and synthetic filesystems (plus the pre-existing opt-in macOS `sandbox-exec` smoke on a synthetic workspace).
