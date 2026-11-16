# The credential broker and the outbound destination boundary

This line closes the `docs/SUPER-AGENT-ALIGNMENT.md` §8.4 gap "No complete endpoint-side independent authorization boundary or general credential capability broker". The row stays **Partial** (34 Aligned / 1 Partial / 0 Missing) until the audit re-evaluates the fixed branch; the mechanism below is implemented and tested, the alignment decision is the reviewer's.

## Purpose and threat model

The broker is the boundary that keeps external-channel credentials and outbound destinations independent of model output. A Worker or a Router whose text was injected cannot

- read a credential for a destination the Source does not allow, or
- steer a reply to a destination the Source configuration and the Ticket origin do not name.

The layer governs *where* credentials and replies may go, never *what* a reply says. Prompt injection that changes the content of a reply is a content-layer risk and is not covered here; the destination boundary holds regardless of what the model wrote.

It has two halves: the credential layer (`CredentialBroker` in `src/server/services/autonomy/credentials.py`) decides where a credential may be used; the outbound endpoint layer (`authorize_outbound` in `src/server/services/autonomy/replies.py`) decides where a reply may be sent. Both are independent of prompt instructions by construction: neither takes reply text or tool-call text as an input to its decision.

## The credential layer

`CredentialBroker` is the single choke point between a credential's policy and the vault. Every credential use in `services/autonomy` goes through a broker, and the broker holds four invariants, each tied to its mechanism:

- **Authorize before reveal.** `CredentialBroker.use` calls `require` and only then `reveal`, so a denied use reads the vault zero times and an allowed use reads it exactly once.
- **Deny by default.** A broker without an allowlist gets `CredentialAllowlist()`, whose every dimension is empty, so it authorizes nothing.
- **Structured audit.** Every denial goes through `audit_denial`: policy version, credential name, user id, method, host, path and the deny reason. The secret and the request body are never part of the record.
- **The secret stays in the caller's scope.** `use` is an async context manager: the secret exists only inside the caller's `async with` block. It is never logged, never placed in an exception or a return value, and authorization runs before the reveal, so a denial cannot carry it either.

The policy version is `CREDENTIAL_POLICY_VERSION = "credential-allowlist-v3"` and is included in every denial record. v2 added the `schemes` dimension so non-HTTP credentials (SMTP) can name the scheme of their destination instead of being forced into HTTPS; v3 added the `ports` dimension, so a policy that names ports binds the destination to exactly those ports — scheme, host and port, not just the host.

Allowlist semantics, enforced by the pure `CredentialAllowlist.check` (no vault, no network):

- **Hosts**: exact, lower-cased host names; no wildcard, scheme or port in an entry; IP literals accepted.
- **Methods**: an explicit token, matched case-insensitively — an HTTP verb for HTTP credentials, or an operation token such as `SMTP_SEND` for non-HTTP ones.
- **Path prefixes**: matched at a segment boundary, so `/repos/acme/widgets` permits that path and its children but not `/repos/acme/widgets-other`.
- **Schemes**: HTTPS by default; HTTP is accepted only on `localhost`, `127.0.0.1` or `::1` (the local-test exception).

URL tricks are refused in `check` before any secret exists: relative URLs, embedded userinfo, query data or fragments, encoded paths, backslashes, dot segments, whitespace or control characters, and invalid ports. `from_mapping` builds a policy from Source config over the dimensions `hosts`, `methods`, `path_prefixes`, `schemes` and `ports`: an omitted dimension keeps the derived default, an explicit empty list denies that dimension (an empty `ports` leaves the port free), and a malformed value fails Source validation rather than becoming a silent deny.

## Per-transport destinations

