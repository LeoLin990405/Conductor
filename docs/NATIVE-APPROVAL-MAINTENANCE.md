# Native approval maintenance

The HITL approval paths that are not covered by a stable public vendor API are
opt-in and need a smoke check after a vendor CLI upgrade.

## Switches

```dotenv
# Codex: app-server, always on for native Codex threads
# ACP providers: DSH, MiMo, MiniMax, Kimi, Grok
NATIVE_CLI_ACP=deepseek-dsh,mimo,minimax,kimi,grok

# ZCode: generated permission-broker copy
NATIVE_CLI_ZCODE_APPROVALS=1

# StepFun: RPC extension-UI confirm
NATIVE_CLI_STEPFUN_APPROVALS=1
```

Without these flags the providers keep their non-approval transport and the
capability matrix continues to say `approvals=false`.

## Protocol anchors

| Provider | Surface | Approval anchor |
|---|---|---|
| Codex | `codex app-server --stdio` | `item/commandExecution/requestApproval` |
| DSH/MiMo/MiniMax/Kimi/Grok | ACP | `session/request_permission` |
| Grok | `grok agent stdio` | ACP + isolated home with `permission_mode = "default"` |
| StepFun | `step --mode rpc` | `extension_ui_request` method `confirm` |
| ZCode | `zcode --output-format stream-json` | `permission.requested` from patched `DenyPermissionBroker` |

## ZCode patched-copy maintenance

`ensure_approval_binary()` is hash-keyed by the vendor CLI bytes and cached at:

```text
~/.cache/conductor/native-cli/zcode/zcode-alpha-approval-*.cjs
```

It fails closed if the minified `DenyPermissionBroker` class is no longer
found. The vendor installation is never modified.

After a ZCode upgrade:

```bash
uv run pytest tests/unit/server/services/native_runtime/test_zcode_stream.py -q
```

The tests include a source-change/cache-regeneration case. If the class layout
changed, update `_DENY_CLASS`/`_BRIDGE_CLASS` in
`src/server/services/native_runtime/zcode_approval.py` after re-running the REA
probe; do not point the runtime at an unpatched copy.

## Grok isolated home

`ensure_grok_approval_home()` copies only `auth.json` into a private cache and
writes:

```toml
permission_mode = "default"
```

The user's `~/.grok/config.toml` is not changed. If a Grok upgrade changes the
config key or ACP permission handshake, the runtime will block rather than
auto-approve.

## StepFun RPC

The native runtime sends:

```json
{"type":"extension_ui_response","id":"<request-id>","confirmed":true}
```

If an upgrade changes `extension_ui_request`/`confirm`, update
`src/server/services/native_runtime/stepfun_rpc.py` and rerun the live smoke.

## Regression checks

```bash
uv run pytest \
  tests/unit/server/services/native_runtime/test_acp_runtime.py \
  tests/unit/server/services/native_runtime/test_zcode_stream.py \
  tests/unit/server/services/native_runtime/test_stepfun_stream.py \
  tests/unit/server/services/native_runtime/test_grok_stepfun.py \
  -q
```

Live smoke (backend on host, provider logged in): create a native thread for
each enabled provider, ask it to write a file outside the workspace, expect an
`interrupt` with `kind=tool_approval`, approve, and verify the file plus
`run_end completed`.

## Security boundary

- The paths only run when explicitly enabled.
- Reject/timeout falls back to deny.
- Grok auth is copied into a private cache; no token is written to logs.
- ZCode uses a generated copy under the user cache; the installed app is
  untouched.


## Live browser picker smoke

Against the real frontend/backend (not the mocked Playwright suite):

```bash
cd web
CONDUCTOR_WORKSPACE_ID=<workspace-uuid> node scripts/live-native-smoke.mjs
```

It opens the real `/chat/<workspace>` page, reads the live
`GET /api/v1/native-agents` inventory through the UI, verifies all eight native
providers are offered and Claude/Gemini/Qwen are not, and fails on page errors.


## Live native-turn smoke

Against the real frontend/backend (not mocked):

```bash
cd web
CONDUCTOR_WORKSPACE_ID=<workspace-uuid> PROVIDERS=grok,kimi,stepfun \
  node scripts/live-native-turn-smoke.mjs
```

Each provider is selected in the real composer, sent a no-tool prompt, and the
assistant's exact reply is awaited on the real thread page.

Run on 2026-10-29, all eight live native providers completed:

```text
codex        browser-codex-ok
deepseek-dsh browser-deepseek-dsh-ok
grok         browser-grok-ok
kimi         browser-kimi-ok
mimo         browser-mimo-ok
minimax      browser-minimax-ok
stepfun      browser-stepfun-ok
zcode        browser-zcode-ok
```

Zero page errors. The mocked Playwright suite remains at 125 passed / 33
pre-existing focus-ring/dashboard failures, which are separate from native
provider coverage.
