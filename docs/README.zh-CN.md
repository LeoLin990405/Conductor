# Conductor 中文说明

**多 Agent 协作的聚合操作平台。**

Conductor 是一个 Super-agent 调度平台：你给出目标、代码评论或定时任务，它都变成同一条流水线里的 Ticket，交给 PTC Agent 或本机原生 CLI Worker 执行，并把线程、文件、产物、报告和需要你回答的问题收拢回 Inbox。完整说明见仓库根目录的 [README](../README.md)。

- 单一入口：Inbox、Tasks、Agents、Lines、Agent fleet、Runs（界面标签为 Logs / 执行日志）、Sources（GitHub、Feishu、cron）、Plugins、Settings
- 统一 Ticket 流水线：手动任务、GitHub 评论和 cron schedule 生成同一种 Ticket（排队、运行、等待、阻塞、失败、取消、重试、报告）
- 多种执行体：PTC worker，加上 12 个 native CLI（Codex、Claude Code、Cursor、DeepSeek DSH、Doubao、Kimi、MiniMax、MiMo、Grok、OpenCode、StepFun、ZCode/GLM），各 CLI 的会话续接与缓存用量能力不同；Gemini 与 Qwen 不注册为 native Worker（只走 PTC），MiniMax 的 `mmx` 文本 API 同样没有注册——注册的 MiniMax Worker 是 `mcode` 这个 agent CLI
- 本地 daemon：注册后的 daemon 拉取 Ticket，在 OS 沙盒里执行（Linux 用 `bwrap`，macOS 用 `sandbox-exec`；Linux 的沙盒与宿主共享网络命名空间，环回端口仍可达）；云端 Worker 默认被拒绝，只有显式开启的 Docker 运行时（`AUTONOMY_CLOUD_WORKER_RUNTIME=docker`）才会运行
- 有界并发：`AUTONOMY_MAX_RUNNING_WORKERS`（默认 4）是本地 daemon claim Ticket 与云端 Worker 启动时都会检查的全局槽位上限，启动时没有空槽的 Ticket 留在 queued 并稍后重试；daemon 可配置 1–32 个并发 Ticket，默认 1
- 可靠投递：durable effect / outbox，带 lease、重试和幂等键；Postgres advisory lock；stall watchdog
- 模型平面：模型清单、provider 路由、fallback、BYOK/OAuth；Super-agent Router 固定为 DeepSeek-V4.1-Flash（DeepSeek 账号路由）
- 工作区、共享文件、skills、MCP、Plugins 与 Settings 留在 App Shell；旧的 Chat、Projects、Memory、Automations 页面已下线，深链会回到 Inbox
- `conductor` CLI（Ticket、Sources、daemon、dispatch、deploy / rollback）和包装托管 Web 应用的 Electron 桌面端

[English](README.en.md) · [日本語](README.ja-JP.md)
