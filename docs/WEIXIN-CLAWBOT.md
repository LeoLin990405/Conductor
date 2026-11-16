# 微信 ClawBot（官方 iLink 渠道）

Conductor 在 **设置 → 数据来源 → 第三方数据源连接** 里接入腾讯官方微信
ClawBot 的 iLink 协议，不依赖 OpenClaw 运行时。

上游依据：[Tencent/openclaw-weixin](https://github.com/Tencent/openclaw-weixin)
的官方渠道插件与 [protocol.md](https://github.com/Tencent/openclaw-weixin/blob/main/docs/protocol.md)。
Conductor 实现的是同一套 iLink Bot 协议：扫码登录、长轮询 `getUpdates`、
`sendMessage` 文本回复。

## 能力

- 微信扫码登录，bot token 存在用户 vault（`WEIXIN_CLAWBOT_CONFIG`），不返回给前端；
- 后台长轮询接收文本消息；
- 消息按 `platform=weixin` + 微信 `from_user_id` 落到 Conductor thread；
- Conductor 的最终回复经 `sendMessage` 发回微信；
- 后端重启后自动恢复已有登录；
- 退出微信时停止 worker 并删除 vault 配置。

当前只处理文字。图片、语音、文件、群聊路由和 typing 状态尚未接入；消息里的
外部内容按不可信资料处理，重要数字仍需原始来源复核。

## 设置页流程

1. 打开 **设置 → 数据来源 → 第三方数据源连接 → 微信 ClawBot**；
2. 点“扫码登录微信”，页面显示官方二维码；
3. 微信扫码并在手机上确认；
4. 页面轮询到 `confirmed` 后保存 token，显示“已连接”；
5. 之后直接给微信里的 ClawBot 发文字，Conductor 会回复到该会话。

设置页接口：

```text
GET  /api/v1/connections
GET  /api/v1/connections/weixin
POST /api/v1/connections/weixin/login
POST /api/v1/connections/weixin/login/poll
POST /api/v1/connections/weixin/logout
```

## 配置

无需环境变量；后端运行在能访问 `ilinkai.weixin.qq.com` 的主机即可。
OSS 模式会自动恢复本地用户的 bot；平台模式在登录成功后启动该用户的 worker。

可选：

```bash
# 后端自己的 API 地址；默认 http://127.0.0.1:8000
CONDUCTOR_SELF_URL=http://127.0.0.1:8000
# 平台模式的 service token，用于 worker 回写 chat
INTERNAL_SERVICE_TOKEN=
```

## 与 OpenClaw 插件的关系

[Tencent/openclaw-weixin](https://github.com/Tencent/openclaw-weixin) 是 OpenClaw
的 channel 插件，必须运行 OpenClaw Gateway。Conductor 没有引入 OpenClaw，
而是直接实现官方 iLink 协议，因此微信消息会进入 Conductor 的 thread、记忆和
工具链，而不是另一个 agent。

## 测试与限制

- `tests/unit/server/services/test_weixin_bot.py`：二维码、文本消息、登录确认、
  入站回复、token 不泄露；
- `tests/unit/server/app/test_connections.py`：登录/轮询/退出接口；
- 真实网络冒烟只验证到官方 `get_bot_qrcode` 返回二维码；扫码和收发消息需要
  用户自己的微信确认；
- 官方 iLink 协议可能随客户端版本变化，升级时优先对照上游 `protocol.md`。
