# Per-credential destination allowlist for the credential broker

This line closes the `docs/SUPER-AGENT-BEYOND.md` §8.4 open problem "Constrain where a credential can be used". The row was **Yes (method, path and redirect checks); partial destination trust**; it is now **Yes (per-credential destination allowlist)**.

## What was ported

The requirement is the open-problem row itself: a broker that accepts an arbitrary endpoint or method can still be used to exfiltrate the credential it holds, and enterprise GitHub endpoints make a host check non-trivial. The design direction is the row's: bind the credential to a validated destination, make enterprise hosts explicit, require HTTPS, and reserve HTTP for loopback tests.

Before this change the GitHub broker in `src/server/services/autonomy/github.py` had useful but implicit checks:

- the HTTP method had to be `GET` or `POST`;
- the request path had to be one of a hardcoded set of repository operations;
- redirects, URL credentials, query data, encoded paths and dot segments were refused before the vault was read;
- the configured `api_base_url` was validated, but there was no per-credential host/method/path-prefix object, no default-deny policy for a credential, and no audit record when a use was refused.

The change makes the destination part of the credential's own policy and makes refusals auditable.

## From which file

| Origin | File | What changed |
|---|---|---|
| Open problem §8.4 | `docs/SUPER-AGENT-BEYOND.md` | The requirement and the design direction recorded in the row. |
| Existing broker | `src/server/services/autonomy/github.py` | Added a `credential_allowlist` field to `GitHubConfig`, derived the default scope from the endpoint/repository, and enforced the scope in `GitHubCredentialBroker` before `reveal_user_secret`. Existing operation checks stay as a second, exact layer. |
| Wire shape | `src/server/models/autonomy.py` | Added `CredentialAllowlistConfig` and the optional `GitHubSourceConfig.credential_allowlist` field. |

New file:

- `src/server/services/autonomy/credentials.py` — the reusable allowlist:
  - `CredentialAllowlist(hosts, methods, path_prefixes)` normalizes and validates its entries.
  - `check(method, url)` is pure and returns a `CredentialDecision`; it never reads the vault or touches the network.
  - `authorize(...)` raises `CredentialDenied` and writes the audit record.
  - `audit_denial(...)` is the single audit writer.
  - `CREDENTIAL_POLICY_VERSION` is included in every record so a later semantic change is distinguishable.

## The policy

A credential may be used only when **all three** dimensions match:

- **Host**: exact host name after lower-casing; no wildcard, scheme or port in the entry. IPv4 and IPv6 literals are accepted. The URL must have no embedded userinfo.
- **Method**: an explicit HTTP method token, case-insensitively matched.
- **Path prefix**: a path-only prefix matched at a segment boundary. `/repos/acme/widgets` permits that path and its children, not `/repos/acme/widgets-other`.

Everything else is denied. Missing or empty lists deny that dimension, so `CredentialAllowlist()` permits nothing.

Additional URL rules, all enforced before the secret is read:

- the destination must be absolute and must use HTTPS, except HTTP on `localhost`, `127.0.0.1` or `::1` for local tests;
- query data, fragments, URL credentials, whitespace/control characters, backslashes, percent-encoded paths and `.`/`..` segments are refused;
- redirects are still refused (`allow_redirects=False`, and a 3xx is a denial), so a permitted path cannot be turned into an unlisted destination.

For GitHub the default scope is the configured `api_base_url` host, `GET`/`POST`, and the two API-relative prefixes the adapter uses: `/user` and `/repos/<owner>/<name>`. An explicit Source config narrows the scope one dimension at a time: an omitted dimension keeps the derived default, while an explicit empty list denies that dimension. A config naming only `methods`, for example, still permits only the endpoint host and the repository/user prefixes.

```json
{
  "credential_allowlist": {
    "hosts": ["github.example"],
    "methods": ["GET"],
    "path_prefixes": ["/repos/acme/widgets"]
  }
}
```

The prefix is API-relative, after an enterprise base path: with `api_base_url = "https://github.example/api/v3"`, `/user` means `https://github.example/api/v3/user`. Malformed values fail Source validation with a `GitHubSourceError`, rather than becoming a silent deny.

