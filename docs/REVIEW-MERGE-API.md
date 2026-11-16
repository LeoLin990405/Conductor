# Review and merge API

The Super-agent CLI used to keep its cross-review queue in per-user JSON files
and to run the merge itself (`pytest`, then `git merge`) inside the CLI
process. This line moves both halves onto the Conductor server:

- the review queue and its verdicts are durable Postgres rows, shared by every
  client and impossible to skip from a second process;
- the merge is a privileged, host-local server operation whose gate is a Codex
  review the server runs itself: it rehearses the merge in a temporary git
  worktree, runs the tests there, requires Codex's `VERDICT: MERGE` on that
  exact rehearsal, and only then fast-forwards the target branch onto the
  rehearsed merge commit.

The CLI commands keep their flags and their `--json` envelope; they now call
these APIs instead of touching worktrees and running git locally. The old CLI's
per-user JSON queue (`~/.config/conductor/reviews`, or `CONDUCTOR_REVIEW_HOME`)
is not read or imported by the server.

Related: [CLI-SUPERAGENT.md](CLI-SUPERAGENT.md), `MIGRATIONS.md` at the repo root
(revision 139; `MIGRATIONS.md` names the current head),
`src/server/app/review_merge.py`, `src/server/services/autonomy/review_merge.py`,
`src/server/database/autonomy/reviews.py`.

## Endpoints

All routes are under `/api/v1`, authenticated like every other route (the CLI
sends the Bearer token it reads from `~/.config/conductor/token`; with
`HOST_MODE=oss`, the default, the server does not look at it and every request is
the single local user, `AUTH_USER_ID`, default `local-dev-user`; the one exception
is service-to-service auth: when `INTERNAL_SERVICE_TOKEN` is set on the server and
a request carries a matching `X-Service-Token`, the user is its `X-User-Id` header
in either host mode, and a wrong service token or a missing `X-User-Id` is a
`401`), and require the
`super_agent` feature. That feature is on for every user by default (catalog
gate `none`); a deployment switches it off, or makes it a per-user opt-in, with a
`features.super_agent` entry in `config.yaml` (`enabled`, `gate`). A disabled
feature answers `404` with the plain string `"Feature not enabled"` (hidden
surface; not the structured error below). That check runs first in every handler
(after authentication and request validation), so a remote merge caller without
the feature sees `404`, not `403`. Rows are per user: another user's review id is
a `404 review_not_found`, and the list and audit routes only return the caller's
rows.

| Method | Path | Success | Purpose |
|---|---|---|---|
| `POST` | `/reviews` | `201` | queue one cross-review |
| `GET` | `/reviews` | `200` | list the caller's reviews, newest first (`line` filter, `limit` 1-200, default 50) |
| `GET` | `/reviews/{review_id}` | `200` | one review (`review_id` is a UUID) |
| `POST` | `/reviews/{review_id}/verdict` | `200` | finish a queued review (`merge` or `block`) |
| `GET` | `/review-audit` | `200` | the durable review/merge audit trail, newest first (`line` and `action` filters, `limit` 1-200, default 100) |
| `POST` | `/merges` | `200` | rehearse, Codex-review and land a merge; **local callers only** |

A malformed body or query (a non-UUID `review_id`, `limit` out of range, an
over-long field, a verdict that is not lowercase `merge`/`block`) is a `422` in
the app-wide redacted shape `{"detail": [{"type", "loc", "msg"}]}`; see
[SUPER-AGENT.md](SUPER-AGENT.md), "Request validation errors".

### `POST /reviews`

```json
{
  "line": "review-merge-api",
  "reviewer": "codexrev",
  "provider": "codex",
  "root": "/home/you/conductor-wt"
}
```

`line` and `reviewer` are 1-200 characters. `provider` defaults to `codex`; it is
stripped and lower-cased and must then match `[a-z0-9_.-]{1,64}`
(`400 invalid_provider`; a value over 64 characters never gets that far and is a
`422`). It is recorded as workflow metadata and never unlocks a merge. `root` (up to 4096
characters) is optional in the wire shape but the CLI always sends its own root;
the server accepts it only when it resolves to the root it was configured with
(`review_merge.worktree_root`, overridable with `CONDUCTOR_WT_ROOT`). A mismatch
is a `409 worktree_root_mismatch` that names the server's root (in the message
and `detail.server_root`) and not what the request's root resolved to, so the
answer does not show where a path on the host leads; a root that cannot be
resolved is `400 worktree_root_invalid`. A client can never point the server at
an arbitrary filesystem path.

