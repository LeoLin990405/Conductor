# ADR 0001 — One subscription-connection framework for model providers

- Status: accepted (implemented on `feat/subscription-logins`, PR #4)
- Date: 2026-10-24
- Scope: `web/` connection UI, `src/server/app/oauth.py`, `src/server/services/*_oauth.py`, `src/server/services/oauth_catalog.py`, `src/llms/manifest/*`

## Context

Conductor already supported four subscription-account logins (ChatGPT Codex, Claude,
Grok, Gemini) and four API-key/coding-plan routes for domestic providers. PR #4
added Kimi Code and Zhipu (BigModel + Z.ai) account logins. Each new provider
tended to arrive as another hardcoded branch in the backend routes and in
`ConnectedAccounts`, which made every addition bigger without making the next one
cheaper or safer.

The remaining domestic providers split into three groups (see
`docs/oauth-providers.md` for the evidence):

1. Providers with a first-party account-authorization surface that can be used
   without the user creating an API key: Kimi Code (device grant), Zhipu/Z.ai
   (browser authorization + key mint), Xiaomi MiMo (browser authorization +
   encrypted paste-back code that carries a minted coding-plan key).
2. A provider whose account OAuth no longer grants model access: Qwen Code's
   OAuth free tier was discontinued on 2026-04-15 (the CLI itself says so);
   DashScope Token Plan keys remain the supported route.
3. Providers whose official surface is API-key/AK-SK only: MiniMax, StepFun,
   Doubao/Volcengine ARK, DeepSeek.

The UI also had no single place that stated which connection kinds a provider
offers; the user had to discover it through the setup wizard's method card.

## Decision

1. **One capability catalog, served by the backend.** A static module
   (`src/server/services/oauth_catalog.py`) describes every connection option:
   `id`, bilingual display names, `brand`, `region`, `kind`
   (`device|paste|redirect|api_key_only`), `login_supported`,
   `unavailable_reason(_en)`, `api_key_provider`, `website_url`, `models`,
   `order`. `GET /api/v1/oauth/providers` serves it to the UI. It is pure data —
   no credentials, no per-user state.
2. **Four protocol kinds, one frontend state machine.** `useSubscriptionLogin`
   already models `device` / `paste` / `redirect`; MiMo is a `paste` flow whose
   pasted code decrypts to a minted key. No provider gets its own polling or
   cache-refresh code.
3. **API-key fallback is a first-class part of every provider, not a separate
   screen.** Each login descriptor names the `api_key_provider` it falls back to;
   `api_key_only` descriptors point at their own provider id. The same section in
   Settings lists login-capable plans and API-key-only providers with one card
   skeleton.
4. **Do not fake a login.** Qwen is shown as API-key-only with the recorded
   reason (OAuth free tier discontinued); MiniMax/StepFun/ARK/DeepSeek likewise.
   A provider is only listed with `login_supported: true` when a first-party
   client flow was verified against its own artifacts.
5. **Minted keys are ordinary BYOK credentials.** MiMo and Zhipu logins mint a
   long-lived provider key; it is stored in `user_api_keys` (encrypted) under a
   dedicated provider id (`mimo-oauth`, `z-ai-oauth`, `z-ai-cn-oauth`), so model
   routing, readiness, status and disconnect use the existing BYOK paths. The
   account token is never used as a chat credential directly.
6. **Evidence before claims.** Protocol constants come from the provider's own
   client artifact or documentation and are recorded with version/path in
   `docs/oauth-providers.md`; the tests pin the protocol and the security
   invariants, and the PR carries the evidence table.

## Why not one generic OAuth endpoint for every provider?

A single `{provider}`-parameterised route would hide the fact that the four kinds
differ in state handling (device code vs pasted code vs browser callback vs key
mint) and in what material is stored. The catalog plus a small adapter per kind
keeps the differences where they are real, while the UI and the state machine
stay uniform. The existing per-provider route paths are kept for
compatibility with the four international providers and the two new ones; a
future refactor can grow generic routes behind the same adapters without
changing the catalog contract.

## Consequences

- Adding a provider of an already-supported kind is a catalog row + adapter
  fields, not a new UI branch.
- The backend must keep the catalog and the manifest provider ids consistent;
  `tests/unit/server/services/test_oauth_catalog.py` and the manifest tests pin
  the cross-checks (models exist, api-key providers exist).
- Cryptography: MiMo's paste-back payload is an X25519 + AES-256-GCM envelope
  decrypted with a per-flow private key held only in Redis for 10 minutes. The
  implementation uses the already-pinned `cryptography` package (a transitive
  runtime dependency via `asyncssh`/`authlib`); if that ever stops being
  installed, the MiMo login must fail closed instead of adding a new dependency
  silently.
- The Zhipu and MiMo flows use the provider's public first-party client
  identifiers and client-shaped requests. That is a compatibility
  implementation, not a supported integration: each catalog row documents the
  fallback (API key) and the flow must degrade to it if the provider changes.
- BigModel browser authorization still requires a loopback origin; the catalog
  marks the requirement and the UI shows the actionable error otherwise.

## Evidence

- `docs/oauth-providers.md` — capability matrix with source versions and paths.
- `docs/runbooks/oauth-acceptance.md` — how a third party re-runs the acceptance.
- PR #4 evidence table — every acceptance item mapped to a command/URL/result.
