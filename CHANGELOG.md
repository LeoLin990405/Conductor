# Changelog

## Unreleased (since v1, 2026-11-08 to 11-10, PRs #22–#73)

- **Round 9a (finance residue and attribution):** the backend drops the `finance` capability flag, the comments that were all that remained of the old finance 404 fences, the trading-tool catalog shims, the Financial tool tier and finance prompt examples, and `stock_code` from persisted run metadata. The web-search tools no longer offer Tavily's `topic="finance"` or Exa's `"financial report"` category. `market_view` is kept only as a legacy alias for steer delivery, so a client that still sends it keeps getting its missed steers as the next turn; the prompt has no surface rules for it. Nothing reads or writes `user_preferences.risk_preference` any more (dropping the column waits for the owner's OK, backlog N65), and tests use neutral example data. Finance-vertical statements and the upstream brand wording are removed from `README.md`, `README.zh-CN.md` and `docs/`; the README license sections name only `LICENSE` and `FORK-CN.md`. `FORK-CN.md` is a short attribution file: Conductor is an independent project, LangAlpha is named only for the Apache-2.0-derived code that remains, that inherited runtime is being retired in rounds 8 and 9, and the other credits (the DeepSeek Harness design, `docs/ports/` provenance, `contrib/mmteam/NOTICE`) are listed there. Finance examples left in plugin skills are backlog N66. `LICENSE` is unchanged.
- **README and plans (#73):** README (en and zh-CN) rewritten against the current code; next-iteration backlog N1–N33 and related PRDs landed on main.
- **GLM serial launches (#72):** the dispatch launcher runs serial providers (default `glm`) one Worker at a time, because the GLM Coding Plan rate-limits concurrent requests (error 1302).
- **Worker credentials (#71):** dispatch Workers receive allow-listed provider credentials from `CONDUCTOR_HOME/secrets.env` and provider env files.
- **Self-iteration round 1 (#70):** four fixes planned, implemented and cross-reviewed by Conductor itself (failed-Ticket subscription cleanup and related follow-ups).
- **Planner and reviewer contracts (#66–#68):** robust planner JSON extraction and retries, schema-enforced plan JSON from Claude Code, and an explicit JSON reply contract for cross-reviewers.
- **CodexBar windows (#65):** Codex Extra usage credits and CodexBar window roles are read correctly so exhausted third-party buckets no longer hide remaining primary headroom.
- **Readiness heartbeats (#64):** `/ready` trusts the server daemon heartbeat, not only cond's start-up file.
- **GitHub Source (#54, #60, #62):** optional `trigger_phrase` lets a Source run on the operator's own token; `require_approval` survives a PATCH that omits it; end-to-end poll coverage for the phrase.
- **OpenCode maintenance (#50, #61):** OpenCode joins the native worker roster with an isolated config directory; later import cleanup only.
- **Session handoff (#52):** before a proactive session rotation, Kimi and MiniMax are asked for one bounded self-report, which is added to the handover note. Other providers, and any timeout or bad answer, fall back to the deterministic note. A line that is asking blocks sibling launches, and placeholder session ids never reach the archive.
- **Per-line worktrees (#48):** each dispatch line works in its own git worktree and branch. The delivery-time git check result is stored with the Ticket report (migration 146).
- **First-run setup (#47):** `cond setup` detects installed CLIs and writes `agents.toml`; `cond agents` launches each CLI's official login and confirms it with the CLI's status command.
- **Super-agent TUI redesign (#45):** our own design language and a Markdown renderer (headings, lists, quotes, tables, code) checked by a fixed layout contract at 40, 80 and 120 columns.
- **Skills & MCP hub (#44):** one library renders skills and MCP servers into every agent CLI, with dry runs, snapshots and rollback. Apply only adds, never deletes, and reports orphaned entries.
- **RA-1 (#42):** the data-token code and finance-era residue are removed (migration 145).
- **Planning:** live auto-dispatch planning is wired, and Claude Code is the default planner.
- **Reports:** full Ticket report bodies are stored (migration 144).
- **Recovery:** dispatch lines recover on their own after a worker failure (W8b).
- **DSH:** the DeepSeek DSH worker can read its keys inside the sandbox.
- **Diagnostics:** native CLI failures carry a specific cause instead of `unknown`, and Cursor and ZCode have isolation flags.
- **Worker delivery contract:** delivery git checks run in a read-only, no-network sandbox.
- **Plan quota:** a ZCode plan that has run out is reported as `quota_exhausted`.
- **Report sanitizing:** report bodies are sanitized strict-first, with an allow-list; it runs in linear time, outside the DB transaction.
- **Memory federation (#37):** each Ticket worker turn gets one read-only memory block. It is selected from Conductor's own memory and, when the user has a binding, from a remote MemFW service (memfw-http/2, separate read and write tokens). Proposals stay in Conductor, and the remote write bridge is off by default.
- **Operations (`cond`):**
  - `cond up|down|status|restart|logs|doctor|upgrade|clean` are protected by a cross-process lifecycle lock.
  - Readiness has two layers (`/ready?scope=backend`, then full `/ready`).
  - `/ready` treats a missing MCP registry as optional. An unreadable one (a broken or looping link, an unreadable parent, malformed TOML) counts as not ready. `cond doctor` reports orphaned git fsmonitor daemons and FSEvents headroom (#38).
  - `upgrade` backs up the DB first, and rollback runs every step in reverse.
  - Worker CLIs auto-update at login and startup, with smoke checks, pins and a broken-CLI gate.
  - A launchd plist is generated. The maintainer's deployment has not switched to `cond up` yet (see `docs/COND.md`).

On the maintainer's local deployment, a read-only smoke Ticket succeeds on all 8 native workers: Codex, Kimi, Grok, Cursor, StepFun, MiniMax, DeepSeek DSH and ZCode. On that deployment, memory federation is live against a homelab MemFW service, and `/ready` returns 200.

## v1 (2026-11-07)

First release on `main`. See the internal document `RELEASE-READINESS` (not published).
