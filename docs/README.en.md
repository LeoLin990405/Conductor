# Conductor

**The aggregation operating platform for multi-agent collaboration.**

Conductor is a Super-agent dispatch platform. You give it a goal, a code-review comment or a scheduled task; it turns each into a Ticket in one pipeline, hands the Ticket to a PTC agent or a native CLI Worker, and collects the threads, files, deliverables, reports and any question that needs your answer back into one Inbox.

- One entry point: Inbox, Tasks, Agents, Lines, Agent fleet, Runs (labelled Logs in the UI), Sources (GitHub, Feishu, cron), Plugins and Settings
- One Ticket pipeline: manual tasks, GitHub comments and cron schedules produce the same kind of Ticket (queued, running, waiting, blocked, failed, cancelled, retried, reported)
- Several agent runtimes: PTC workers, plus 12 native CLIs (Codex, Claude Code, Cursor, DeepSeek DSH, Doubao, Kimi, MiniMax, MiMo, Grok, OpenCode, StepFun, ZCode/GLM), whose session-resume and cache-usage reporting differ per CLI; Gemini and Qwen are not registered as native Workers (PTC only), and neither is MiniMax's `mmx` text API — the registered MiniMax Worker is the `mcode` agent CLI
- Local daemons: a registered daemon pulls Tickets and runs them inside an OS sandbox (`bwrap` on Linux, `sandbox-exec` on macOS; the Linux sandbox shares the host network namespace, so loopback ports stay reachable); cloud Workers are refused by default and run only in the opt-in Docker runtime (`AUTONOMY_CLOUD_WORKER_RUNTIME=docker`)
- Bounded concurrency: `AUTONOMY_MAX_RUNNING_WORKERS` (default 4) is the global slot cap checked when a local daemon claims a Ticket and when a cloud Worker launches; a launch that finds no free slot stays queued and is retried. A daemon takes 1-32 concurrent Tickets (default 1)
- Reliable delivery: durable effects and an outbox with leases, retries and idempotency keys; Postgres advisory locks; a stall watchdog
- Model plane: model manifest, provider routing, fallback, BYOK/OAuth; the Super-agent Router is pinned to DeepSeek-V4.1-Flash through the DeepSeek account route
- Workspaces, shared files, skills, MCP, Plugins and Settings stay in the app shell; the old Chat, Projects, Memory and Automations pages are retired and their deep links land on the Inbox
- A `conductor` CLI (Tickets, Sources, daemons, dispatch, deploy and rollback) and an Electron desktop shell around the hosted web app

Full documentation and commands: [README](../README.md).

[中文](README.zh-CN.md) · [日本語](README.ja-JP.md)
