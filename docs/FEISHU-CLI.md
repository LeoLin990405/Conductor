# 飞书（Lark）CLI 接入

Conductor 通过官方 [飞书 CLI](https://github.com/larksuite/cli) 接入飞书，而不是在服务端保存一套飞书 SDK 凭证。CLI 负责应用绑定、用户授权和 token 刷新；Conductor 只提供受控的 `FeishuCLI` 工具。

## 能力

当前接的是 **读优先** 的群聊消息入口：

| action | 用途 |
|---|---|
| `status` | 查看 CLI 是否已安装、已绑定应用、token 是否有效 |
| `search_chats` | 按群名关键字查群，拿到稳定的 `oc_...` chat_id |
| `list_chats` | 列出机器人/用户可见的群 |
| `read_messages` | 读取一个群最近消息，支持 `start`/`end` 时间窗和条数上限 |
| `search_messages` | 跨群按关键字、群、时间范围搜索消息 |
| `send_message` | 显式发送消息（需要用户明确要求时使用） |

典型工作流：

```text
搜索“项目/研发/运营”群
  → 取 chat_id
  → 读今天 09:00-15:00 的消息
  → 把消息当作外部资料，结合原始来源做交叉验证
```

消息是外部内容，工具描述里明确要求模型不要把它当指令；重要数字仍需原始来源复核。

## 安装与授权

后端必须运行在 **能访问 lark-cli 的主机** 上（当前 host backend 满足；Docker backend 看不到宿主机 CLI）。

```bash
npm install -g @larksuite/cli
lark-cli --version
lark-cli config init --new
```

`config init --new` 会创建/绑定飞书自建应用并输出一个授权链接；在飞书中完成确认后：

```bash
lark-cli doctor
lark-cli whoami
lark-cli auth login     # 需要以用户身份读私密群时
```

机器人身份能读的群取决于应用权限和机器人是否在群里；用户身份能读该用户可见的群。本租户的机器人身份被数据安全策略拒绝，因此 `list_chats`/`search_chats`/`read_messages`/`search_messages` 默认以 `identity=user` 调用；需要机器人身份时显式传 `identity=bot`。只做通知时应用身份即可。

## 设置界面

飞书登录已经放进 **设置 → 数据来源 → 第三方数据源连接**：

1. 打开设置页，飞书卡片显示“应用已绑定 / 待授权 / 已连接”；
2. 点“连接飞书”，后端通过 `lark-cli auth login --no-wait --json --domain im` 生成设备码；
3. 页面显示二维码和授权链接，飞书确认后点“我已授权，完成连接”；
4. 后端用 `lark-cli auth login --device-code ...` 换取用户 token，卡片变为“已连接”。

接口在 `src/server/app/connections.py`，返回结构是 provider 列表而不是飞书专用字段，
以后新增第三方数据源只需增加一个 provider 描述和对应 adapter；页面统一从
`GET /api/v1/connections` 读取，不返回任何第三方 secret。

Feishu third-party settings

## 服务配置

`.env`：

```bash
FEISHU_CLI_ENABLED=1
# 可选：服务 PATH 里没有 npm-global 时
FEISHU_CLI_BIN=/Users/you/.npm-global/bin/lark-cli
# FEISHU_CLI_TIMEOUT=45
```

重启后端后，主 Agent 的工具列表会多出 `FeishuCLI`。子 Agent 没有这个工具，避免后台任务扩大外部读取范围。

也可以用飞书官方 MCP（`@larksuiteoapi/lark-mcp`）接入 Conductor 的 MCP 服务器，但 CLI 额外提供本地 `event consume`，更适合后续做群消息持续采集。

## 后续：群消息作为持续信息源

这次先完成按需读取。下一步可以做：

1. `lark-cli event consume im.message.receive_v1` 常驻消费群消息；
2. 按 `chat_id` 白名单 + 去重写入本地证据库；
3. 交给 Conductor 的分析流程做摘要、事件抽取和来源交叉验证；
4. 只在用户明确配置的群里启用，写操作继续走人工确认。

## 故障排查

- `not_configured`：运行 `lark-cli config init --new`，按提示在浏览器完成授权。
- `找不到 lark-cli`：安装 CLI，或设置 `FEISHU_CLI_BIN` 为绝对路径。
- 没有可见群：机器人不在群里；先邀请机器人，或用 `identity=user` 并在 `lark-cli auth login` 后读取用户可见群。
- 权限不足：按 CLI 报错里的 scope 提示到飞书开放平台开通权限；用户身份可用 `lark-cli auth login --scope ...`。
