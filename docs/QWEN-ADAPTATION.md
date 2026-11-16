# Qwen3.8 / 3.7（通义千问 / DashScope）适配

第六个按同一流程对齐的模型族。本机无 `DASHSCOPE_API_KEY`，因此本轮依据 **阿里云百炼官方文档 + DSH token-plan 目录** 对齐配置，真实调用标记为待验证。

## 官方文档确认（阿里云百炼 Model Studio）

| 项 | Qwen3.8 Max / Flash |
|---|---|
| 上下文 | 1,000,000（最大输入 991,808，思考模式 983,616） |
| 最大输出 | **131,072**（思考模式同） |
| 最大思维链 | 262,144（Flash `thinking_budget` ≤256K） |
| 输入模态 | **Image / Text / Video** |
| 推理 | 混合模式默认开启；`reasoning_effort` 取值 **low / medium / xhigh**，Max 默认 **xhigh**；Flash 支持 `thinking_budget` |
| 其他 | 函数调用、结构化输出、前缀续写、上下文缓存、联网搜索 |

Qwen3.7 Plus：混合模式默认开启，无 `reasoning_effort` 档位（DSH `supportsReasoningEffort:false`），DSH 目录 maxTokens 65536。

## DSH pi-ai 目录交叉验证

- `qwen-token-plan.json` / `qwen-token-plan-cn.json`：qwen3.8-max/flash 1M context、maxTokens 131072、图片 resize 2000×2000 / 4.5MiB / JPEG 80、`thinkingLevelMap` **仅 low/medium/xhigh**；qwen3.7-plus maxTokens 65536、不支持 effort。
- Token Plan 还聚合了第三方模型（deepseek/glm/kimi 等），与 Conductor 的聚合模型目录方向一致。

## 本轮对齐

| 项 | 变更 |
|---|---|
| qwen3.8 推理档位 | 七档（none..max）→ **low/medium/xhigh**（官方/DSH 一致） |
| qwen3.8 默认档位 | medium → **xhigh**（官方默认） |
| qwen3.8 输出上限 | 新增 **131072** |
| qwen3.7-plus | 移除不存在的 reasoning 档位；新增 **65536** 输出上限 |
| 图片边界 | dashscope / dashscope-intl provider 增加 2000×2000 / 4.5MiB / JPEG80；图片中间件覆盖 |
| 模态 | 保持 text/image（官方还支持 video；Conductor 尚无视频注入/传输管线） |

## 请求形态契约（无 key 也可验证）

`tests/unit/llms/test_blocked_family_contracts.py` 检查 Responses 客户端构造出的请求体：

- `qwen3.8-max`：`max_output_tokens=131072`、`reasoning={"effort": "xhigh"}`、`use_responses_api=True`
- `qwen3.7-plus`：`max_output_tokens=65536`、无 reasoning 档位

这保证 manifest 调整不会悄悄丢掉 effort 或输出上限；真实调用仍待 `DASHSCOPE_API_KEY`。

## 未完成 / 阻塞

- **真实调用阻塞**：本机无 `DASHSCOPE_API_KEY`；拿到后需验证：xhigh 档位、131072 上限、图片/视频输入、Responses API reasoning 透传、缓存计费字段
- 视频输入管线、`thinking_budget` 显式控制、`enable_thinking` 关闭未接
- Qwen3.6/3.5 条目与 Token Plan 聚合端点未加入

## 测试

新增 Qwen parity 测试（1M/131072/三档/xhigh/3.7 无档位/图片边界）与 dashscope 图片中间件覆盖。
