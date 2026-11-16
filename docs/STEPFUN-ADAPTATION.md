# StepFun（阶跃星辰）适配

第七个按同一流程对齐的模型族。本机有 **`STEPFUN_PLAN_API_KEY`（Step Plan 订阅）**，因此除官方文档外，还用 Step Plan 的 `/models` 元数据与真实调用做了验证（DSH pi-ai 目录中没有 StepFun provider）。

## 官方文档确认

| 模型 | 上下文 | 最大输出 | 输入 | 推理 |
|---|---|---|---|---|
| `step-5-preview` | 1,000,000 | **64K** | Text / Image / Video（≤60 图、MP4<128MB、建议<5min） | `low`/`medium`/`high`；Chat 用 `reasoning_effort`，Messages 用 `output_config.effort` |
| `step-3.7-flash` | 256K（262,144） | 文档未给固定上限（Chat API 默认 INF） | Text / Image / Video | `low`/`medium`/`high`，默认 medium |
| `step-3.5-flash` | 256K | — | Text | 推理模型（低推理模式仅 2603 变体） |
| `step-router-v1` | 262,144 | — | Text | 路由模型 |

端点：
- OpenAI 协议：`https://api.stepfun.ai/step_plan/v1`（Step Plan，CN 平台为 `api.stepfun.com/step_plan/v1`）
- Anthropic 协议：base `https://api.stepfun.ai/step_plan`（SDK 自动追加 `/v1/messages`）
- 其他能力：工具调用、JSON Mode/Schema、Prompt Caching、流式

## 本轮对齐

| 项 | 变更 |
|---|---|
| step-5-preview 输出上限 | 新增 **65536**（64K） |
| step-5-preview 上下文 | 1,000,000 → **1,024,000**（Step Plan `/models` 的 `max_input_tokens`） |
| step-3.5-flash 推理 | 新增 low/medium/high，默认 medium |
| step-5-preview 输入模态 | 补充 **image**；视频管线落地后补充 **video**（见 `docs/VIDEO-INPUT.md`） |
| step-5-preview 推理档位 | 新增 low/medium/high，默认 medium，写 `parameters.reasoning_effort` |
| step-3.7-flash 输入模态 | 补充 **image**；视频管线落地后补充 **video**（见 `docs/VIDEO-INPUT.md`） |
| step-3.7-flash 推理档位 | 新增 low/medium/high，默认 medium |
| 端点 | 保留既有 CN `api.stepfun.com` 根路径与 Step Plan 变体；与官方文档的协议路径一致 |

## 真实调用验证（`STEPFUN_PLAN_API_KEY`）

| 场景 | 结果 |
|---|---|
| `step-5-preview` + `reasoning_effort=low` | ✅ 返回 ok |
| `step-3.7-flash` + `reasoning_effort=medium` | ✅ 返回 ok |
| `step-5-preview` base64 图片 | ✅ "Red" |
| `step-5-preview` / `step-3.7-flash` base64 MP4（1.2s 测试视频，Read ToolMessage 形态） | ✅ 正确描述画面（`docs/VIDEO-INPUT.md` §5） |
| `step-3.5-flash` + 视频 | ✅ 400 `model_incompatible`（读侧 strip 会先替换为占位，不会发出去） |
| Step Plan `/models` | ✅ 返回 max_input_tokens / vision / effort 列表，用于校正上下文 |

## 未完成 / 阻塞

- `step-3.5-flash-2603` 低推理模式、step-router 路由行为
- 1M 上下文长文与 tool calling / prompt caching 尚未实测
- 官方文档无 DSH 对应 provider，无法做 DSH 目录交叉验证

## 测试

新增 StepFun parity 测试（1M/65536/三档/medium/模态 + 3.7 flash 256K），模态断言包含 `video`；视频管线专项测试见 `tests/unit/ptc_agent/middleware/file_operations/test_multimodal_video.py`。
