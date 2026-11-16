# GLM-5.3（智谱）适配

第五个按同一流程对齐的模型族。本机无 `ZAI_*` BYOK key，但本地库中存在一条 **智谱订阅登录（z-ai-cn-oauth）** token，因此配置依据官方文档 + DSH pi-ai 目录对齐，并已用该订阅账号完成真实调用验证。

## 证据

官方文档（docs.bigmodel.cn）：
- **GLM-5.3**：仅文本、1M 上下文、最大输出 128K；始终思考，`thinking.type` 仅支持 `enabled`，`reasoning_effort` 支持 `low/high/max` 且**默认 max**；推荐 `temperature=1`、`top_p=0.95`
- **GLM-5.3-Flash**：原生多模态，输入 **视频/图像/文本/文件**，1M 上下文，最大输出 128K；思考同样始终开启，推荐 `reasoning_effort=max`、`clear_thinking:false`
- Anthropic Message 协议端点：`https://open.bigmodel.cn/api/anthropic`；OpenAI 端点：`/api/paas/v4`
- Coding Plan 当前通过 OpenAI Chat Completion 协议调用

DSH `@earendil-works/pi-ai`：
- `zai.json` / `zai-coding-cn.json`：GLM-5.3 1M context、maxTokens **131072**、thinkingLevelMap low/high/max（off 为空 → 不可关闭）；GLM-5.3-Flash 同窗口 + 图片 resize 2000×2000 / 4.5 MiB / JPEG 80

## 本轮对齐

| 项 | 变更 |
|---|---|
| 默认推理强度 | GLM-5.3 全系 high → **max**（官方默认） |
| 输出上限 | GLM 全系 128000 → **131072**（DSH 适配器口径；官方文档记作 128K） |
| 图片边界 | z-ai 系 provider 增加 `image_resize` 2000×2000 / 4.5 MiB / JPEG 80；图片中间件覆盖 z-ai / z-ai-cn / z-ai-coding / oauth 变体 |
| 思考配置 | 既有 `extra_body.thinking = {type: enabled, clear_thinking: false}` + `reasoning_effort` 与官方推荐一致；确认无 off 档 |
| 多模态 | GLM-5.3-Flash 保留 text/image/pdf（官方为视频/图像/文本/文件）；视频输入管线尚未实现 |
| 订阅模型 | 新增 `glm-5.3-flash-cn-oauth`（智谱订阅登录可直接用 Flash 多模态），已加入 OAuth 目录与测试 |

## 真实调用验证（订阅 token `z-ai-cn-oauth`）

| 场景 | 结果 |
|---|---|
| `glm-5.3-cn-oauth` + `reasoning_effort=low` | ✅ 返回 ok，含 thinking 块 |
| `glm-5.3-cn-oauth` + `reasoning_effort=max` | ✅ 返回 ok，含 thinking 块 |
| max_tokens | ✅ 客户端 131072 |
| 解析路径 | ✅ `resolve_oauth_llm_client("local-dev-user", "glm-5.3-cn-oauth")` 走 zhipu OAuth token，端点 `/api/anthropic` |
| `glm-5.3-flash-cn-oauth` 文本 + `reasoning_effort=low` | ✅ 返回 ok |
| `glm-5.3-flash-cn-oauth` 视觉（base64 图片） | ✅ "Green" |
| Flash max_tokens | ✅ 客户端 131072 |

## 未完成 / 阻塞

- coding plan key 实测可用：`glm-5.3-cn-coding`（provider `z-ai-cn-coding`，Anthropic 端点）以 `reasoning_effort=max` 真实返回 ok + thinking，说明官方「coding plan 目前仅 OpenAI 协议」的说明不适用于该端点，Conductor 现有 Anthropic 路由可保留
- 仍待验证：1M 上下文长文、GLM-5.3-Flash 图片/文件/视频输入、BYOK 的 `ZAI_*` key
- GLM-5.3-Highspeed / GLM-4.6V / FlashX / Legal 等新条目未加入（缺价格/目录条目）
- 视频输入、顶层缓存 TTL、结构化输出未接
- Coding Plan 当前走 Anthropic 端点；官方说明 coding plan 目前以 OpenAI 协议为主，需真实 key 验证后再决定是否切换

## 测试

新增 GLM parity 测试（1M/131072/默认 max/无 off/Flash 多模态/图片边界）与 z-ai 图片中间件覆盖。
