# Conductor 特性详解

README 里每项特性只有一行，这里是完整说明。

- **统一入口。** 手动目标、GitHub issue 与 PR 评论、cron 定时任务与飞书消息都会变成同一条流水线里的 Ticket。
- **可持久化的 Ticket 生命周期。** Ticket 的状态是 queued、running、waiting、ready、blocked、awaiting clarification、completed、failed、cancelled 或 archived；switch 会保留线程和 worktree，文件 handoff 附带 sha256 manifest。
- **两套 worker 运行时。** PTC agent 在沙箱里运行 Python、MCP 工具和子 Agent；native CLI worker 用厂商 CLI 自己的会话和缓存执行。
- **13 个 native adapter，11 个 native Worker。** `src/llms/native_cli/` 里有 Codex、Claude Code、Cursor、Kimi、DeepSeek DSH、Grok、StepFun、MiniMax（mcode）、MiMo、ZCode、GLM（智谱）的 adapter，另有 Doubao/ARK 与 MiniMax mmx 两个纯文本 adapter，它们没有注册为 native Worker。Gemini 与 Qwen 只走 PTC。
- **有界并发。** 本机 daemon 同时运行 1–32 个 Ticket（默认 1）。`AUTONOMY_MAX_RUNNING_WORKERS`（默认 4）还限制所有 Worker 上同时 running 的 Ticket 总数：daemon claim 与 Cloud Worker 启动在同一把锁下一起计数，找不到空闲 slot 的启动会留在 queued 排队而不是失败（[`docs/CLOUD-WORKERS.md`](CLOUD-WORKERS.md)，“Concurrency”）。`NativeAgents` 一次调用最多并发 4 个任务，`NATIVE_CLI_MAX_PARALLEL`（默认 4）只能把它调低。
- **实时 Worker 输出。** 本机 daemon 在 Codex CLI 运行期间，把它可见的文本和工具名推给服务端（默认走 app-server 的 token delta，`CONDUCTOR_CODEX_APP_SERVER=0` 时只回传已完成条目；在出现任何可见输出之前结束且没有最终答案的回合，会回退到 `codex exec` 重试一次）；`GET /api/v1/autonomy/tickets/{id}/activity/stream`（SSE）、CLI 与 TUI 可以跟随，断线后续传。共享的凭据扫描器在两端检查每个事件，命中即终止该次运行的实时文本。只有 Codex，需要迁移 140，`CONDUCTOR_DAEMON_TOKEN_STREAM=0` 可关闭，扫描器是启发式而不是保证（[`docs/SUPER-AGENT.md`](SUPER-AGENT.md)）。
- **事务化投递。** 跨进程动作以 durable effect 写入 outbox，带 lease、重试和幂等键；Postgres advisory lock 让多个 backend worker 安全调度。
- **监督机制。** 确定性 watchdog 标记卡住的 queued、waiting、running Ticket，并为卡住的 running worker 排入独立的 Watchdog session 复核。
- **沙箱选择。** PTC agent 可用 Daytona、E2B、Docker 与 SSH computer。本机 native worker 必须有 OS 隔离 profile，缺失时拒绝启动；云端自主 Worker 默认被拒绝，只有显式开启的 Docker 运行时才会启动（见下）。
- **审查与合并 API。** `conductor review` 与 `conductor merge` 调用服务端路由：审查裁决是持久的 Postgres 行；合并先在临时 worktree 里演练并跑测试，由服务端 Codex 审查，之后才用 compare-and-swap 的 `git update-ref` 落地（[`docs/REVIEW-MERGE-API.md`](REVIEW-MERGE-API.md)）。
- **操作面。** REST API、`conductor` CLI、交互式 Super-agent TUI、Web 控制台、Electron 桌面壳、Agent Plugins 包、MCP 目录，以及只读的 agent fleet 视图。