| Transport | Operation | Destination | Default allowlist | Ordering |
|---|---|---|---|---|
| GitHub | `GET`/`POST` | `https://<api host>/<api-relative path>` | `github_credential_allowlist`: the API host, `GET`+`POST`, `/user` and `/repos/<repository>` | authorize before reveal; a denial reads the vault zero times |
| Slack | `POST` | `https://<webhook_host>/` | the Source's non-secret `webhook_host` (default `hooks.slack.com`) | declared destination authorized **before** the reveal (a declared-host denial reads the vault zero times); the revealed URL's host must equal the declaration, checked after the reveal before any send |
| Email | `SMTP_SEND` | `smtp://<host>:<port>` | exactly the Source-configured host:port; an explicit `credential_allowlist` config mapping (`hosts`/`methods`/`path_prefixes`/`schemes`) overrides per dimension | strict authorize before reveal; the destination comes from config |

**GitHub** keeps two exact layers. `GitHubCredentialBroker.request_json` still runs its own repository-scope checks — method must be `GET`/`POST`, the path must be one of the adapter's repository or `/user` operations, redirects are refused — and raises `GitHubSourceError`, so the external error behavior is unchanged. The credential allowlist is checked underneath, against the API-relative path after any enterprise base path. Every pre-reveal refusal is audited; the only post-request refusal is a redirect, and the token is never passed to the audit writer.

**Slack** declares its destination: the Source config carries a non-secret `webhook_host` (default `hooks.slack.com`; a legacy `allowed_webhook_hosts` list still works — its first entry becomes the declaration). The broker authorizes `https://<webhook_host>/` **before** the vault is read, so a declared-host denial audits and reads the vault zero times. The revealed webhook URL *is* the secret, so every post-read check runs after exactly one reveal and before any send: the raw URL is refused when it carries whitespace or control characters (the URL parser would quietly repair a newline); the shape checks (HTTPS, no userinfo, query, fragment, encoded path or dot segments) keep their refusal; and the revealed host must **equal the declared host exactly** — there is no loopback bypass. Only a *declared loopback* host keeps the local-test exception, and the exception covers the scheme alone (http allowed) — the host comparison still applies. Every post-read refusal audits host-only (**the webhook path is never recorded: it embeds the token**), discards the secret and refuses the send. Invariants: a declared-destination denial reads the vault zero times; a post-read refusal reads exactly once and the secret is never used, logged or audited by value.

**Email** satisfies authorize-before-reveal strictly: the destination is `smtp://<smtp_host>:<smtp_port>` from the Source config, so `_smtp_settings` can wrap the whole vault read in `broker.use(operation="SMTP_SEND", destination=...)` and a denied send never reaches the vault.

## The outbound endpoint layer

`authorize_outbound` in `src/server/services/autonomy/replies.py` is called by `reply_effect` before any transport work — including the GitHub and Feishu reconcile reads. It is independent of prompt instructions by construction: it reads only the Source configuration, the outbox row's identity fields (`source_id`, `thread_key`, and `to_address` for email) and the Ticket origin (`mandate.source_id`, `origin_thread_key`, optional `mandate.reply_source_ids`). Reply body text never enters the decision; it reaches this layer only as the reply `body` and is ignored.

The rules:

- a reply may go only to the Ticket origin Source or to a Source explicitly configured as a reply destination (`reply_source_ids` on the Ticket mandate); any other destination is a cross-Source delivery and is denied;
- a GitHub reply's thread must be the Ticket's origin thread, and when the Source names a repository, the thread must name that repository (`github:<repository>#<number>`);
- an email recipient must equal the Source configuration's `to_address`; an outbox row cannot override it;
- an unknown or incomplete Ticket origin fails closed for external transports, and a GitHub reply whose Ticket has no origin thread is denied (`origin_thread_missing`) — the row alone never carries a GitHub reply out;
- the `local` transport has no external destination and is always allowed;
- a transport this layer does not know returns `unchecked_transport` — the existing fallback in `reply_effect` still refuses to send, so nothing leaves either way.

A denial takes the existing failure path: `mark_dropped("outbound reply denied: <reason_code>")` plus `PermanentEffectError`. Nothing is sent.

## Reason codes

Stable constants in `replies.py` (`OUTBOUND_*`), asserted by tests:

