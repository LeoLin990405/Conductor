# 知识星球（Zsxq）

Conductor 通过本机 OpenCLI 的官方 `zsxq` adapter 接入知识星球。设置页提供登录入口、状态检测和动态测试；OpenCLI 负责浏览器会话，Conductor 不保存密码。

## 能力

- 打开知识星球 Web 登录页，支持用户自己的微信/手机号登录；
- `opencli zsxq whoami` 检测登录状态；
- `opencli zsxq dynamics --limit N -f json` 读取所有星球最新动态；
- API 层提供测试数据、登录检测，便于后续把星球动态作为社群信息源；
- 支持 `groups`、`topics`、`topic`、`search` 等 OpenCLI 命令，可在后续 adapter 中扩展。

## 设置页

**设置 → 数据来源 → 第三方数据源连接 → 知识星球**

1. 点“打开知识星球登录”，浏览器会打开 `https://wx.zsxq.com/`；
2. 用微信/手机号完成登录；
3. 回 Conductor 点“检测登录”；
4. 连接后点“测试动态”验证读取。

接口：

```text
GET  /api/v1/connections/zsxq
POST /api/v1/connections/zsxq/browser-login
POST /api/v1/connections/zsxq/test
```

## 前置条件

- 后端主机安装 OpenCLI（`opencli` 在 PATH，或设置 `OPENCLI_BIN`）；
- OpenCLI 浏览器扩展已连接；
- 知识星球账号有可见星球；动态读取是只读的。

## 限制

- 当前接入的是登录/读取入口，动态只读，尚未接入任何内容管道；下一步可按星球白名单做去重、摘要和来源交叉验证；
- OpenCLI 的站点会话和反爬状态由 OpenCLI/浏览器管理，Conductor 不保存 Cookie/密码；
- 仅用于用户本人可见的星球内容，不要把私有星球内容外传。
