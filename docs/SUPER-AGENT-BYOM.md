# BYOM：云端 Super-agent + 本地 daemon

BYOM 把 Ticket 的 native CLI 执行放到用户自己的机器。Router、Ticket、排队和
报告仍由 Conductor backend 管理。GitHub/Feishu Source、手动任务和 recurring
schedule 都可以绑定一个用户自己的 daemon；未绑定 daemon 时沿用 backend Worker。

```text
GitHub / Feishu Source ─┐
手动任务 ────────────────┼─> Ticket(worker_daemon_id) -> local daemon -> report
recurring schedule ─────┘               backend keeps Router / Ticket / Outbox
```

## 注册和绑定

在 **Agents → Local daemons** 注册，或调用管理 API：

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -H 'X-User-Id: local-dev-user' \
  -d '{"name":"my-laptop","provider":"codex"}' \
  http://127.0.0.1:8000/api/v1/autonomy/daemons
```

响应中的 token 只返回一次。它由 32 个随机字节生成，呈 43 位 URL-safe bearer
字符串；数据库只保存 SHA-256。daemon 管理列表、创建和删除由用户身份保护。Source、
manual Ticket 和 schedule 绑定时，backend 校验 daemon 属于当前用户，并把任务
provider 固定为 daemon 注册的 provider；不匹配会拒绝请求。Daemon token 只允许
claim、heartbeat、progress 和 report，claim 只会返回分配给该 daemon 的 Ticket。

手动任务绑定示例：

```json
{
  "prompt": "Compute 6 * 7 and reply with only 42.",
  "workspace_id": "<home-workspace-id>",
  "worker_provider": "codex",
  "worker_daemon_id": "<daemon-id>"
}
```

Source 的 GitHub/Feishu config 和 recurring schedule 也接受 `worker_daemon_id`；
`worker_provider` 可省略并从 daemon 推断，显式填写时必须匹配。Source 更新会替换
整个 config，因此解除绑定时在新 config 中传 `worker_daemon_id: null`；schedule 的
PATCH 传 `worker_daemon_id: null` 可清除绑定。Agents、Sources、manual task 和 schedule
表单提供 daemon 选择。未指定 daemon 时仍走既有 backend Native Worker。

## 启动

```bash
CONDUCTOR_DAEMON_TOKEN=<token> \
.venv/bin/python scripts/conductor_local_daemon.py \
  --base-url https://conductor.example \
  --provider codex \
  --cwd ~/work/my-repo
