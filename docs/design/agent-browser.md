# Agent browser design

Status: Accepted design. This is a clean-room implementation; no code is
ported from the legacy projects named in `legacy-survey`.

## Goal and boundary

Provide local agents with a persistent, headless Chromium browser they can
control through a small `conductor browser` CLI. Each agent gets an isolated
profile under `~/.conductor/agent-browser/<agent>/`. The browser runs as a
managed background process and remains available between CLI calls. It is
never attached to a user's foreground browser or profile.

The implementation has no server or Conductor API dependency. Browser commands
must take the local CLI path and must not construct `APIClient`, read its token,
load dotenv files, or contact the Conductor backend. Page navigation and web
CLI reads/searches are the only network activity in scope; the manager and its
control channel are local.

## Components

```text
agent skill / CLI
       │ bounded JSON over private Unix socket
       ▼
per-agent browser owner process ── Playwright CDP ── managed headless Chromium
       │                                              │
       └── profile, PID/health metadata               └── isolated persistent profile
```

The CLI is a thin client. A per-agent owner process owns the browser lifecycle,
Playwright connection, persistent context, and reference registry. It starts
Chromium itself with a persistent user-data directory and a loopback-only CDP
endpoint, reads the endpoint created for that newly launched profile process
(for example, Chromium's `DevToolsActivePort` file), then connects Playwright
to that endpoint. It never accepts an arbitrary CDP URL or attaches to a
process it did not start. The owner binds a
Unix-domain socket in the agent's private state directory; the profile and
state directories are mode `0700`, files are mode `0600`, and the socket is
removed on clean shutdown. Startup validates ownership and refuses symlinked or
unexpected paths. Process metadata is for recovery and status only, not proof
that a process is safe to attach to; ownership is established by the live owner
process and its managed child.

Use a `BrowserDriver` Protocol for operations the service needs. The production
adapter imports Playwright lazily and implements persistent Chromium, page,
frame, navigation, DOM snapshot, and shutdown operations. Offline tests inject
a fake driver and do not launch browsers or open sockets. Playwright is already
installed in the approved Python 3.14.8 environment, and the root project
declares it. Do not install packages or browser binaries as part of this work.

## CLI and local command routing

Add `conductor browser` with these operations:

| Command | Behavior |
|---|---|
| `open` | Start the per-agent owner/browser if absent; report managed status. |
| `snapshot` | Return a compact semantic view of the current page and frames. |
| `run` | Execute a bounded JSON or YAML sequence of browser steps. |
| `import-login` | Import selected storage state for explicitly allowed origins. |
| `close` | Ask the owner to close Chromium and stop. |
| `commands` | List native browser commands and available site adapters. |
| `help` | Return the JSON Schema for a native command. |
| `site` | Run a supported read, headings, or links action through a named site adapter. |
| `read` | Select an adapter by hostname, or return a generic semantic page view. |
| `eval` | Evaluate page JavaScript only when this call includes `--enabled`; return status metadata. |
| `network` | Capture one requested navigation only when this call includes `--enabled`; return bounded metadata. |

Agent identity is required explicitly with `--agent`, then validated as one
safe path component. The command is registered in the
existing argparse CLI and added to its local-command routing; its handler may
accept and ignore the shared handler's client argument. Dispatch occurs before
any `APIClient` is constructed.
Keep browser implementation modules separate from the parser and client. Errors
printed to the terminal are classified and sanitized: do not include cookies,
local-storage values, authorization headers, page form values, or raw browser
protocol payloads.

### OpenCLI-inspired native commands and skills model

The command surface uses a native registry with typed command definitions and
JSON Schemas. `commands` exposes registered names and `help` exposes the
schema each command accepts. Site adapters match an allowlisted hostname and
provide bounded semantic operations. A generic browser read remains available
when no adapter matches. Adapters do not expose a general shell or arbitrary
selector interface.

The bundled `docs` and `catalog` adapters are samples for the reserved
`docs.example.test` and `catalog.example.test` fixture domains. Other pages use
the generic semantic reader unless a deployment adds an adapter for its own
site.

The command model takes an OpenCLI-inspired approach to discoverable commands
and per-command schemas, implemented entirely as Conductor-owned Python code.
It has no external OpenCLI executable or runtime dependency, including
`~/.local/bin/opencli`. No OpenCLI or Ego-Lite source was copied. Browser
sessions remain isolated under `~/.conductor/agent-browser/<agent>/`; the
validated `--agent` name selects that agent's persistent browser profile.

`conductor skills-hub` exposes the local skill catalog and installs the one
bundled `agent-browser` skill through allowlisted adapters for Claude Code,
Codex, Cursor, OpenCode, and the `.agents` convention. The skill source is
stored once in `skills/agent-browser/` and force-included in the wheel. Existing
destinations are reported as present and never overwritten. The hub does not
fetch skills from a remote registry.

### Page access gates and state

`read` and `site` make only the page navigation requested by the caller through
the managed browser. `eval` and network capture require an explicit per-call
`--enabled`; evaluation returns status metadata only, and network capture
returns method, origin, resource type, and status without paths, queries,
headers, or bodies. The browser does not run an ambient capture loop. Login
import accepts explicit exact-domain allowlists and supported HTTPS storage
entries. Browser state stays in the selected agent profile and is never copied
into the skill hub or shared skill directory.

The separate `conductor web` entry point uses the current agent's isolated
managed browser by default. `web search` searches in that browser and reads
each result URL through the same browser route. It fetches the search-result
HTML directly only when the browser cannot read the search page because of a
classified availability or timeout failure; it then still reads each extracted
URL through the browser first. A result-page read can fall back to direct fetch
only after a classified browser availability or timeout failure. `web read`
selects the matching site adapter when provided and otherwise uses a generic
semantic read. `--raw` and `--api` select direct response fetches explicitly.
`.json` and known raw file suffixes route directly to fetch without browser
startup; binary suffixes are returned as base64. These fallbacks do not apply
to arbitrary browser errors.

## Browser operations and reference contract

`run` accepts only a documented step schema: `goto`, `click`, `fill`, `wait`,
`snapshot`, and `extract`. Each action takes either a latest-snapshot `ref` or
an exact semantic target (`role`, `name`, and optional snapshot `frame` key),
so a flow can act after navigation without stale references. No CSS selector
interface is exposed. `extract` returns JSON-compatible values from the
selected target and an explicit field allowlist, or YAML when requested. Each
step has an index, operation, status, elapsed time, and bounded result or safe
error. The full request is parsed and validated before the first side effect:
unknown operations/fields, excess step count, invalid URLs, and invalid limits
reject the batch without execution. Apply 15-second per-step and 60-second
whole-batch timeouts, a 30-step maximum, and 32 KiB per-step / 256 KiB total
response bounds.
`run` has no `eval`, arbitrary JavaScript, shell, or unrestricted
page-function operation. The separate native `eval` command is gated on each
call and returns status metadata only.

Snapshots use bounded semantic DOM traversal within each Playwright frame and
contain concise landmarks and visible interactive elements (role, accessible
name, concise text, and a generated reference), with page URL and title. They
omit hidden nodes, scripts, styles, password fields, and all input values.
Reference keys include a document generation and stable frame identity, never a
positional frame index. The owner stores resolvers only for the most recent
snapshot; a new snapshot replaces that resolver table. Navigation,
document/frame replacement, or a removed node invalidates its reference and
fails closed with a request for a fresh snapshot. Never silently retarget a
stale reference by selector or position.

Walk same-origin and cross-origin frames through Playwright's frame API and
evaluate semantic traversal within each frame, subject to frame-count and
snapshot-size caps. Give each frame a stable frame key and reference namespace;
include frame URL and origin in the snapshot. A frame
that cannot be inspected is represented by a bounded per-frame status, without
failing the top-level snapshot. Actions identify both frame and reference, so a
reference from one frame cannot resolve in another.

## Login-state import

`import-login` is an explicit opt-in path. The caller supplies a state file and
one or more exact normalized DNS domains using `--domain` (for example,
`example.test`). Reject wildcards, schemes, ports, paths, userinfo, malformed
domains, and IP literals. A local-storage origin must be a valid HTTPS origin
whose hostname exactly equals one allowed domain; reject paths, userinfo,
unexpected ports, and any other scheme. For each cookie, strip at most one
leading dot from its domain and require exact equality with an allowed domain.
Never admit parent or subdomain matches. Reject malformed cookie and storage
entries, and reject every unsupported storage-state field, including IndexedDB.
Import into the selected agent's managed context only. Never print or log state
contents; report cookie/local-storage counts and normalized domains imported.
On failure, show a safe reason and source path only when safe, with no values.
Treat the file as sensitive input and do not copy it into the profile tree.

## Skill installation

Ship a browser skill that explains the supported CLI, operation schema,
reference refresh rules, bounds, and safe login import. Provide an installer for
Claude, Codex, Cursor, OpenCode, and `.agents` skill directories. Installation
creates missing target directories and writes only to a missing destination;
it never overwrites or edits an existing skill. Report installed, already
present, and failed destinations separately. Keep the skill source in the
repository and make the installer idempotent.

## Offline verification and rollout

Tests use fake drivers and temporary `HOME` and `TMPDIR` roots inside the
worktree. Run them with a cleared environment (`env -i` plus the minimum
deterministic `PATH`, locale, and Python variables), disable dotenv loading, and
block sockets. Tests must not use provider credentials, external network, a
running backend, a database, a real browser, or the user's home directory.
Cover local CLI dispatch without `APIClient` construction; secure directory,
socket, and owner lifecycle behavior; whole-batch validation before actions;
limits and error sanitization; reference invalidation and frame isolation;
origin/domain validation and value-free import reporting; and non-overwriting
skill installation.

The repository uses pytest with asyncio strict mode and defaults to excluding
integration, slow, and regression tests. Browser tests belong under the unit
suite and should follow the repository's existing pytest conventions. A real
Chromium smoke test can be a separate explicitly invoked local check after the
design and implementation are accepted; it is not a prerequisite for offline
unit verification.

## Main risks and decisions to preserve

- CDP is a powerful control surface. Keep it private to the managed process,
  loopback-bound, and unavailable as a user-supplied attach target.
- Browser state is sensitive. Keep profiles per agent with private permissions,
  never include storage values in snapshots, errors, or logs, and make state
  import narrowly allowlisted and opt-in.
- DOM references can become stale between calls. Bind them to document and
  frame identity, fail closed, and require a fresh snapshot.
- Large or slow pages can consume resources. Bound frames, steps, elapsed time,
  snapshot size, and extraction results at the protocol boundary.
- A process PID file alone is not a safe ownership check. Use the socket and
  owner process lifecycle, validate filesystem metadata, and only connect to the
  CDP endpoint created by that owner.
- The legacy survey informs the motivation and scope only. New modules,
  interfaces, and tests are to be designed and written independently.