## Audit record

Every denial writes one structured warning through `audit_denial`:

```
[autonomy] credential denied event=credential_denied policy=credential-allowlist-v1 \
  credential=<name> user=<id> method=<method> host=<host> path=<api-relative path> reason=<reason>
```

The record contains no parameters or request body, and `credential`, `user`, `method`, `host`, `path` and `reason` are passed through the existing external-text redactor and truncated before logging. Authorization runs **before** `reveal_user_secret`, so a denied request cannot put the secret in the record. A redirect denial happens after the request, but the token is only in the Authorization header and is never passed to the audit function.

This is process logging, not a durable security-audit table; the broader "Make security behavior auditable" row remains open.

## Tests

All tests use fakes and literals; no vault, network, database, backend or provider is touched.

New tests in `tests/unit/server/services/autonomy/test_credential_allowlist.py` — **22 tests**:

- an empty allowlist denies each dimension;
- a configured host/method/prefix allows the path and its children;
- each dimension is enforced independently, including the segment-boundary case;
- thirteen URL-trick cases (userinfo, query, fragment, empty query/fragment, public HTTP, dot segments, encoded path, backslash, newline, port zero, relative URL, missing host);
- loopback HTTP allowed, public HTTP refused, IPv6 loopback;
- case-insensitive host matching and port handling;
- `from_mapping` normalization and malformed/unknown config rejection;
- `authorize` raises and audits;
- the audit record redacts credential-shaped path text and carries the policy version.

Added to `tests/unit/server/services/autonomy/test_github_source.py` — **10 new tests** (the file now has 27):

- the default scope derives from the endpoint and repository;
- an explicit scope is used;
- a partial explicit scope keeps the derived default for every omitted dimension;
- an explicit empty list denies that dimension;
- a malformed scope fails config parsing;
- a repository that cannot form a safe prefix is rejected as config, not a runtime error;
- the broker denies a host, a method and a path outside the scope before revealing the vault secret;
- an enterprise base path is treated as API-relative.

Existing tests extended: the operation-denial and redirect tests now also assert the audit record exists; the redirect test asserts the revealed token is absent from the log.

## Validation

Run with the approved fallback interpreter:

```sh
../../Documents/deepseek-harness/default-workspace/AlphaCN/.venv/bin/python -m pytest \
  tests/unit/server/services/autonomy/test_credential_allowlist.py \
  tests/unit/server/services/autonomy/test_github_source.py -q
# 49 passed

../../Documents/deepseek-harness/default-workspace/AlphaCN/.venv/bin/python -m pytest \
  tests/unit/server/services/autonomy -q
# 370 passed

../../Documents/deepseek-harness/default-workspace/AlphaCN/.venv/bin/python -m pytest \
  tests/unit/server/services/autonomy tests/unit/server/test_autonomy_api.py \
  tests/unit/server/models tests/unit/server/test_import_layering.py -q
# 700 passed
```

The full server unit suite at the final tree collected 8,800 tests: **8,796 passed, 2 skipped, 2 failed**. Both failures are `tests/unit/server/services/native_runtime/test_login.py` raising `OSError: out of pty devices` before any assertion, an environment limit unrelated to the autonomy change.

Ruff passed on all five changed or new Python files. No credential was read, printed or copied; the literal placeholders in the tests are not real secrets. No network call, live GitHub connection, frontend change, backend on `:8000` or live database was used.

## What is not in this port

- **GitHub only.** The reusable allowlist exists, but `GitHubCredentialBroker` is the only broker wired to it. Slack's webhook URL already has its own host allowlist and its path is the secret, so it is not a URL-prefix policy; email is SMTP, not an HTTP broker.
- **No signed per-call capability.** The allowlist is configuration attached to the Source's credential, not a short-lived capability token bound to one operation.
- **No durable audit table.** Denials go to the process log. A multi-worker deployment must ship them to its log sink to retain them.
- **No live acceptance.** The policy and broker are covered by unit tests with fakes; no real enterprise endpoint or live GitHub call was made.