```

`--once` 只处理一个 Ticket。Daemon 在运行 CLI 前检查 token 是否存在、provider
是否受支持，以及 claim Ticket 的 provider 是否与本机配置一致；错误 provider 会
在不启动 CLI 的情况下报告失败。它使用 Ticket 的 `worker_model`。普通 Codex
Ticket 可写入工作目录；observe/speculative Ticket 使用 Codex plan/read-only 模式。
本机 policy（`--policy`）要求只读时——policy 文件的 `read_only: true`、provider 自己的
`read_only: true`，或 speculative Ticket——CLI 同样以只读模式运行，外层 workspace
沙箱也收回工作目录的写权限（CLI 私有状态目录仍可写），因此即使请求绕过操作模式匹配，
该次运行也无法修改工作目录。
Grok、StepFun、ZCode 保持 adapter 的 read-only 模式，Claude Code 和 Cursor
使用其固定 plan 模式。无法由 adapter 保证只读的 provider 会 fail closed。
本机 policy 文件是设备侧权威边界：必须属于 daemon 用户、权限 0600 或更严格、不是符号
链接，并且位于所有 workspace 和 isolation root 之外；未知字段会拒绝加载；请求或
launch prompt 中无法归类为任何操作的“可执行片段”（shell 内建命令、PATH 上的可执行
文件或路径）同样 fail closed。详细格式见 `config/daemon_policy.example.yaml` 与
`docs/ports/beyond-endpoint-plan.md`。

Daemon 每次 claim 都持有独立的 90 秒执行 lease，因此一个 daemon 可以并行处理多个
Ticket。注册或更新 daemon 时可将持久化 `max_concurrency` 设为 1–32（默认 1）；本机
`--max-concurrency` 也为 1–32（默认 1），限制该进程的 claim loops。每个进程的实际执行
上限取 daemon 持久化上限、本机上限和 `AUTONOMY_MAX_RUNNING_WORKERS` 全局上限的最小值，
全局上限默认 4。创建 API、PATCH API 和 Agents → Local daemons 可查看或更新持久化上限；
数据库事务中的 claim 会执行该上限，因此多个进程共用同一 token 时总量仍受限。Daemon 与
backend Worker 共用由 Postgres 协调的全局 slot cap。满槽 Ticket 保持 queued，后续调度再试。
Daemon 默认每 10 秒为仍在本机运行的 Ticket 批量 heartbeat，也可用 `--heartbeat-interval`
调整。运行期间还会按 `--interval` 轮询所属 Ticket 状态；Ticket 结束或 stop 后，
daemon 取消本地 CLI 子进程且不再提交终态 report。blocked/waiting Ticket 的 UI 回复
保留为未投递 input；下一次 claim 用新的持久 claim id 和 lease 原子获取 input 并追加
到原 prompt。claim 收据使响应丢失时可安全重放；input 只在 lease/status 更新成功的
事务中标记 delivered。运行中的 Ticket 不接受此 resume 路径，避免在有效 lease 上启动
第二个 CLI。Progress
和 report 必须携带当前 lease id；lease 过期或被替换后，旧进程不能再更新服务器状态。
相同 claim id 的重试会返回原 claim，report 的重试会返回原结果，避免网络响应丢失后重复
启动同一 claim 或重复提交报告。对网络超时、限流和服务端错误，daemon 使用带上限的指数
退避和随机抖动重试；认证错误会停止。失去 heartbeat 或本地 lease 截止时间到达时会取消
对应 CLI 子任务，并尽力报告失败；无法联系 backend 时报告可能无法送达。UI stop 会先写入
持久 stop request，daemon 通过状态轮询看到 `cancelled` 后取消 CLI、确认 leader 与进程组
已退出，再提交带进程证据的 stop ack；服务端核对取消状态、lease 以及 stop request 之后的
heartbeat，记录 `verified` 或 `unverified`。超过 `autonomy_stop_ack_timeout_seconds`
（默认 30 秒）未确认时，scheduler 会把 stop 升级为 `unverified`、撤销 lease 并在 Inbox
提示。仍只轮询状态、不提交 ack 的旧 daemon 会被记录为 `unverified`，不会再被默认为已停止。
删除仍被
Source、schedule 或非终态 Ticket 使用的 daemon 会返回 409；先解除绑定并结束相关
Ticket，再删除 daemon。

Migration 110 给 `autonomy_schedules` 增加 daemon 外键和索引，并加上 Source 与
schedule 的数据库 owner guard。Migration 111 增加每 Ticket 的 lease/fencing 字段和持久
claim/report 收据。Migration 112 增加 daemon 持久化并发上限。Migration 119 增加
`worker_lease_heartbeat_at`、`autonomy_stop_requests` 及 stop 超时索引。Sources 的绑定
保存在 config JSON 中。这些 migration 随代码提交加入；本地隔离验证不代表已应用到任何
数据库。部署 Migration 111 后，daemon progress/report 必须带 lease id；部署 Migration
112 后可使用持久化并发上限。升级时请同时部署新版 daemon 脚本，旧客户端不兼容 lease 协议，
其停止只会留下 `unverified` 结果。

## 验收状态和限制

历史本机 manual Codex pilot 完成过一次 `6 * 7` 任务并报告 `42`。它证明当时的
manual claim/CLI/report 路径曾真实执行；它不验证目前的 Source/schedule 路由、
Migration 110、多 worker 并发或当前代码头。当前真实 Source→daemon 端到端验收和
`scripts/multiworker_gate/` 尚未运行；本次没有触碰正在运行的 8000 端口。

Scheduler 在发现 lease 过期后会把仍 running 的 Ticket 放回队列，允许 daemon 再次执行。
这是重新执行，不是恢复 native CLI 会话；lease fencing 保护服务器状态，不能撤销失联前
CLI 已产生的本机或外部副作用。UI stop 依赖状态轮询，取消本地进程会有一个轮询周期的延迟；
stop ack 是 daemon 对本机进程的证明，服务端校验状态与 heartbeat，但无法独立检查用户机器。
Verified-stop 路径目前由 fake runner 和 focused unit tests 覆盖，尚未用真实 native CLI、
生产数据库迁移或真实超时升级验收。
服务没有 mTLS 或
机器指纹，部署应使用 TLS。Daemon token 是 bearer 凭据，持有者可以执行分配给它的
Ticket，因此需要像本机 CLI 凭据一样保护。
