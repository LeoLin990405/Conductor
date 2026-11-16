# Roadmap

> [!NOTE]
> **Scope freeze (2026-11-09).** Only packages already in review are being finished, each with at most two more fix rounds. New features wait until development runs through Conductor itself.

## In progress (planned and under review, not on `main` yet)

- **Super-agent TUI redesign:** merged (#45).
- **Skills & MCP hub:** merged. Apply is additive-only; propagating remove/disable through a journaled ownership release is next iteration.
- **Instructions hub:** one shared `AGENTS.md`/`CLAUDE.md` source, rendered for every CLI. Deferred to the next iteration: the read-only preview still misses CLI-specific files and must check the size budget on each final file.
- **Per-line worktrees for dispatch:** merged (#48, migration 146).
- **MemFW retrieval:** multi-hop expansion (memfw #18) and per-term search (memfw #20) are merged in memfw, and Conductor-side federation is merged (#37). Further retrieval rounds are frozen until after the cutover.
- **Shared memory ontology:** one canonical concept per thing, with aliases. The first 47 seed concepts are approved, and the cross-agent importers and OKF export are merged in fleet-memory. memfw concept anchors and alias expansion are merged (memfw #24); loading the seed concepts into the live vault needs approval.
- **Memory "dreams":** offline consolidation that only proposes candidates for human review (designed; its evaluation data is merged in memfw #21; implementation is next iteration).
- **Session handoff:** merged (#52). Before a proactive rotation, Kimi and MiniMax are asked for a self-report; the other providers use the deterministic note.
- **Retiring the remaining legacy chat surfaces.**
- **Prompt refiner (optional, off by default):** deferred to the next iteration. It was cut from the new-workers package. The design is unchanged: a Gemini/Antigravity model rewrites a request, the user confirms it, and the refiner never writes code.
- **Wakers for dynamic planning:** CodexBar quota events (low, exhausted, reset) and RepoBar repository events (CI, PRs, issues, releases) wake parked work. Router worker selection becomes quota-aware.
- **First-run setup:** merged (#47): `cond setup` detects installed CLIs and writes `agents.toml` (refuses to overwrite without `--force`), and `cond agents` can launch each CLI's official login, then confirms it with that CLI's own status check. The role-selection page is deferred.
- **New native workers:** OpenCode merged (#50), with its config isolated under the per-worker HOME.
- **Cross-agent memory importers:** merged in fleet-memory, including ZCode memory silos and safe frontmatter parsing. OKF export with concepts waits for the memfw ontology.

## Open follow-ups

- [ ] Add a Linux network namespace and egress proxy so the worker loopback deny is enforceable ([`docs/ports/worker-isolation.md`](ports/worker-isolation.md)).
- [ ] Reconcile the leftover configuration and documentation with the platform boundary in `FORK-CN.md`.
- [ ] Run `make up` (the whole Compose stack) once and record the result; both backend images now build (2026-11-06, the internal document `RELEASE-READINESS` (not published)).