| Code | Meaning | Transport scope |
|---|---|---|
| `ok` | allowed | all |
| `missing_ticket_origin` | Ticket unknown/absent or origin Source unknown; fail closed | all external transports |
| `cross_source` | the destination Source is neither the origin nor a configured reply Source | all external transports |
| `thread_mismatch` | the GitHub row's thread is not the Ticket origin thread | GitHub |
| `origin_thread_missing` | the Ticket's GitHub origin thread is missing, empty, `#0`, or not `github:owner/repo#N`; fail closed — nothing to bind the reply to | GitHub |
| `destination_not_from_source` | the GitHub thread does not name the Source's repository (or is not github-shaped) | GitHub |
| `recipient_mismatch` | the outbox row tried to override the email recipient | email |
| `unchecked_transport` | transport unknown to this layer; the caller's fallback still refuses to send — not a denial | unknown transports |

The credential layer's denial reasons are the free-text strings produced by `CredentialAllowlist.check`; they are audited through `audit_denial` together with the policy version and never become API error text on their own (each transport maps them to its own Source error).

## Audit fields

Every credential denial writes one structured warning through `audit_denial`:

```
[autonomy] credential denied event=credential_denied policy=credential-allowlist-v3 \
  credential=<name> user=<id> method=<METHOD> host=<host> path=<path> reason=<reason>
```

Each value is passed through the existing external-text redactor, truncated, and has control characters flattened to spaces, so one denial stays one log line and credential-shaped text cannot smuggle a secret into the record. There is no secret and no request body in the record, and authorization runs before the reveal, so a denied request cannot put the secret there. This is process logging, not a durable security-audit table; the broader "Make security behavior auditable" row remains open.

## What this does not cover

- **Prompt injection vs reply content.** The destination boundary holds, but what the model writes into a reply is unchanged risk: an injected Worker can still put attacker-chosen words into a reply that is legitimately delivered to the legitimate destination.
- **Direct vault reads in the app layer.** `src/server/app/autonomy.py` still calls `reveal_user_secret` directly in three places: line 198 (GitHub Source check verifies the PAT secret exists before the config is accepted), line 221 (email Source check verifies the SMTP secret exists), line 233 (Slack Source check verifies the webhook secret exists). They are the Source check interface — presence verification at Source create/update time, not outbound sends. That file is outside this round's scope and unchanged.
- **Feishu credentials.** Feishu is unchanged this round. `src/server/services/autonomy/feishu.py` holds no vault credentials at all — its module contract states "The CLI owns app binding and user authorization, so this module holds no Feishu credentials", and poll, check and send go through the `lark-cli` subprocess (`_run_lark`). There is no vault read and therefore no credential for the broker to govern. Feishu replies *are* outbound-authorized: `TRANSPORT_FEISHU` is a known transport in `authorize_outbound`, bound by the same Source/origin and fail-closed rules.
- **Slack's declared-destination design.** The webhook URL is itself the secret, so its host can only be checked after the reveal — which is why the Source now declares a non-secret `webhook_host` that is authorized (and audited on denial) before the vault is read. The raw vault value is judged before `strip`, `urlsplit`, or POST. An empty value is `empty_url`. Whitespace, a character below U+0020, or DEL anywhere in the value is `invalid_url_chars` (a space belongs in the value only as `%20`). Only a value that passes those two checks reaches the scheme, host, and port checks, and the raw value is the value that would be posted — it is not stripped. Every refusal, including those two reason codes and a host or scheme refusal, writes one sanitized denial: `host` is the parsed host, or `""` when parsing never succeeded, and `path` is always `""`. The raw URL and the secret never appear in the audit, the log, or the exception. The POST count is 0. `broker.check` and the transport call the same `_reject_raw_url`, so they agree. After a legal URL is accepted, a client failure is one of three fixed codes: `slack_http_error` (the HTTP status stays on the numeric `status` field), `slack_unreachable`, or `slack_timeout`. That exception text does not carry the webhook URL. IPv6 loopback (`::1`) is outside v1 and stays refused. Transports with config-derived destinations (GitHub, Email) deny with zero vault reads.
- **No persistence, no server API.** There is no migration and no server-API change in this line. `reply_source_ids` is read from the Ticket mandate, but nothing populates it yet: it is reserved for an explicit user-level reply-Source configuration, and today replies must target the origin Source.
- **Live acceptance.** Evidence is offline fakes only; no real Slack webhook, SMTP relay or GitHub endpoint was contacted.

