# Skills hub: native unified skill management for the agent fleet

Ports the concept of a central skills manager into Conductor as `src/skills_hub/` plus a `conductor skills` CLI, a read-only dashboard API, and a web panel. The per-agent skill-directory table is borrowed from [xingkongliang/skills-manager](https://github.com/xingkongliang/skills-manager) (MIT). No other code from that project is copied; the Conductor implementation is native.

## Paths we ship

Source repository: [xingkongliang/skills-manager](https://github.com/xingkongliang/skills-manager). Licence: MIT. The borrowed table is the built-in directory list in `src-tauri/src/core/tool_adapters.rs` (`default_tool_adapters`). Conductor ships this fleet subset, not that project's adapter code:

| Canonical id | Aliases | Directory |
| --- | --- | --- |
| `claude` | `claude-code` | `~/.claude/skills` |
| `codex` | | `~/.codex/skills` |
| `cursor-agent` | `cursor` | `~/.cursor/skills` |
| `grok` | | `~/.grok/skills` |
| `kimi` | | `~/.kimi-code/skills` |
| `mcode` | `minimax` | `~/.minimax/skills` |
| `step` | `stepfun` | `~/.stepcode/skills` |
| `zcode` | `glm`, `zhipu` | `~/.zcode/skills` |
| `deepseek` | `deepseek-harness`, `dsh` | `~/.config/conductor/deepseek/skills` |
| `opencode` | | `~/.config/opencode/skills` |
| `shared` | | `~/.agents/skills` |

`kimi` and `mcode` keep those canonical ids. Their directories match the skills-manager rows for Kimi Code (`~/.kimi-code/skills`) and MiniMax Code (`~/.minimax/skills`; that project keys the row `minimax_code`). `step` (alias `stepfun`) is `~/.stepcode/skills`: skills-manager has no StepFun row, and `~/.step` is not the directory this fleet uses. DeepSeek stays on the Conductor harness path rather than skills-manager's `~/.dsh/skills`. `shared` is our id for the shared `~/.agents/skills` root. Unknown agent ids are an error, never a guessed path.

Agent directory overrides still come from `~/.config/conductor/config.json`. When `~/.conductor/skills-hub.toml` exists (`TOML_CONFIG_FILENAME` in `src/skills_hub/config.py`), its `[agents]` table overrides only the ids it sets; the JSON file still loads, and the same id in both files takes the TOML path. Relative overrides must resolve inside the home. Unknown TOML keys are ignored. Absolute paths stay allowed.

## What was built, from where

- `src/skills_hub/core.py` — central repo model: `~/.conductor/skills` (default, configurable via `CONDUCTOR_SKILLS_REPO` or `~/.config/conductor/config.json`), skill discovery, `SKILL.md` front-matter parsing, and the tracked `manifest.json` (which agent got which skill, where it was imported from). Agent path overrides in that JSON file are overlaid by `~/.conductor/skills-hub.toml` (`[agents]` only) when the TOML file exists.
- `src/skills_hub/adapters.py` — per-agent skill-directory table: claude `~/.claude/skills`, codex `~/.codex/skills`, cursor-agent `~/.cursor/skills`, grok `~/.grok/skills`, kimi `~/.kimi-code/skills`, mcode/minimax `~/.minimax/skills`, step/stepfun `~/.stepcode/skills`, zcode `~/.zcode/skills`, deepseek harness `~/.config/conductor/deepseek/skills`, opencode `~/.config/opencode/skills`, shared `~/.agents/skills`. Unknown agent ids are an error, never a guessed path. See [Paths we ship](#paths-we-ship) for the MIT path-table attribution.
- `src/skills_hub/importer.py` — import from git URL (`git clone --depth 1`, the only subprocess/network path), local folder, or zip (traversal-rejecting, size-capped). Bundle or single-skill layouts both detected.
- `src/skills_hub/manager.py` — install/uninstall/sync in symlink (relative) or copy mode. Uninstall only removes entries the manifest attributes to Conductor; installs refuse directories inside a git worktree; every mutating pass snapshots first.
- `src/skills_hub/snapshots.py` — pre-change snapshots of the agent directories (entry names, symlink targets) with `conductor skills rollback [id]`. Rollback recreates only what a later change removed; it never deletes, and reports the entries it did not record as `kept`.
- `src/skills_hub/presets.py` — named presets (`presets/<name>.json`) and per-project/per-line skill sets with plan/dev/review role filtering.
- `src/skills_hub/seed.py` — idempotent `fleet-rules` skill (no self-review, never print/copy secrets, worktree-only edits).
- `src/conductor_cli/skills_cli.py` — `conductor skills list|import|install|uninstall|preset|project|sync|rollback|seed-fleet-rules`, all local (no server round-trip).
- `src/server/app/fleet_skills.py` — read-only `GET /api/v1/fleet-skills` (repo contents, per-agent entries with managed/foreign/broken markers, presets, projects) for the agent-fleet dashboard; no mutating verbs; behind the same `CurrentUserId` dependency as the other dashboard routes.
- `web/src/api/fleetSkills.ts`, `web/src/features/superagent/FleetSkillsPanel.tsx`, `AgentsPage.tsx` — collapsible, lazily loaded read-only fleet-skills panel.
- `pyproject.toml` — `src/skills_hub` added to the wheel packages.

## rr2 review fixes (2026-11-03, second BLOCK → addressed)

The grok rr2 re-review of the range found the first round fixed but rollback still destroyed data it did not record. Each finding below is fixed with regression tests:

- **rollback never destroys data it did not record (P1).** `snapshots._restore_agent` cleared every recorded entry and re-materialized it from the central repo (or a tombstone placeholder), so a hand-installed skill still on disk lost its only copy — the reviewer's repro turned `handmade/SKILL.md` (`UNIQUE-USER-CONTENT-EDITED`) into the placeholder and reported `restored: {}` because a dir entry still compared equal before/after. Restore now only recreates what is **missing**: an entry still on disk belongs to the user and is left untouched (the one lossless repair: a symlink whose target drifted gets re-pointed). Rolling back a snapshot of the current tree is a strict no-op, as is a second rollback.
- **plain files are part of the snapshot state (P1).** `_scan` recorded only symlinks and directories, so rollback deleted any regular file in an agent skill directory (reviewer repro: `loose.txt` removed). Files are now recorded (`kind: "file"`, base64 content capped at 64 KiB, oversized/unreadable files recorded as content-less so they are preserved but not fabricated), restored byte-for-byte when missing, and validated with a traversal-only name check since real file names carry dots the skill-name pattern forbids. Non-regular children (fifo/socket/device) are no longer deleted either.
- **relative agent overrides cannot escape HOME (P2).** `config._resolve_agent_dirs` joined a relative `agents.<id>` onto the home without normalizing, so a `..` component pointed uninstall/prune/rollback outside it — and `allowed_dirs` compared the same un-normalized value, so it would not catch the escape. Relative overrides are now normalized and refused when they resolve outside the home; absolute overrides are normalized too.
- **the inventory no longer names paths or sources (P2).** `GET /api/v1/fleet-skills` returned each repo skill's absolute `path` and import `source`/`source_ref`; a git URL can embed a token, visible to every authenticated user on a shared host. The response now carries only `name`/`description`/`installed` per repo skill (`fleet_skills._public_skill`); the TS `FleetRepoSkill` type and the panel match.
- **the lazy fleet panel cannot take down the Agents page (P2).** `AgentsPage` loaded `FleetSkillsPanel` through `React.lazy` + `<Suspense>` with no error boundary, so a failed chunk load (deploy deleted it) reached the route-level `StaleBuildBoundary` and replaced the whole page. A local `FleetSkillsPanelBoundary` now absorbs any panel render/chunk failure into an inline alert with a retry, and logs it.

## Review fixes (2026-11-03, verdict BLOCK → addressed)

All six P1 and both P2 findings from the grok code review of `7ffc9b54e..HEAD` were fixed, each with regression tests:

- **sync no longer deletes.** `manager.sync` was unlinking/`rmtree`ing every manifest-tracked agent skill missing from the repo and dropping the manifest row. Sync is now additive by default: stale entries are reported in `ChangeSet.stale` and left on disk and in the manifest; removal is explicit via `sync(prune=True)` / `conductor skills sync --prune` or named `uninstall`.
- **`import --force` is atomic.** The old code deleted `repo/<name>` before the size check and copy; a failed import left the skill gone. The replacement is now budget-checked and fully staged inside the repo (`importer._stage_in_repo`/`_replace_target`), then swapped by rename with the previous content kept recoverable until the new copy is in place.
- **rollback validates its targets.** `snapshots.rollback_snapshot` refuses a snapshot whose `dir` is missing (a missing `dir` used to fall back to the process cwd before deleting children) or relative, validates every directory before deleting anything, and accepts `allowed_dirs` (the CLI passes its configured agent directories) so a crafted snapshot JSON cannot point the restore at an unrelated folder. `create_snapshot` requires absolute directories at write time.
- **hub directory names are reserved.** `snapshots`, `presets` and `projects` are rejected as skill names (`core.RESERVED_NAMES`), closing the path where an imported `snapshots` skill plants fake snapshot JSON that a default rollback would follow.
- **symlinks are never followed.** Import staging, copy-mode install/sync, and snapshot restores all copy with `symlinks=True` (links preserved as links), and the budget walk uses `os.walk(followlinks=False)`: a link pointing at a huge tree outside the skill can no longer be pulled into the repo or past the 64 MiB cap, and symlink loops cannot hang or inflate the copy.
- **relative config overrides anchor at HOME.** `load_config()` without an explicit `home` (the CLI and the API) left relative `agents.<id>` overrides relative, so uninstall/sync resolved them against the process cwd; they now resolve against the effective home (`Path.home()` when no home is passed).
- **the dashboard endpoint authenticates.** `GET /api/v1/fleet-skills` now takes `CurrentUserId`, matching the other dashboard routes (anonymous platform-mode callers get the 401 instead of the repo path and per-agent directories).
- **inventory and panel robustness.** `list_installed` reports a per-agent `error` (unreadable directory, symlink loop) instead of failing the whole inventory; the panel surfaces that field and renders a FastAPI string `detail` from `ApiError.response.data` instead of the transport's "Request failed with status code N" (`fleetSkillsErrorMessage`).

## rr8 review fixes (2026-11-03, MERGE with notes → addressed)

Two notes from the rr8 review of `7ffc9b54e..HEAD`, both fixed with regression tests:

- **import errors never quote a credential.** git echoes the remote it was given back on an authentication failure, so `git clone` stderr carried `https://deploy:hunter2@host/…` verbatim into the `SkillsError` the CLI prints; the local/zip branches echoed the caller's source string the same way. `importer.redact` now scrubs every string the module puts in a message — a URL's whole userinfo (a username can *be* the token), secret-shaped query parameters, bearer values and token-shaped words (`ghp_…`, `glpat-…`, `xox…`, `AKIA…`, `sk-…`, `AIza…`) — keeping host and path so the refusal is still diagnosable. The shapes mirror `server/utils/error_sanitization.py`; they are duplicated rather than imported because the CLI path must not pull the server's dependencies in. (The manifest still records the source verbatim, since `sync` re-imports from it — that is the user's own file on their own machine, and the dashboard API has never returned it since rr2.)
- **rollback reports user-added skills instead of deleting them.** `_restore_agent` cleared every child the snapshot did not record, so a skill installed by hand (or by a later `install`) after the snapshot was deleted by a rollback — the same data-loss class rr2 fixed one layer deeper. The deletion loop is gone: rollback recreates only what a later change removed, leaves everything else alone, and returns it under `kept` next to `restored`. Removal stays with `sync --prune` and `uninstall`, the verbs that exist to delete.

## Tests

- rr2 round (this fix): `pytest tests/unit/skills_hub/ tests/unit/server/app/test_fleet_skills.py tests/unit/server/app/test_no_trading_capability.py tests/unit/server/app/test_skills_limits.py tests/unit/server/app/test_skill_plugin_owned.py` — 96 passed (9 new regression tests: rollback idempotency, handmade-dir preservation, loose-file preservation/restore, symlink re-point, kind-change preservation, `..`-override rejection, dot-component normalization, inventory redaction); `pnpm vitest run src/features/superagent/__tests__/{FleetSkillsPanel,AgentsPage.mainAgent,AgentsPage.fleetSkillsChunkError,daemonRouting}.test.tsx` — 17 passed (3 new: boundary absorb/retry, chunk-failure page survival); `pnpm typecheck` and ESLint on the touched files clean.
- Backend (repo venv): `tests/unit/skills_hub/` + `tests/unit/server/app/test_fleet_skills.py` + `tests/unit/test_conductor_cli.py` — 107 passed (18 new regression tests); plus `tests/unit/conductor_cli/` and `tests/unit/server/app/test_no_trading_capability.py` (full-app route table) — 16 passed. Full `tests/unit` suite green. All tests use a tmp HOME; no real skill directories were read or written.
- Frontend: `pnpm vitest run src/features/superagent/__tests__/` — 22 passed (4 FleetSkillsPanel tests, incl. an ApiError-with-string-detail rejection and a per-agent read error); `pnpm typecheck` clean; `pnpm lint` 0 errors.
- Ruff clean on all new/changed modules; `git diff --check` clean.
- Smoke: `python -m src.conductor_cli.cli skills seed-fleet-rules → list → install --agent zcode --agent claude → uninstall --agent zcode` in a tmp HOME produced relative symlinks and a tracked, agent-scoped uninstall.

No DB migration (file-backed only). No GitHub cloud sync or login. No legacy sources were modified.
