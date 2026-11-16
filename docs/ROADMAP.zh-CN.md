# 路线图

> [!NOTE]
> **范围冻结（2026-11-09）。** 只收尾已经在审查中的包，每个最多再修 2 轮；新功能等开发本身改由 Conductor 派发之后再做。

## 进行中（已规划、正在审查，尚未进入 `main`）

- **Super-agent TUI 重设计：** 已合并（#45）。
- **Skills & MCP Hub：** 已合并。apply 只增不删；通过 journal 记录所有权释放、再传播 remove/disable，放到下个迭代。
- **Instructions Hub：** 共用一份 `AGENTS.md`/`CLAUDE.md` 源文件，为每个 CLI 生成各自的版本。已推迟到下个迭代：只读预演还会漏掉各 CLI 的专属文件，也要对每个最终文件检查大小上限。
- **派发线独立 worktree：** 已合并（#48，迁移 146）。
- **MemFW 检索：** 多跳扩展（memfw #18）和按词检索（memfw #20）已合入 memfw，Conductor 侧的联邦也已合入（#37）。后续的检索轮次冻结到切换之后。
- **共享记忆本体：** 每个东西一个统一概念，并登记别名。首批 47 个种子概念已审定；跨 agent 的记忆导入器和 OKF 导出已合入 fleet-memory；memfw 的概念锚点和别名展开已合入（memfw #24）；把种子概念导入正式 vault 需要先经批准。
- **记忆“做梦”：** 离线整理记忆，只提出候选、交给人审（已完成设计；评测数据已合入 memfw #21；下个迭代实现）。
- **会话交接：** 已合并（#52）。主动轮换前，Kimi 和 MiniMax 会先给一份自述；其他 provider 用固定的交接说明。
- **退役剩余的旧 chat 入口。**
- **提示词优化（可选，默认关闭）：** 推迟到下个迭代，已从新 worker 包中拆出。设计不变：由 Gemini/Antigravity 模型改写需求，用户确认后才执行，它不写代码。
- **用于动态规划的唤醒器：** CodexBar 的额度事件（偏低、用尽、重置）和 RepoBar 的仓库事件（CI、PR、issue、发布）会唤醒停住的工作；Router 选 Worker 时会考虑剩余额度。
- **首次启动引导：** 已合并（#47）：`cond setup` 检测已安装的 CLI 并写入 `agents.toml`（不加 `--force` 不会覆盖已有文件）；`cond agents` 可以调用各 CLI 的官方登录，登录后再用该 CLI 自己的状态检查确认。角色选择页已推迟。
- **新的 native worker：** OpenCode 已合并（#50），配置目录隔离在每个 worker 自己的 HOME 下。
- **跨 agent 的记忆导入器：** 已合入 fleet-memory，包括 ZCode 记忆仓和安全的 frontmatter 解析。带概念的 OKF 导出等 memfw 本体完成后再做。

## 已知待办

- [ ] 为 Linux 增加 network namespace 与 egress proxy，使 worker 的 loopback 拒绝可以强制执行（[`docs/ports/worker-isolation.md`](ports/worker-isolation.md)）。
- [ ] 让遗留的配置与文档和 `FORK-CN.md` 的平台边界保持一致。
- [ ] 运行一次 `make up`（整套 Compose 栈）并记录结果；两个 backend 镜像现在都能构建（2026-11-06，内部文档 `RELEASE-READINESS`（未公开））。
