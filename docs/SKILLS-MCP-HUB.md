# Skills & MCP Hub

本机用一套中央库管理各 Agent CLI 的 skills 和 MCP，而不是在每个 CLI 的目录里各改一份。Skills 放在 `~/.conductor/skills`，MCP 登记在 `~/.conductor/mcp/servers.toml`，再由各自的渲染器写回该 CLI 的原生配置；只改本 hub 管理的条目。写操作默认是 dry-run，必须显式加 `--apply` 才会落盘，并且先做快照。在评审通过之前，不要对真实 HOME 执行 `--apply`，这一步由所有者本人来做。Worker 只接收预设声明的 skills 和 MCP，而且本包不会把操作者自己的 `AGENTS.md` / `CLAUDE.md` 送进 Worker（那部分隔离是 Task 2，不在这里实现）。

This note is the operator guide for the in-house hub. The agent path table follows the layout used by [skills-manager](https://github.com/xingkongliang/skills-manager) (MIT); the code in this tree is Conductor's, not a copy of that app. Licence detail for the port lives in `docs/ports/skills-hub.md`. Nothing below is a copy of a real config, and no secret value belongs in any of these files.

Checked against this worktree. A command or function is marked **implemented in this branch** only when that function is in the tree. Everything else is still only the spec in the internal planning note `skills-mcp-hub` (not published).

## 1. Concepts

Three stores, one writer.

**Central skills repo.** Default `~/.conductor/skills`. Override the repo with `CONDUCTOR_SKILLS_REPO` (absolute path) or the `repo` key in `~/.config/conductor/config.json`. The repo holds one directory per skill (`SKILL.md` plus the skill's files), a manifest of what Conductor installed where, named presets, per-project sets, and snapshots. Names `snapshots`, `presets`, and `projects` are reserved so a skill cannot plant a fake store inside the repo. **Implemented in this branch** (`default_repo`, `discover_skills`, `RESERVED_NAMES`).

**MCP registry.** `~/.conductor/mcp/servers.toml`. One entry per server: name, transport (`stdio`, `http`, or `sse`), command, args, url, env, target agents (`all` or a list), per-agent overrides, and enabled. Env values are variable names or `keychain:<service>/<account>` references. Never a plaintext secret. **Implemented in this branch** (`src/mcp_hub/store.py`, mode `0600`).

**Managed sidecar.** A renderer writes only the entries this hub owns. Ownership is `managed_by = "conductor"` on the entry, or a sidecar list next to the native file. Anything the operator added by hand stays byte-for-byte, including the rest of Claude's `~/.claude.json` outside the `mcpServers` key. **Implemented in this branch** (`src/mcp_hub/editing.py`, `src/mcp_hub/store.py`).

**Per-agent renderers.** One adapter per CLI reads and writes that CLI's native file (JSON or TOML). The skills side and the MCP side are different files; see the path table below. Skills installs are **implemented in this branch** (`AgentDirs`, `install`). MCP renderers are **implemented in this branch** (`src/mcp_hub/render.py`). MiniMax is JSON at `~/.minimax/mcp.json`. The hub does not write `config.yaml`.

**What already runs today.** `conductor skills` is local (no server round-trip). **Implemented in this branch** (`register_parser` in `src/conductor_cli/skills_cli.py`):

| Command | What it does |
|---|---|
| `list` | Skills in the central repo. `--installed` adds per-agent entries. `--agent` limits the set. |
| `import <git-url\|dir\|zip>` | Copy skills into the central repo. `--ref` for a git branch or tag. `--force` replaces the same name. This is not `adopt`. |
| `install <skill...>` | Materialize into agent directories. `preset:<name>` expands a preset. `--agent` repeatable. `--mode symlink\|copy`. |
| `uninstall [skill...]` | Remove only entries the manifest says Conductor installed. |
| `preset list\|show\|save\|delete` | Named bundles stored as JSON under the repo. |
| `project list\|show\|set` | Per-project, per-line sets. `--line`, `--role plan\|dev\|review`. |
| `sync` | Reconcile agent dirs with the repo. Stale entries are reported, not deleted, unless `--prune`. |
| `rollback [snapshot-id]` | Restore the newest snapshot, or the id you pass. |
| `seed-fleet-rules` | Ensure the built-in `fleet-rules` skill exists. `--force` overwrites it. |
| `adopt` | Plan a content-hash adopt. `--apply` snapshots, then writes. |
| `status` | Per-agent installed, link-or-copy, hand-installed, conflict, and drift. |
| `check` | Compare an installed skill with its recorded upstream. |
| `update` | Fast-forward one skill that records an upstream. |
| `backup --remote <git-url>` | Push the central library. Skips the SQLite index, secrets, and files over 100 MB. |

`--json` is the CLI envelope (`src/conductor_cli/output.py`, `API_VERSION=v1`). Exit codes are 0 ok, 2 usage, 1 runtime.

## 2. First use

Do not point any of this at the real HOME until the warning in section 8 is lifted. Use a temporary `HOME` while reviewing.

**Skills: gather what is already installed.**

```bash
conductor skills adopt
conductor skills adopt --apply
```

`adopt` scans every agent skill directory, copies skills into `~/.conductor/skills`, and de-duplicates by content hash. Two copies with the same name and different bytes are both kept. With `--apply` it snapshots first, then replaces the originals with a symlink (or a copy when the link mode says so). With no `--apply` it only prints the plan. **Implemented in this branch** (`src/skills_hub/adopt.py`). It is a different command from `conductor skills import`, which pulls a git URL, folder, or zip you name.

`conductor skills check` and `conductor skills update` track an upstream git URL and commit. `conductor skills status --json` reports per-agent installed / link-or-copy / hand-installed / conflict / drift. `conductor skills backup --remote <git-url>` backs up the central library and skips the SQLite index, secrets, and files over 100 MB. **Implemented in this branch** (`status.py`, `updates.py`).

**MCP: gather, preview, write, undo.**

```bash
conductor mcp import
conductor mcp diff
conductor mcp apply --apply
conductor mcp rollback
```

**Implemented in this branch** (`src/conductor_cli/mcp_cli.py`, `src/mcp_hub/`): `list`, `status`, `import`, `add`, `remove`, `enable`, `disable`, `apply`, `diff`, `rollback`, and `check <name>`.

- `import` reads the MCP entries already scattered across agent configs, de-duplicates them, and stages them into `~/.conductor/mcp/servers.toml`.
- `diff` prints the per-agent change without writing.
- `apply` snapshots first, then writes. Passing `--apply` is what allows the write. Without it the command stays a dry run, same as the other MCP writes.
- `rollback` restores the snapshot from before that apply.

`remove` and `disable` write the registry only.
`apply` never deletes or disables a server key in Claude JSON, Codex TOML, or any other agent config.
A key the registry no longer wants is reported as `orphaned`.
Delete that agent-config entry by hand.

Every MCP command is specified to accept `--json`, a uniform error shape, and stable exit codes. Default for any command that would write an agent config or the registry is dry-run. `--apply` is the write switch.

`conductor mcp check <name>` reports status `not-run` unless a runner is supplied to `check_server`. It never prints env values. The library runner, when one is passed in, may start a stdio server or ping an http one, then report tool count and elapsed time.

## 3. Presets and worker delivery

A preset names the skills and MCP servers one project or one dispatch line needs.

**Skills presets that exist now.** **Implemented in this branch** (`save_preset`, `effective_skills` in `src/skills_hub/presets.py`). `conductor skills preset save <name> <skill...>` writes `presets/<name>.json` inside the central repo. The skills must already be in the repo. `conductor skills project set <project> <skill...> --line <line> --role plan|dev|review` writes `projects/<project>.json`. A role filter keeps skills that list that role or that list no role. `conductor skills install preset:<name>` expands the bundle into agent directories on the machine you point `HOME` at.

**Worker preset file.** Spec: `presets/<workspace>.toml` declaring the skills and the MCP servers for that workspace or line. That TOML shape is not implemented in this branch. Do not confuse it with the JSON presets above.

**`plan_delivery`.** **Implemented in this branch** (`src/skills_hub/delivery.py`). Codex's daemon calls `plan_delivery(preset, isolated_home)` when a Ticket or a dispatch line starts. This package does not wire the daemon. The function returns actions whose destinations stay under `isolated_home`. It does not write, and it does not call `Path.home()`. A skill name or a symlink chain that leaves `allowed_roots` raises `DeliveryError`. Secrets in an MCP document must be `${VAR}` or `keychain:service/account`.

**Instruction files.** Workers do not receive the operator's `~/.codex/AGENTS.md` or `~/.claude/CLAUDE.md` from this package. That isolation is Task 2. This hub must not implement it a second time. Only the delivered preset applies inside the worker.

**Audit.** Spec: each Ticket records which skill and MCP versions it received. Not implemented in this branch.

## 4. Agent path table

Canonical ids stay `kimi`, `mcode`, and `step`. The directories are the ones those CLIs actually read. Unknown ids are an error; the table does not guess. **Implemented in this branch** (`_agent_dirs` in `src/skills_hub/adapters.py`).

| Agent id | Aliases | Skill directory | MCP config (spec) |
|---|---|---|---|
| `claude` | `claude-code` | `~/.claude/skills` | `~/.claude.json` key `mcpServers` (JSON). Touch only that key. |
| `codex` | | `~/.codex/skills` | `~/.codex/config.toml` tables `[mcp_servers.*]` |
| `cursor-agent` | `cursor` | `~/.cursor/skills` | `~/.cursor/mcp.json` |
| `grok` | | `~/.grok/skills` | `~/.grok/config.toml` |
| `kimi` | | `~/.kimi-code/skills` | `~/.kimi-code/mcp.json` |
| `mcode` | `minimax` | `~/.minimax/skills` | `~/.minimax/mcp.json` key `mcpServers`. `config.yaml` is left byte-identical. |
| `step` | `stepfun` | `~/.stepcode/skills` | `~/.stepcode/config.toml`. The MCP agent id is `stepfun`. |
| `zcode` | `glm`, `zhipu` | `~/.zcode/skills` | `~/.zcode/cli/config.json` key `mcp.servers` |
| `deepseek` | `deepseek-harness`, `dsh` | `~/.dsh/skills` (`$DSH_HOME/skills`; dsh also scans `~/.agents/skills`) | `~/.dsh/cordis.patch.yml`: a top-level YAML array of patch ops; servers are `@deepseek-ai/dsh-mcp-client` entries under `insert`, keyed by `config.serverName`. Never `.credentials.yaml` or `AGENTS.md`. |
| `opencode` | | `~/.config/opencode/skills` | `~/.config/opencode/opencode.json` key `mcp` |
| `gemini` | | `~/.gemini/skills` | `~/.gemini/settings.json` key `mcpServers` (off switch is `mcp.excluded`, so a disabled server is deleted) |
| `antigravity` | | `~/.gemini/antigravity/skills` | `~/.gemini/config/mcp_config.json` key `mcpServers` (`url`, legacy `serverUrl`; `disabled: true`) |
| `qwen` | | `~/.qwen/skills` | `~/.qwen/settings.json` key `mcpServers` (`httpUrl` = http, `url` = sse) |
| — | | — | MCP id `claude-desktop`: `~/Library/Application Support/Claude/claude_desktop_config.json` key `mcpServers`, stdio only; `${VAR}` env is not expanded there, so it is refused unless `--allow-inline-secret` |
| `shared` | | `~/.agents/skills` | MCP id `agents`: `~/.agents/mcp.json` key `mcpServers`, stdio only |

The corrected rows are `kimi` → `.kimi-code`, `mcode` / `minimax` → `.minimax`, `step` / `stepfun` → `.stepcode`, and `deepseek` → `.dsh/skills`. The old `.kimi`, `.mcode`, `.step` and `.config/conductor/deepseek` directories are kept as **legacy detection only**: `skills drift` reports one that still exists, nothing writes there. Instruction targets: mcode reads `$MINIMAX_DATA_DIR/AGENTS.md` (`~/.minimax/AGENTS.md`), step reads `$STEP_CODING_AGENT_DIR/AGENTS.md` (`~/.stepcode/agent/AGENTS.md`); `instructions status` lists an old `.mcode`/`.step` file under `legacy`.

**Rehearsal and drift.** Every `conductor mcp …` and `conductor skills …` subcommand takes `--home <dir>` (default: the real HOME), so a whole import → diff → apply → rollback can be run on a copy first. With an explicit `--home`, `~` in the skills config means that home, configured paths that leave it are refused, and the `CONDUCTOR_SKILLS_*` env overrides are ignored. Writers preview unless `--apply`: `mcp import/add/remove/enable/disable/apply/rollback`, `skills install/uninstall/sync/rollback/adopt`. `conductor mcp drift` and `conductor skills drift` are read-only: per agent, catalog entries `missing` / `different` (field names only) / `extra` (with ownership), `unrepresentable`, `fragile` commands (paths under a `draft-*` workspace, `/tmp`, `/var/folders`, the Trash) and `secrets` (key paths of literal secret-looking values — never the value).

Skill directories above are **implemented in this branch**. The MCP column is **implemented in this branch** (`src/mcp_hub/agents.py`). Cursor's MCP id is `cursor`. Skills keep the id `cursor-agent` with alias `cursor`. Claude writes only the `mcpServers` key; sibling keys stay byte-stable. Codex uses `[mcp_servers.NAME]` and `[mcp_servers.NAME.env]`. Grok TOML entries carry `enabled`.

**Overrides.** **Implemented in this branch** (`toml_config_path`, `_read_toml_agents`, `load_config`).

Resolution order for one invocation:

1. Built-in table above.
2. `~/.config/conductor/config.json` — `repo`, `default_link_mode` (`symlink` or `copy`), `extra_agent_dirs`, and an `agents` object of id → directory. `CONDUCTOR_SKILLS_CONFIG` replaces this JSON path (absolute). `CONDUCTOR_SKILLS_REPO` replaces `repo`.
3. `~/.conductor/skills-hub.toml`, optional. Only the `[agents]` table is read. An id set there wins over the same id in the JSON file. Every other JSON setting still applies. Any other TOML key is ignored. A missing file is an empty overlay, not an error. A custom id in `[agents]` adds an agent the built-in table does not know.

Relative override paths are anchored at HOME, normalized, and refused when they leave HOME (`..` is an error). Absolute override paths are kept as given. At most 32 agent overrides and 16 extra directories. Either config file over 1 MiB is refused. Invalid JSON or TOML is an error with a message safe to print.

`default_link_mode` selects symlink or copy for `install` and `sync`. `adopt --apply` uses the same setting when it replaces originals. Symlinks are relative where the existing installer writes them.

## 5. Rollback and atomic write

**Skills snapshots. Implemented in this branch** (`create_snapshot`, `rollback_snapshot`, `atomic_write_json`).

Every mutating skills pass (`install`, `uninstall`, `sync`, and a forced import that replaces a tree) snapshots the agent directories it is about to touch, then changes them. `conductor skills rollback` with no id uses the newest snapshot. The CLI only allows a restore into directories this config knows as agent skill dirs.

A snapshot records entry names and whether each was a symlink (and its target), a directory, or a plain file. Directory bodies are not copied. A missing copy-mode skill comes back from the central repo; if the repo skill is gone, the slot comes back as a small tombstone, not as stale content. Plain files up to 64 KiB are stored and restored; a larger file is remembered so rollback will not invent bytes for it.

Rollback does not delete. It only recreates what is missing, and it re-points a symlink whose target drifted. An entry still on disk is left alone. An entry the snapshot never recorded (a skill installed by hand, or by a later install) is left alone and reported as `kept`. Rolling back a snapshot of the current tree is a no-op. A second rollback is a no-op. Deleting a tracked skill is `uninstall` or `sync --prune`, not rollback. At most 50 snapshots are kept.

Snapshot JSON is validated before any restore: the id cannot contain a path separator, every recorded directory must be absolute, and it must sit in the caller's allowed set. A missing directory does not fall back to the process working directory.

**Atomic writes that exist now. Implemented in this branch.**

- JSON the hub owns (`manifest.json`, presets, projects, snapshot files) goes through `atomic_write_json`: a temp file in the same directory, flush, `fsync`, then `os.replace`.
- Replacing a skill tree stages the copy inside the repo (so the rename stays on one filesystem), then `os.replace`. The previous tree is moved aside first and put back if the new name does not land, so the target is never an empty gap.

**MCP apply. Implemented in this branch** (`src/mcp_hub/apply.py`, `atomic.py`). `apply --apply` snapshots first, writes a temp file, then renames it into place, and keeps the existing file mode. `conductor mcp rollback` restores those exact bytes. A crash mid-write leaves the previous complete file. Only hub-managed entries change. `diff` writes nothing.

A real apply appends a journal event before each target: the target path, the pre-hash, the intended post-hash, and state `intent`. Each event is a new file. The write is atomic and the directory is fsynced. An existing journal file is never rewritten. After the file lands, a new event records `published`. After the ownership sidecar is saved, a new event records `registered`. The journal is the only fact. The sidecar is a cache: every command replays the journal and rebuilds it. Apply, remove, and rollback decide from that in-memory replay and do not read `managed.toml`. If the sidecar write fails, the decision already made is unchanged; the next command rebuilds the cache. `import_existing` appends an `imported` event whose `owned` list is the full claim for that agent, so a later replay keeps those names. The file stays published, the result counts the target as a success, and a warning says `published_unregistered`. Earlier targets are not rolled back. `conductor mcp recover` is that replay. An `intent` or `published` event whose current bytes equal the post-hash, and which is still the latest valid apply for that agent, is appended as `registered`. Any other `intent` is appended as `abandoned`. For each agent, the latest valid apply wins even when the file bytes match an older apply. Those older bytes stay the user's and are not claimed. Recover does not register `rolled_back` or `abandoned`.

Manual rollback restores a target only when that snapshot is still the latest valid apply for the agent. An older snapshot reports `superseded_by_later_apply` until the newer one has been rolled back, whether or not the hashes match. The restore compare-and-swaps against the post-hash: a change between the check and the replace, or the delete, leaves the user's bytes alone and reports `modified_since_apply`. The file is restored first. A new journal event then records `rolled_back` with `rollback_of` set to that snapshot id. If that append fails, the next replay sees the file is already the pre-image and appends the missing event, so rollback can be retried. Bytes that equal an older apply's post-hash are that older apply, not this fill-in. Roll snapshots back newest first.

`conductor skills update --force` on an imported copy snapshots the whole skill tree before every replace that will happen, including a change that is only permission bits or only an empty directory. That snapshot keeps content, the type of every entry, hidden files and directories, symlinks as links, the permission bits of the skill root and every subdirectory, empty directories, and files larger than 64 KiB. If a file cannot be read, or the tree is over 200 MB, the update refuses before it replaces anything and says why. `conductor skills rollback` writes content and entry types back first, with directories temporarily writable, and restores directory permissions last, deepest first, including the root. A symlink whose text stays inside the skill root can be replaced by the snapshotted directory. A symlink that points outside is refused and is not followed. Replacing a read-only directory grants `u+w` on that directory and its parent directories for the removal, then puts the recorded modes back deepest first. Agent-directory snapshots stay content-light and still cap loose files at 64 KiB. A skill restore opens each path component with `O_NOFOLLOW`, so a missing parent is not created through a symlink that points outside the skill.

## 6. Security

Dry-run is the default for `adopt` and for every MCP command that writes. Nothing reaches an agent directory or a native MCP file until `--apply`.

**Secrets.** The registry stores env names or `keychain:<service>/<account>` only. Renderers write `${VAR}` into the agent config. Plaintext env values are refused. `render_agent` accepts `allow_inline_secret` for a named server; the CLI does not expose that switch, and `conductor mcp add --env KEY=value` exits 2 without storing or printing the value. The registry file is mode `600`. `conductor mcp check` does not print env values. Do not log, print, or commit secret values. Import errors that would echo a git URL go through `redact` (**implemented in this branch**, `src/skills_hub/importer.py`): userinfo, secret-shaped query parameters, bearer values, and token-shaped words are masked; host and path stay so the failure is still diagnosable.

**Already enforced on the skills path. Implemented in this branch.**

- Relative agent overrides that resolve outside HOME are refused.
- Skill copies use `symlinks=True`, so a link is copied as a link and is not followed into another tree.
- Snapshot rollback refuses a directory outside the configured agent dirs.
- Uninstall removes only manifest-tracked entries.
- Install into a skill directory that lives inside a git worktree is refused.

**Red team.** `tests/unit/skills_hub/test_redteam_malicious.py` and `tests/unit/mcp_hub/test_redteam.py` cover:

- Path escape: skill names, MCP names, and override paths that try to leave the repo, the agent dir, or the worker HOME (`..`, absolute paths, prefix tricks).
- Symlink: a skill or a config path that is a link to somewhere else, including a link planted in an agent directory before adopt or apply.
- Injection: a skill name or an MCP name that breaks TOML, JSON, or YAML structure, or that collides with `managed_by` / the sidecar so a hand-written entry gets overwritten.
- Torn write: a second writer during apply, or a kill between temp-file create and rename. The native file must stay the old complete contents or the new complete contents.
- Worker delivery: `plan_delivery` refuses a destination outside the isolated HOME, including a symlink chain that leaves `allowed_roots`. It does not open the real HOME for write.

When skills-manager is installed, the spec is to read its library through its `--json` CLI and show it as a read-only source. Never write into its library. Not implemented in this branch.

## 7. Web page and TUI

**Web.** **Implemented in this branch** (`web/src/features/skills-mcp/`). Route `/skills-mcp`, title Skills & MCP. Two matrices: agent × skill, and agent × MCP. Cells are installed, missing, drifted, or conflict. Actions call `POST /api/v1/skills-mcp/adopt|apply|rollback`. `loadSkillsMcp` tries `GET /api/v1/skills-mcp/status`. A 404 shows the CLI as the source of truth, because this branch does not add server routes. Secret-like fields render as `••••`. The older read-only fleet panel (`GET /api/v1/fleet-skills`) is a different surface.

**TUI.** `/skills` and `/mcp` are on branch `feat/tui-redesign`, not in this worktree. They run `conductor skills status --json` and `conductor mcp status --json` as a subprocess (`shell=False`). They do not import `skills_hub`. Glyphs: `✔` in sync, `◆` needs you, `✖` failed.

The page and the CLI `status --json` commands are the source of truth for this branch. The TUI reads that JSON.

## 8. Do not apply this to the real HOME yet

Do not run `--apply` against the real HOME until review has passed. That includes `conductor skills adopt --apply`, `conductor skills install`, `conductor skills sync`, `conductor mcp import` once it writes, and `conductor mcp apply --apply`.

The owner runs that himself, after review. Until then, exercise the commands with a temporary `HOME` so `~/.claude.json`, `~/.codex/config.toml`, `~/.kimi-code`, `~/.minimax`, `~/.stepcode`, and the other agent directories on the machine stay untouched. A dry run with no `--apply` is the only mode that is safe to point at the real home before that review, and only if the implementation under review actually honours dry-run. Adopt and MCP apply are in this branch; they write when `--apply` is set. `plan_delivery` does not write — it only returns actions. The executor (`apply_delivery`) is the write boundary. Point writes only at a temporary `HOME`.
