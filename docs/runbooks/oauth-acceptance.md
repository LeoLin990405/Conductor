# Runbook — verifying and accepting subscription connections

This runbook is written so that someone who is neither the author nor the
requester can reproduce the acceptance. Everything here is run against a
deployment that includes the branch under review.

## 0. Preconditions

- App reachable the same way production is (`http://localhost:18100` through the
  documented tunnel). BigModel sign-in only works from a loopback origin.
- Backend, Postgres, Redis healthy; `GET /api/v1/system/readiness` reachable.
- A test account per provider the operator actually wants to accept. Do not put
  credentials in shell history, screenshots, or this document.
- Model calls made during acceptance are real and spend provider quota; keep
  them minimal (one short turn per accepted model).

## 1. Automated gates (must be green before any live step)

Backend (repo root):

```bash
UV_CACHE_DIR=<cache> uv run --frozen pytest \
  tests/unit/server/services/test_mimo_oauth.py \
  tests/unit/server/services/test_oauth_catalog.py \
  tests/unit/server/services/test_kimi_oauth.py \
  tests/unit/server/services/test_zai_oauth.py \
  tests/unit/llms/test_oauth_anthropic_identity.py -q
UV_CACHE_DIR=<cache> uv run --frozen ruff check <changed backend files>
```

Frontend (`web/`):

```bash
npm_config_manage_package_manager_versions=false pnpm test
npm_config_manage_package_manager_versions=false pnpm typecheck
npm_config_manage_package_manager_versions=false pnpm lint
npm_config_manage_package_manager_versions=false pnpm build
```

The suite must include the catalog cross-check (every login descriptor's
`api_key_provider` and `models` exist in the manifest) and the MiMo security
tests (tampered payload, replayed state, expiry, no secret in logs/responses).

## 2. Endpoint-level evidence (no user credentials)

Run against the deployment; redact `device_code` / `code` / token fields before
pasting anywhere.

```bash
# Capability catalog (authenticated in the app; same-origin)
curl -s http://localhost:18100/api/v1/oauth/providers | jq '.providers[] | {id,kind,login_supported,api_key_provider}'

# Kimi device authorization (public client; returns a user_code)
curl -s -X POST http://localhost:18100/api/v1/oauth/kimi/device/initiate | jq '{user_code,verification_url}'

# Z.ai CLI authorization init (server-mediated; returns authorize_url)
curl -s -X POST -H 'content-type: application/json' \
  -d '{"provider":"zai"}' http://localhost:18100/api/v1/oauth/zhipu/initiate | jq '{authorize_url}'

# MiMo initiate (returns an authorize URL; no credential in it)
curl -s -X POST http://localhost:18100/api/v1/oauth/mimo/initiate | jq '{authorize_url}'
```

Expected: 200 with the documented shapes; 503 only when Redis is unavailable.
None of these responses may contain `sk-`, `access_token`, `refresh_token`, or a
private key.

## 3. Per-provider live acceptance (user-interactive)

For each provider, record: login start, authorize page, result screen,
`GET /api/v1/users/me/api-keys` `has_key` for the provider id, a minimal model
turn, and disconnect. The same steps apply to the four existing international
logins; the domestic rows below are the ones added/changed here.

| Provider | Start | Expected stored credential | Minimal proof the login is usable |
| --- | --- | --- | --- |
| Kimi (`kimi-oauth`) | Settings → 国内模型连接 → Kimi → 登录 → open verification URL → enter code | OAuth row for `kimi-oauth`; models `kimi-k3-oauth` | One short chat turn on `kimi-k3-oauth`; Kimi console shows the plan usage |
| 智谱 (`z-ai-cn-oauth`) | 智谱 → 登录 → authorize on `bigmodel.cn` → returns to `/oauth/zhipu` | OAuth row for `z-ai-cn-oauth` carrying the minted key; models `glm-5.3-cn-oauth` | One short turn on `glm-5.3-cn-oauth`; BigModel console shows the key/usage |
| Z.ai (`z-ai-oauth`) | Z.ai → 登录 → authorize page | OAuth row for `z-ai-oauth`; models `glm-5.3-oauth` | One short turn on `glm-5.3-oauth`; Z.ai console usage |
| MiMo (`mimo-oauth`) | MiMo → 登录 → authorize page → paste the displayed code | `user_api_keys` row for `mimo-oauth` with minted key + base URL; models `mimo-v2.6-flash-oauth`, `mimo-v2.6-pro-oauth` | One short turn on `mimo-v2.6-flash-oauth`; MiMo Token Plan page shows usage |
| Qwen/百炼, MiniMax, StepFun, Doubao/ARK, DeepSeek | 填写 API Key → paste a key (or reuse the configured one) | `user_api_keys` row for the provider | One short turn on that provider's model; API-key path only (catalog explains why) |

After each: disconnect and assert `has_key` becomes false and the model picker no
longer treats it as connected. Re-connect with the API-key path where the catalog
offers it.

## 4. Negative / security checks

- Replay a one-time callback (POST the same MiMo code twice, or reuse a device
  poll after success): the second call must fail with a sanitized 4xx, and no
  credential may be written.
- Tamper with one byte of the MiMo pasted code: must fail closed, no credential
  written.
- Expire a pending flow (wait past its TTL): initiate returns a flow, callback
  after TTL fails.
- BigModel initiate with a non-loopback `redirect_uri`: must be rejected.
- Grep the backend log and the browser network panel for token-shaped strings
  after a login: only redacted material may appear.
- Disconnect while the model is the current default: the app must fall back to
  the deployment default instead of failing the next turn.

## 5. Time and persistence checks

- Complete a login, restart the backend (or wait for a worker rotation), reload
  the app: the connection must still show connected and a model turn must work.
- Let an OAuth access token approach expiry (or force it in a staging database):
  the next turn must refresh transparently; an expired/revoked token must read as
  disconnected, not as connected-but-failing.

## 6. Evidence to attach to the PR

For every row: command or click path, expected vs actual, timestamp, and a
redacted artifact path (terminal output, screenshot, or log excerpt). Do not
attach anything containing a credential, code, or private key.

## 7. Rollback

Stop the deployment, restore the previous revision/images (deployment README's
update procedure), verify `health` + `readiness`, and confirm stored
subscriptions are either disconnected or still decryptable with the unchanged
`BYOK_ENCRYPTION_KEY`. No migration is required for this change.

## 8. Not claimed until step 3 is completed by the operator

- “Live LLM/Agent acceptance passed” — the author's unit tests and endpoint probes
  do not prove a provider authorizes and serves a real turn.
- Per-model availability in regions/plans not exercised.
- Any statement about provider quota or billing beyond what the provider console
  shows for the test account.