Server-side permission rules, in order:

1. the caller is authenticated;
2. the `super_agent` feature is enabled for them;
3. both `line` and `reviewer` resolve to git worktrees directly under the
   configured root (a request names a line, never a path; a name with a path
   separator, or a symlink that resolves outside the root, is
   `404 line_not_found`);
4. **a line may never review itself** — identical names, or two names that are
   the same directory on a case-insensitive filesystem or through a symlink
   inside the root, are refused (`409 self_review_refused`). The database
   repeats the name-level check with `CHECK (line <> reviewer)`.

The review pins the line's current `HEAD` as `head_sha` (an unreadable line is
`409 review_line_unreadable`). `state` is `queued` until a verdict arrives, then
`completed`; the table also allows `failed`, but no code path sets it. The
response keeps the shape the file-backed queue printed, plus the new fields:

```json
{
  "id": "3a4f...",
  "line": "review-merge-api",
  "reviewer": "codexrev",
  "provider": "codex",
  "state": "queued",
  "verdict": null,
  "head_sha": "3f2a…",
  "note": "",
  "created_at": "2026-11-04T12:00:00Z",
  "completed_at": null
}
```

### `POST /reviews/{review_id}/verdict`

```json
{"verdict": "merge", "note": "diff reviewed; tests targeted", "root": "/home/you/conductor-wt"}
```

`verdict` must be lowercase `merge` or `block` on the wire (the CLI lowercases
`--verdict MERGE`); `note` is up to 2000 characters. The line HEAD is captured
when the review is *queued* (it is already in the `head_sha` response field).
The verdict is a compare-and-set: it is accepted only while the line is still
on that commit, otherwise the server answers `409 review_stale` (with
`detail.reviewed_sha` and `detail.head_sha`) and the operator must queue a fresh
review. The server re-reads the line HEAD for that check, so the verdict call
also applies the `root` match and can answer `404 line_not_found` or
`409 review_line_unreadable`. Only a queued review can be completed; a second
verdict is `409 review_not_open`, and an unknown id is `404 review_not_found`.

The queue is the operator's durable review record. It is deliberately **not**
the merge gate: its verdict is caller-asserted workflow metadata, and wiring it
into merge would create a race between recording a verdict and landing. The
merge route instead runs the Codex CLI itself against the exact commit it is
about to land and requires `VERDICT: MERGE` from Codex's final answer, so a
forged or stale queue record cannot land a commit. The queue remains the place
to track and audit review requests and their reported verdicts.

## `POST /merges`

```json
{
  "line": "review-merge-api",
  "into": "integrate/r2",
  "test_cmd": ".venv/bin/python -m pytest tests/unit/conductor_cli -q",
  "root": "/home/you/conductor-wt"
}
```

`line` and `into` are 1-200 characters, `test_cmd` up to 4096 and `root` up to
4096. `into` must be a valid branch name (`git check-ref-format --branch`;
`400 invalid_target_branch` otherwise).

### Local-only privilege

Merge executes `git`, a test command and the Codex CLI against host worktrees,
so it is available **only to a local caller** on the machine running the
backend. Three independent conditions are required, and there is no remote
path:

1. the transport peer (`request.client.host`) is exactly `127.0.0.1`, `::1` or
   `localhost` (a fixed set; an IPv4-mapped peer such as `::ffff:127.0.0.1` is
   not accepted);
2. none of these forwarding headers is present: `Forwarded`, `X-Forwarded-For`,
   `X-Forwarded-Host`, `X-Forwarded-Proto`, `X-Real-IP`, `CF-Connecting-IP`,
   `True-Client-IP`, `X-Client-IP` — a proxy adds one, so a proxied request is
   refused even when it reaches the server from `127.0.0.1`. The list is exact:
   other `X-Forwarded-*` headers (for example `-Port`) are not checked;
3. the `Host` name (compared without its port, lower-cased, trailing dot dropped)
   is one the operator configured as local (`review_merge.local_hosts` or
   `CONDUCTOR_MERGE_LOCAL_HOSTS`, default `127.0.0.1`, `::1`, `localhost`).
   Adding a host name there is the *explicit local permission* an operator can
   grant a same-host front end; the peer must still be loopback and the
   forwarding-header rule still applies.

