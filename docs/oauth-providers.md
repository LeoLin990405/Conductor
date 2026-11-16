# Subscription connections: capability matrix and evidence

All six account-login flows share one frontend state machine
(`web/src/components/model/useSubscriptionLogin.ts`), one catalog
(`src/server/services/oauth_catalog.py`, served by
`GET /api/v1/oauth/providers`) and the existing encrypted credential stores.
API-key-only providers are listed with the same card skeleton and their
official website for obtaining a key.

## Matrix

| Provider | Login | Kind | API-key fallback | Claim edge |
| --- | --- | --- | --- | --- |
| ChatGPT Codex (`codex-oauth`) | ChatGPT subscription | device code | `openai` | existing |
| Claude (`claude-oauth`) | Claude subscription | paste-back code | `anthropic` | existing |
| xAI Grok (`grok-oauth`) | SuperGrok / X Premium | device code | `xai` | existing |
| Gemini (`gemini-oauth`) | Google account (Code Assist) | paste-back code | `gemini` | existing |
| Kimi Code (`kimi-oauth`) | Kimi 编程套餐 | device code | `moonshot-coding` | added in PR #4 |
| 智谱 BigModel (`z-ai-cn-oauth`) | BigModel account | browser redirect + key mint | `z-ai-cn-coding` | added in PR #4 |
| Z.ai (`z-ai-oauth`) | Z.ai account | server-mediated CLI authorization + key mint | `z-ai-coding` | added in PR #4 |
| Xiaomi MiMo (`mimo-oauth`) | MiMo 平台账号 | browser authorization, encrypted paste-back code, key mint | `mimo` | this change |
| Qwen / DashScope (`dashscope`) | **not available** | API key only | — | OAuth free tier discontinued |
| MiniMax (`minimax`) | **not available** | API key only | — | official CLI login is API-key based |
| StepFun (`stepfun`) | **not available** | API key only | — | Step Code/Step Plan are key-based |
| Doubao / ARK (`doubao-plan`) | **not available** | API key only | — | ARK uses API Key/AK-SK |
| DeepSeek (`deepseek`) | **not available** | API key only | — | API key only |

`api_key_provider` is the provider id the "fill an API key" secondary action
opens; the provider catalogue and the model manifest are cross-checked in tests
(`tests/unit/server/services/test_oauth_catalog.py`).

## Evidence per protocol

### Kimi Code — device authorization grant

- Source: official `kimi-cli` KLIP-14, “Kimi Code OAuth /login”
  <https://raw.githubusercontent.com/MoonshotAI/kimi-cli/main/klips/klip-14-kimi-code-oauth-login.md>
  (public client id, `https://auth.kimi.com` device/token endpoints, required
  `X-Msh-*` client headers, refresh grant, bearer use on `api.kimi.com/coding/v1`).
- Local artifact used to cross-check: `kimi-code` 2.1.1
  (`/opt/homebrew/Cellar/kimi-code/2.1.1`).
- Live probe: `POST https://auth.kimi.com/api/oauth/device_authorization` with the
  documented public client returns `user_code` + `verification_uri_complete`
  (2026-10-24). No token material recorded.

### 智谱 BigModel / Z.ai — browser authorization + coding-plan key mint

- Source: CLIProxyAPI PR #3928 (“Z.AI / BigModel - ZCode (GLM) OAuth login”),
  <https://github.com/router-for-me/CLIProxyAPI/pull/3928>, verified upstream with a
  real BigModel Coding Plan account (8 GLM models over the Anthropic-compatible
  endpoint), plus the installed ZCode client bundle for the BigModel loopback
  redirect flow (`zcode://oauth/callback` in the bundled client; the reviewed
  implementation uses a loopback callback instead).
- Protocol: Z.ai uses `zcode.z.ai/api/v1/oauth/cli/init` + poll; BigModel uses
  `bigmodel.cn/login` with a loopback redirect and `zcode.z.ai/api/v1/oauth/token`
  exchange; both end with the business API key mint
  (`/api/biz/customer/getCustomerInfo` → org/project → `/api_keys` → copy secret).