## Tests

All tests use fakes and literals; no vault, network, database or provider is touched.

- `tests/unit/server/services/autonomy/test_credential_allowlist.py` — **30 tests**: empty allowlist denies every dimension; host/method/prefix enforced independently, including the segment boundary; URL tricks, loopback/public HTTP, case-insensitive hosts and ports; `from_mapping` normalization, non-string entries, unknown keys; `authorize` raises and audits; redaction and single-line audit records; the policy version appears in every record.
- `tests/unit/server/services/autonomy/test_github_source.py` — **27 tests**: default scope derivation, explicit/partial/empty/malformed scopes; broker denies a host, a method and a path outside the allowlist before revealing the secret; enterprise base path is API-relative; redirects refused; the exact secret is redacted from provider payloads and errors.
- `tests/unit/server/services/autonomy/test_slack_transport.py` — **37 tests**: webhook URLs that could reach somewhere else are refused with no connection; a declared-destination denial reads the vault zero times and is audited; a revealed URL off the declared host, under the default configuration, is audited once (parsed host, empty path) and never posted; an empty vault value and a raw value carrying whitespace, a control character, or DEL are refused before parsing, with one vault read, one denial, an empty path, and no POST; a legal URL posts once and writes no denial; IPv6 loopback stays refused; a declared loopback host keeps the scheme exception only; an enterprise host must be declared explicitly. A `ClientResponseError`, a connection error, and a timeout become `slack_http_error`, `slack_unreachable`, and `slack_timeout` without the webhook URL.
- `tests/unit/server/services/autonomy/test_email_transport.py` — **21 tests**: the default allowlist names exactly the configured relay; explicit override per dimension; malformed config rejected at config time; an allowlist that excludes the relay denies before the vault; the default config authorizes the relay and reads the vault once.
- `tests/unit/server/services/autonomy/test_outbound_authorization.py` — **22 tests**: the full rule matrix — Source binding, `reply_source_ids`, GitHub thread and repository bindings, email recipient binding, fail-closed missing origin for every external transport, `local` allowed without origin, unknown transport reported `unchecked_transport`.
- `tests/unit/server/services/autonomy/test_channel_reply_delivery.py` — **20 tests**, including end-to-end denials: cross-Source, off-thread and recipient-override replies are dropped with `outbound reply denied: <code>` and raise `PermanentEffectError` without a send. A GitHub origin that is missing, empty, `#0`, or free-form is dropped as `origin_thread_missing` before marker lookup, post, or send. `github:acme/widgets#42` is still posted.
- `tests/unit/server/services/autonomy/test_reply_effect_orphan.py` — **4 tests**, `tests/unit/server/services/autonomy/test_outbox_side_effects.py` — **9 tests** (reconcile-before-retry with markers and deterministic Message-IDs), `tests/unit/server/services/autonomy/test_feishu_source.py` — **10 tests** (drafts, redaction, reconcile via the CLI fakes).

## Validation

Run with this worktree's own virtualenv (never a shared or symlinked one):

```sh
uv sync --frozen --group dev --extra test --extra cli
LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 \
.venv/bin/python -m pytest -q \
  tests/unit/server/services/autonomy/test_credential_allowlist.py \
  tests/unit/server/services/autonomy/test_github_source.py \
  tests/unit/server/services/autonomy/test_slack_transport.py \
  tests/unit/server/services/autonomy/test_email_transport.py \
  tests/unit/server/services/autonomy/test_outbound_authorization.py \
  tests/unit/server/services/autonomy/test_channel_reply_delivery.py \
  tests/unit/server/services/autonomy/test_reply_effect_orphan.py \
  tests/unit/server/services/autonomy/test_outbox_side_effects.py \
  tests/unit/server/services/autonomy/test_feishu_source.py
# 191 passed
```

No real Slack webhook, SMTP relay or GitHub endpoint was contacted; every test runs against fakes and literal placeholders that are not real secrets.