A remote or proxied caller gets `403 local_only`, and the attempt is written
to the audit log as a `merge_refused` row (best effort: a database failure
there is logged, not raised). The check does not trust forwarding headers for
the positive case either. The CLI sends merges to `--url` / `CONDUCTOR_URL`
(default `http://127.0.0.1:8000`), so the default already satisfies condition 3.

`test_cmd` is run as an argv by the server's own OS user with the server's
environment (see [The rehearsal](#the-rehearsal)), which is why this route is
local-only.

### Preconditions, in order

1. the `super_agent` feature (`404` otherwise), then the local gate (loopback
   peer + local Host + no forwarding headers, `403 local_only` otherwise);
2. the configured worktree root matches the request;
3. `line` is a git worktree directly under the root, on a branch (not detached,
   `line_detached`), not already on `into` (`same_branch`);
4. exactly one worktree under the root is checked out on `into` (the target),
   and it is not the line's own worktree (`same_worktree`);
5. both worktrees are clean (the target is checked first) — untracked files
   count as dirty. Tests run in a rehearsal worktree built from the pinned
   commits, so a dirty tree would test state that never lands;
6. the rehearsal and its tests (next section; the test command is parsed at its
   start, so `400 test_command_invalid` is only reported once steps 1-5 pass);
7. the mandatory merge-time Codex review, whose final answer must end with
   `VERDICT: MERGE`;
8. neither side advanced or changed during the rehearsal/Codex run;
9. the durable `merge_started` audit row (a database failure refuses before git
   moves), then the CAS landing on `refs/heads/<into>`. The server does not
   rewrite the target worktree; the response says `worktree_sync_required`.

Whether the line is already merged is only discovered in step 9: the rehearsal
merge is then a no-op (its HEAD equals the target), so the tests and the Codex
review still run and a `merge_started` row is written before the
`409 already_merged` refusal.

### The rehearsal

The server creates a temporary detached worktree from the target repo at the
pinned target SHA (`git worktree add --detach --force <tmp> <target-head>`),
merges the pinned line SHA into it (`git merge --no-ff`, creating the merge
commit the real merge would create), and runs the test command with that
worktree as cwd:

- the request's `test_cmd` (`--test-cmd`), else `review_merge.test_command` /
  `CONDUCTOR_MERGE_TEST_CMD` when configured (split with
  `shlex`, run as an argv list — never a shell). A relative first argument
  containing a `/` that exists only in the line worktree is anchored there, so
  the pre-server invocation `--test-cmd '.venv/bin/python -m pytest -q'` keeps
  working even though the rehearsal worktree has no `.venv`; a bare command
  name (`pytest`) is looked up on `PATH` as usual;
- otherwise the line worktree's `.venv/bin/python -m pytest -q` when it exists,
  else the server interpreter with `-m pytest -q`.

The test process inherits the server's full environment, with `CONDUCTOR_WT_ROOT`
set to the configured root and a `PYTHONPATH` that lists a temporary import
guard, the rehearsal tree, its `src/` and (when present) `libs/ptc-cli`, ahead
of any `PYTHONPATH` the server already had. The guard is a `sitecustomize`
that strips the line checkout and the server's own checkout from `sys.path`
(paths inside a `.venv` there are kept) and removes editable-install import
finders from `sys.meta_path`, so a package the merge deleted cannot
silently import from the unmerged checkout. It reads the two checkout paths from
`CONDUCTOR_REHEARSAL_LEAK_PREFIX` (the line) and `CONDUCTOR_REHEARSAL_SERVER_PREFIX`
(the server), which the server sets for the test process only. The process runs
in its own process group, which is killed when `test_timeout_seconds` elapses
(`tests_timeout`). The rehearsal worktree, the import guard and the Codex
final-answer file are created in the server's temporary directory
(`conductor-merge-*`, `conductor-import-guard-*`, `conductor-codex-final-*.txt`),
not under the worktree root.

The rehearsal worktree is removed in a `finally` (a removal failure is logged,
never raised over the result), and the target branch is not touched
until the tests are green. Because the tests run against the actual merged
tree, a change that only breaks after combining both branches is caught before
the target moves. The worktree must still be clean and on the same commit when
the tests and Codex finish; a test command that edits tracked files, adds
untracked files that are not git-ignored, sets assume-unchanged/skip-worktree
flags or commits is refused (`rehearsal_modified`), because otherwise the tested
contents would differ from the commit that lands.

### The mandatory Codex review

After the tests are green the server runs the Codex CLI against the same
rehearsal worktree:

```text
<codex_command> exec --sandbox read-only --color never -C <rehearsal> \
  --output-last-message <file> <prompt>
```

`<codex_command>` is `review_merge.codex_command` split with `shlex`, so it may
carry its own arguments; `<file>` is a temporary file the server deletes
afterwards. The process runs with the rehearsal as cwd and inherits the server's
environment, and it runs in its own process group like the tests (`codex_timeout`
kills the group). The argv above is exactly what `_run_codex_merge_review`
builds. The flags exist in `codex exec --help` of codex-cli 0.162.1 (last checked
2026-11-05); a review against the real Codex service is not part of the unit or
end-to-end tests, which script the reviewer.

The prompt asks Codex to review `git diff <tested target> HEAD`, inspect the
changed files and their tests, not modify anything, and end with exactly one
line — `VERDICT: MERGE` or `VERDICT: BLOCK`. The verdict is read from Codex's
**final answer file** (`--output-last-message`, the last 64 KiB of it), never
from stdout/stderr: diagnostics can quote the prompt or an earlier draft, so a
stray `VERDICT: MERGE` there must not gate a merge. The last non-empty line of
the final message must be the verdict line (matched case-insensitively,
surrounding blanks allowed); anything else is `codex_verdict_missing`. The merge
lands only on `MERGE`; a non-zero exit, timeout, missing binary, missing verdict
or `BLOCK` is a structured error and the target is untouched. Codex's stdout,
stderr and final message are never returned or stored — a failure carries only
its exit code, the timeout, or the text of the error that stopped the binary
from starting. A `BLOCK` therefore comes back without its findings
(`codex_blocked` carries only `verdict` and `review_output_omitted`); to read
them, run the same review yourself. A real merge needs the Codex CLI installed
and logged in on the backend host; the unit and end-to-end tests replace
`_run_codex_merge_review`, so they run without Codex.

After a green rehearsal and a merging Codex verdict the server re-verifies
branches, cleanliness and HEADs, writes the durable `merge_started` audit row
(a database failure refuses the merge before git moves), and lands the merge
with one ref-only compare-and-set:

```text
git -C <target> update-ref --no-deref refs/heads/<into> <rehearsed merge commit> <tested target head>
```

The rehearsal merge commit already lives in the target repository's object
database (linked worktrees share it), and the CAS either moves the named branch
onto the byte-identical tested+reviewed commit or changes nothing
(`409 target_advanced`). Landing does **not** touch the target worktree or
index: a checked-out worktree can hold uncommitted edits or be changed by
another process, and rewriting it server-side is the filesystem/race surface
this contract deliberately drops. The success response therefore says
`worktree_sync_required: true` and names the landed ref; the caller syncs its
checkout (for example `git -C <target> reset --hard`). Nothing is pushed. Until
that sync, the target worktree still holds the old tree against the moved ref, so
`git status` shows it as dirty (the landed changes appear reversed in the index,
for example a file the line added shows as a staged deletion) and the next merge
into it is refused `target_dirty`.

```json
{
  "merged": true,
  "line": "review-merge-api",
  "branch": "feat/review-merge-api",
  "into": "integrate/r2",
  "commit": "…",
  "tested_sha": "…",
  "tests": "green",
  "pushed": false,
  "tested_in_temporary_worktree": true,
  "codex_verdict": "merge",
  "landed_ref": "refs/heads/integrate/r2",
  "worktree_sync_required": true
}
```

## Structured errors

A refusal or failure answers with a non-2xx status and FastAPI's `detail`
carrying a stable structure:

```json
{
  "detail": {
    "code": "tests_failed",
    "message": "The merge rehearsal is red; the target branch was not touched.",
    "remediation": "Fix the failing tests on the line branch, commit, then retry; the merge-time Codex review runs again automatically.",
    "detail": {"output_tail": "…"}
  }
}
```

`detail.detail` is code-specific (here the scrubbed tail of the test output;
`merge_conflict` lists up to 20 `conflicts`, `codex_failed` has `exit_code`,
`target_worktree_missing` none). `conductor` prints `message` and `remediation`.
For 4xx refusals the CLI's JSON envelope keeps `errors[0].code == "cli_error"`
and exit code 1 (the machine-readable server `code` is not copied into the
envelope); classified 5xx errors use the server-error path
(`errors[0].code == "server_error"`, exit code 3) and still carry the server's
message/remediation. A 5xx body is only echoed when it has all of `code`,
`message` and `remediation`; any other 5xx body is never echoed (the CLI prints
`HTTP <status> from Conductor`). In `--json` mode a failure envelope goes to
stderr. Codes:

| Code | Status | Meaning / remediation |
|---|---|---|
| `local_only` | 403 | merge from a non-loopback peer, a non-local `Host`, or with a forwarding header; run it on the backend host |
| `worktree_root_mismatch` | 409 | CLI root ≠ server root; set `CONDUCTOR_WT_ROOT` for both |
| `worktree_root_invalid` | 400 | resolving the claimed root raised an `OSError`, a `RuntimeError` (`~name` for a user that does not exist) or a `ValueError` (a NUL byte); the error text, which quotes the path, is not returned |
| `line_not_found` | 404 | no git worktree of that name directly under the root; the message says `Unknown line` or `Unknown reviewer` and does not repeat the name |
| `invalid_target_branch` | 400 | `into` is not a valid branch name |
| `line_detached` | 409 | line has no branch; check one out and commit |
| `same_branch` / `same_worktree` | 409 | the line is already on `into`, or its worktree is the target's; name a different line |
| `line_dirty` / `target_dirty` | 409 | commit or stash before merging (untracked files count) |
| `target_worktree_missing` | 409 | check out the target branch in a worktree under the root |
| `target_worktree_ambiguous` | 409 | leave exactly one worktree on the target branch |
| `target_worktree_moved` | 409 | the target worktree switched branches while it was being resolved; retry from a stable checkout |
| `target_worktree_outside_root` | 409 | the target worktree is a symlink escaping the root; use a real worktree |
| `test_command_invalid` | 400 | `test_cmd` / `test_command` is not a valid `shlex` string (for example an unbalanced quote). A blank `test_cmd` is not an error: it falls back to `review_merge.test_command`, then the default command |
| `test_command_failed` | 409 | the test command could not be started (missing executable) |
| `test_worktree_failed` | 409 | `git worktree add` for the temporary rehearsal worktree failed (disk, permissions on the target repository). Failing to create the temporary directory itself is not mapped here; it is a `500 merge_internal_error` |
| `merge_conflict` | 409 | resolve the conflicts on the line branch, re-review, retry; target untouched |
| `merge_failed` | 409 | the rehearsal merge failed without conflicts; target untouched |
| `tests_failed` | 409 | fix the red tests, re-review, retry; target untouched |
| `tests_timeout` | 409 | focused test command too slow; raise the timeout or narrow it |
| `codex_unavailable` | 409 | Codex CLI missing/not executable; install it or set `codex_command` (500 when the configured command is whitespace-only; an empty `codex_command` is rejected when the config loads) |
| `codex_command_invalid` | 500 | `codex_command` is not a valid `shlex` string |
| `codex_failed` | 409 | Codex exited non-zero (auth, prompt, tooling); exit code only is returned |
| `codex_timeout` | 409 | Codex review exceeded `codex_timeout_seconds` |
| `codex_verdict_missing` | 409 | Codex's final message did not end with a `VERDICT:` line; check its invocation |
| `codex_blocked` | 409 | Codex returned `VERDICT: BLOCK`; its findings are not returned, so run the review yourself to read them, fix, and re-review |
| `rehearsal_modified` | 409 | the test command or Codex changed the rehearsal worktree (edits, untracked files, index flags, commits); make tests read-only wrt the checkout |
| `line_changed` / `target_changed` | 409 | a worktree became dirty or switched branch during the rehearsal; retry |
| `line_advanced` / `target_advanced` | 409 | a branch moved during the rehearsal/landing; git changed nothing; retry |
| `already_merged` | 409 | the line is already an ancestor of the target; nothing to land (found late, see Preconditions) |
| `audit_unavailable` | 503 | the durable `merge_started` row could not be written; no merge attempted |
| `git_failed` | 409 | a git command in a worktree failed; inspect it with `git status` |
| `git_unavailable` / `git_timeout` / `root_unreadable` / `merge_internal_error` | 500 | git is missing or hung (60s per call), the root is unreadable, or an unexpected error; see the backend log |
| `self_review_refused` | 409 | (review API) a line cannot review itself |
| `invalid_provider` | 400 | (review API) provider is not a short lowercase identifier |
| `review_not_found` | 404 | (review API) unknown id, or another user's review |
| `review_not_open` | 409 | (review verdict API) the review already has a verdict |
| `review_stale` | 409 | (review verdict API) the line moved after the review was queued; queue a fresh review |
| `review_line_unreadable` | 409 | (review API) the line's HEAD could not be read to pin or verify the review |
| `invalid_audit_action` | 400 | (`GET /review-audit`) `action` is not one of the audit actions below |

The service also defines `invalid_verdict` (400), but it cannot be reached over
HTTP: the request model only admits lowercase `merge`/`block`, so any other value
is a `422` before the service runs.

Each merge error carries a kind that decides its audit row (see
[Durable audit](#durable-audit)). The refusals — everything a caller can fix
before the rehearsal (`local_only`, `worktree_root_*`, `line_not_found`,
`invalid_target_branch`, `line_detached`, `same_*`, `*_dirty`,
`target_worktree_*`, `test_command_invalid`), plus `codex_blocked` and
`already_merged` — are written as `merge_refused` with outcome `refused`. Every
other merge error (red or timed-out tests, conflicts, Codex errors, git errors,
`rehearsal_modified`, moved branches, `audit_unavailable`,
`merge_internal_error`) is written as `merge_failed` with outcome `failed`.

The failure `output_tail` (and the git `output` field) is scrubbed for
credentials on a best-effort basis and bounded by `review_merge.output_tail_chars`
(default 2000, at most 20000; `0` disables truncation, an explicit operator
choice): the last 30 non-empty lines, cut to that many characters. The scrub is a
name and shape heuristic, not a guarantee. It removes the values of server
environment variables whose *names* contain `token`, `secret`, `password`,
`passwd`, `api key` (`api_key`, `api-key`, `apikey`), `private key` or
`credential` (case-insensitive), plus URL userinfo, bearer tokens,
`api_key=`/`authorization:`-style parameters, `?key=`/`?token=`-style URL query
values, and the `sk-`, `AIza`, `ghp_`-family, `github_pat_` and `glpat-` shapes.
A secret under another name or shape that a test command prints (checked: the
values of `DB_PASS` and `SUPABASE_SERVICE_ROLE_KEY` come back unredacted) is not
removed, so keep secrets out of test output. Several `detail.reason` fields are
only truncated to 200 characters (`git_unavailable`, `root_unreadable`) or run
through the general error-text sanitizer without the environment-value scrub
(`test_command_invalid`, `codex_unavailable`, `merge_internal_error`), and
`worktree_root_mismatch` and `root_unreadable` put the server's absolute root path
in their message (`worktree_root_mismatch` also in `detail.server_root`). A
refusal on a review route (they are authenticated and remote-available) does not
repeat a `reviewer`, `root` or `action` the request chose, nor a `line` that did
not resolve to a worktree under the root. The self-review and unreadable-head
refusals do name the line, which by then is a worktree that exists there; the
merge route (loopback only) names its line and branches.
Raw process output (`output`, `output_tail`) is never stored in the audit table.

## Durable audit

`GET /review-audit` returns the caller's audit rows newest-first, filterable by
`line` and `action`:

```json
{"audit": [
  {
    "audit_id": "…",
    "action": "merge_merged",
    "line": "review-merge-api",
    "reviewer": "",
    "target_branch": "integrate/r2",
    "outcome": "ok",
    "detail": {
      "tested_sha": "…",
      "commit": "…",
      "codex_verdict": "merge",
      "tested_in_temporary_worktree": true,
      "landed_ref": "refs/heads/integrate/r2",
      "worktree_sync_required": true
    },
    "created_at": "2026-11-04T12:00:00Z"
  }
]}
```

Actions: `review_requested`, `review_completed`, `merge_refused`,
`merge_started`, `merge_merged`, `merge_failed`; `outcome` is `ok`, `refused` or
`failed` (the table enforces both lists). `detail` by action:

| Action | `detail` keys |
|---|---|
| `review_requested` | `review_id`, `reviewer_provider`, `head_sha` |
| `review_completed` | `review_id`, `verdict`, `head_sha`, `reviewer_provider` |
| `merge_started` | `tested_sha` (line), `tested_target_sha`, `merge_commit`, `codex_verdict` |
| `merge_merged` | `tested_sha`, `commit`, `codex_verdict`, `tested_in_temporary_worktree`, `landed_ref`, `worktree_sync_required` |
| `merge_refused` / `merge_failed` | `code`, `message`, `remediation` and the code's own `detail` (as in [Structured errors](#structured-errors)) minus any raw output field |

Merge rows leave `reviewer` empty. Review mutations write their audit row in the
same explicit transaction as the review row (the conversation pool runs with
autocommit, so the transaction block is what makes it atomic), and a crash
cannot leave a recorded review without its history. Only a review that was
actually recorded is audited: a refused request or verdict
(`self_review_refused`, `review_stale`, ...) writes no row.

A merge writes `merge_started` **before it moves the target branch** (the
rehearsal's git work in the temporary worktree has already happened), with the
tested SHAs and the Codex verdict; if that row cannot be written the merge is
refused (`audit_unavailable`) so a landing always has a durable intent record. A
`merge_started` row with no matching `merge_merged`/failure row therefore means
the process died mid-merge, or the final audit write failed (it is best effort);
reconcile it against the target branch's `git log`. The final
`merge_merged`/`merge_refused`/`merge_failed` row is written after the git
operation (which cannot share a Postgres transaction). A merge that finds the
line already merged writes `merge_started` and then `merge_refused`
(`already_merged`). Audit rows keep structured fields only — raw test/Codex
output is bounded and scrubbed (best effort, see
[Structured errors](#structured-errors)) for the HTTP response and never copied
into the table.

## CLI

```bash
conductor review run review-merge-api --by codexrev --provider codex
conductor review status review-merge-api
conductor review verdict 3a4f… --verdict MERGE --note "diff reviewed"
conductor merge review-merge-api --into integrate/r2
conductor merge review-merge-api --into integrate/r2 --test-cmd '.venv/bin/python -m pytest tests/unit -q'
conductor --json merge …        # same envelope as before: {api_version, command, timestamp, data, errors, exit_code}
```

`--verdict` is case-insensitive on the command line (`MERGE` or `merge`; the CLI
lowercases it). `--json` is a top-level flag (a trailing `--json` on its own is
also accepted). The commands call `POST /reviews`, `GET /reviews`
(`review status` always asks for `limit=100`, plus `line` when given),
`POST /reviews/{id}/verdict` and `POST /merges`; no `conductor` command calls
`GET /reviews/{id}` or `GET /review-audit`. `review run`, `review verdict` and
`merge` send the CLI's own root in `root`. The CLI talks to `--url` /
`CONDUCTOR_URL` (default `http://127.0.0.1:8000`) and refuses a non-HTTPS URL
outside loopback.

The CLI waits up to `CONDUCTOR_MERGE_TIMEOUT_SECONDS` (default 11,100s, clamped
to 60-86,400s; a non-numeric value falls back to the default) for a merge
request, covering the maximum server budget (7,200s tests + 3,600s Codex) with
margin. Two Super-agent front ends run the CLI as a subprocess and carry their
own guard: the line shell (`ptc_cli/superagent/shell.py`) follows a configured
value (that value + 120s, floor 11,400s, ceiling 90,000s) so it does not kill
the request first, while the full-screen TUI's `/merge` uses a fixed 11,100s and
does not follow the variable. Ordinary commands keep the client's 30s default.
Classified structured errors — including 5xx `audit_unavailable` and
Codex-configuration failures — reach the operator as `message` + `remediation`;
unclassified 5xx bodies are never echoed. The JSON envelope keeps the stable
codes: 4xx `cli_error`/exit 1, 5xx `server_error`/exit 3.

The CLI still resolves the line locally first (a typo fails without a round
trip) and still refuses a self-review before calling the server; the server
repeats both rules authoritatively.

## Configuration

The committed `config.yaml` has no `review_merge:` block, so every value below is
the default of `ReviewMergeConfig` (`src/config/models.py`); add the block to
change one. `config.yaml` is read once per backend process (the loader is
`lru_cache`d), so an edit needs a restart; so does a change to the environment
overrides below, because they come from the server process's own environment:

```yaml
review_merge:
  worktree_root: ~/conductor-wt
  test_command: ""            # default command for the rehearsal worktree
  test_timeout_seconds: 3600  # hard cap on the test run (max 7200)
  output_tail_chars: 2000     # bounded, scrubbed failing-output tail (0-20000)
  codex_command: codex        # required: merge-time Codex review executable
  codex_timeout_seconds: 900  # hard cap on the Codex review (max 3600)
  local_hosts: [127.0.0.1, "::1", localhost]  # Host names merge may use
```

A value outside its range (for example `test_timeout_seconds: 8000`) or an empty
`codex_command` fails validation when the config loads (a `ValidationError`).
`local_hosts` entries must be bare host names: an entry with `/`, a space, `@`,
brackets or a control character is rejected (so IPv6 is written `"::1"`), they
are stored lower-case without a trailing dot, and at least one is required. The
request's Host is compared without its port, so an entry that carries a port
never matches.

Environment overrides (set them in the environment the backend starts with. The
server looks them up on every call rather than caching them, but a running
process's environment cannot be changed from outside, so in practice only
in-process code such as a test changes them without a restart. An empty value is
ignored, a non-numeric timeout is ignored with a warning, and **if the merged
result fails the `ReviewMergeConfig` validation, the server drops every
environment override** below with one warning (`ignoring invalid review_merge
environment overrides`) and falls back to the `config.yaml` values,
`CONDUCTOR_WT_ROOT` included. A `CONDUCTOR_MERGE_LOCAL_HOSTS` entry that is not a
bare host name, or is over 255 characters, and a `CONDUCTOR_WT_ROOT` over 4096
characters are examples; `CONDUCTOR_MERGE_TEST_CMD` and `CONDUCTOR_CODEX_COMMAND`
are cut to 4096 characters instead):

| Variable | Meaning |
|---|---|
| `CONDUCTOR_WT_ROOT` | shared by the CLI and the server; the single root line resolution uses |
| `CONDUCTOR_MERGE_TEST_CMD` | default test command for the rehearsal |
| `CONDUCTOR_MERGE_TEST_TIMEOUT_SECONDS` | test timeout (clamped to 1–7200s) |
| `CONDUCTOR_CODEX_COMMAND` | Codex executable for the merge-time review |
| `CONDUCTOR_CODEX_TIMEOUT_SECONDS` | Codex review timeout (clamped to 1–3600s) |
| `CONDUCTOR_MERGE_LOCAL_HOSTS` | comma-separated Host allowlist (explicit local permission) |
| `CONDUCTOR_MERGE_TIMEOUT_SECONDS` | CLI-side wait for one merge request (default 11,100s, min 60s, max 86,400s); read by the CLI and the Super-agent shell, not by the server |

The CLI also reads `CONDUCTOR_WT_ROOT` (default `~/conductor-wt`) and
`CONDUCTOR_URL`, which is why the two roots must agree.

## Migration

Revision `139_review_merge_audit.py` follows `138` and adds
`autonomy_review_records` and `autonomy_review_audit`; it does not renumber any
existing revision. It was the single head when it landed; the current head is
`140` (`140_daemon_output_sessions.py`, which follows 139). The downgrade refuses
while a `merge_started` or `merge_merged` audit row exists: those rows are the
only durable record that a real merge landed (or was in flight), and a rollback
must not delete them silently. `MIGRATIONS.md` at the repo root lists 139 and
its downgrade guard.

## Limitations

- Merge is a single-host operation: the backend must run on the machine that
  holds the worktrees, and the CLI's root must match the server's.
- The rehearsal runs whatever test command is configured against the merged
  tree; it is not a substitute for the operator's own full-suite gate.
- Landing is a ref-only compare-and-set: `git update-ref
  refs/heads/<into> <rehearsed merge commit> <tested target>` either lands the
  byte-identical commit or changes nothing (`target_advanced`). The target
  worktree and index are not rewritten by the server; the response says
  `worktree_sync_required: true` and the caller syncs its checkout. This
  removes the filesystem rollback/race surface entirely, but means a
  concurrent checkout of the target worktree is not coordinated — it can see
  the moved ref only when it next refreshes/syncs. Until the operator syncs, the
  worktree reads as dirty and blocks the next merge into it (`target_dirty`).
- Codex's review text is discarded. A `BLOCK` returns no findings and the audit
  rows hold only the verdict, so the findings must be reproduced by running the
  review again.
- Nothing is pushed. The rehearsal's temporary directories live in the server's
  temp directory, and `git worktree add` also writes into the target repository.
  When `git worktree add` fails (a full disk, an unwritable target repository) the
  merge is refused with `test_worktree_failed`; when a temp path itself cannot be
  created (the rehearsal directory is made before that command, the import guard
  and the Codex final-answer file later) the `OSError` is not mapped and the merge
  answers `500 merge_internal_error`.
- The queue review is the operator's durable record, not the merge gate; the
  merge-time Codex run is the authoritative review of the exact commit that
  lands. Keep the target worktree clean: the route refuses to start while it is
  dirty.