- Live probe: `POST https://zcode.z.ai/api/v1/oauth/cli/init` returns an
  `authorize_url` + flow metadata for the Z.ai flow (2026-10-24).

### Xiaomi MiMo — browser authorization, encrypted paste-back, key mint

- Source: official MiMo Code CLI, package `mimo-code` 0.1.15
  (`/opt/homebrew/Cellar/mimo-code/0.1.15/libexec/lib/node_modules/@mimo-ai/cli/bin/.mimocode`,
  a compiled Bun bundle) and <https://mimo.mi.com/docs/en-US/tokenplan/integration/mimo-code>.
- Protocol recovered from the bundle (string offsets ~78,091,060 and the
  surrounding plugin code):
  - `MIMO_PLATFORM_URL` (default `https://platform.xiaomimimo.com`);
  - authorize URL `<platform>/authorize?pk=<X25519 public key>&redirect_uri=<platform>/authorize/code/callback&kn=mimocode&key_name=mimo-code-cli-key-<hex>`;
  - the platform returns an opaque `u` payload (the paste-back code);
  - decrypt: base64url → 32-byte ephemeral public key, 12-byte AES-GCM nonce,
    ciphertext + 16-byte tag; shared = X25519(private key, ephemeral public key);
    key = SHA-256(shared); AES-256-GCM decrypt → `{uid, url, sk}` where `sk` is a
    minted MiMo API key and `url` is its inference base URL.
- Live probe: `GET https://platform.xiaomimimo.com/authorize?...` returns the
  platform SPA (HTTP 200) for loopback and platform callback redirects; no login
  was completed in this record, so this is endpoint-level evidence only.
- Risk: public first-party client shape (`kn=mimocode`, `key_name` prefix). If the
  platform changes, the flow must remain disabled and the API-key route used.

### Qwen — OAuth not offered (evidence of discontinuation)

- Source: official `qwen-code` 0.25.1 bundle
  (`/opt/homebrew/Cellar/qwen-code/0.25.1/libexec/lib/node_modules/@qwen-code/qwen-code/chunks/`,
  the `qwen-oauth` auth error table): “Qwen OAuth free tier was discontinued on
  2026-04-15. Run /auth to switch to Coding Plan, OpenRouter, Fireworks AI, or
  another provider.”
- The device-code endpoint itself still answers (probe 2026-10-24), but the
  official client states the OAuth route no longer grants model access, so the
  capability catalog lists Qwen as API-key-only. DashScope/Token-Plan keys remain
  the supported route (the deployment already uses a token-plan base URL).
- Qwen Code OAuth protocol references (not implemented): QwenLM/qwen-code commit
  `782c536` and PR #225.

### MiniMax / StepFun / Doubao ARK / DeepSeek — API key only

- MiniMax: <https://platform.minimax.cn/docs/m-plan/minimax-cli> — “登录” is
  `mmx auth login --api-key sk-cp-…`; no account OAuth.
- StepFun: <https://platform.stepfun.com/docs/zh/guides/developer/cli> and
  Step Code / Step Plan docs — API keys / plan keys only.
- Doubao/Volcengine ARK: <https://docs.volcengine.com/docs/ark/ark-cli?lang=zh> —
  ARK CLI authenticates with AK/SK or API keys; no consumer OAuth.
- DeepSeek: <https://platform.deepseek.com/> — API keys only.
- These are documentation-level findings as of 2026-10-24; if a provider ships an
  account authorization surface later, add a catalog row + adapter, not a UI
  branch.

## Security invariants (tested)

- OAuth state/pending flows live in Redis with a bounded TTL and are consumed
  once; replay fails.
- The MiMo private X25519 key never leaves Redis and never appears in a response
  or log; the browser only sees the public key embedded in the authorize URL.
- Tokens/keys never appear in URLs produced by this app (the MiMo code is
  provider-issued ciphertext, not a credential), in responses, or in logs.
- `disconnect` deletes the stored credential and the frontend cache entry; an
  expired/revoked credential reads as disconnected, never as connected.
- BigModel redirects are restricted to loopback URLs on the server side.
