# Doubao（火山方舟 ARK Plan）适配

第八个按同一流程对齐的模型族。与前面几家不同：**官方文档站（docs.volcengine.com / console）在本机不可抓取**（SPA + 反爬，只返回重定向空页），因此以「ARK Plan 路由 + 真实调用」为主要证据，公开文档/第三方资料仅作参考。

## 证据

- **官方文档不可达**：`docs.volcengine.com` 相关页面均重定向/空内容；ARK 模型详情在 console SPA 内
- ARK Plan 路由：`https://ark.cn-beijing.volces.com/api/plan/v3`（OpenAI 协议，`use_response_api`），env `VOLCENGINE_PLAN_API_KEY`
- 第三方资料（仅参考）：Doubao Seed 2.1 Pro 支持 256K 上下文、深度思考参数、多模态理解
- 本地实测（本轮）：`VOLCENGINE_PLAN_API_KEY` 有效，`doubao-seed-2-1-pro` 文本与 **base64 图片** 均返回正确结果

## 本轮对齐

| 项 | 变更 |
|---|---|
| 输入模态 | Seed 2.1 Pro / Turbo / Lite / 2.0 Pro 补充 **image**（此前仅 text，实测图片可用） |
| 深度思考 | 新增 reasoning：`none`/`high`，默认 high，on/off 写 `extra_body.thinking.type`（实测 enabled 有 reasoning_content、disabled 无） |
| 上下文 | 保持 **128,000**（ARK Plan 路由现有记录值；无官方文档证据前不改成 256K） |
| 端点 | 保持 ARK Plan OpenAI 路由；`doubao-coding` / `volcengine` 标准路由未使用 |
| 测试 | 新增 `test_doubao_native_parity.py`：Plan provider/endpoint/env、Seed 2.1 条目 context + text/image |

## 真实调用验证（`VOLCENGINE_PLAN_API_KEY`）

| 场景 | 结果 |
|---|---|
| `doubao-seed-2-1-pro` 文本 | ✅ 返回 ok |
| `doubao-seed-2-1-pro` base64 图片 | ✅ "Red" |
| `thinking.type=enabled` | ✅ 返回 ok + `reasoning_content` |
| `thinking.type=disabled` | ✅ 返回 ok，无 reasoning |
| Conductor `create_llm(reasoning_effort=high/none)` | ✅ 正确写 `extra_body.thinking.type` |

## 未完成 / 阻塞

- 官方文档不可达：256K 上下文、深度思考参数（`thinking`）、最大输出、图片约束均无法用官方证据闭环
- 标准 ARK 路由（`/api/v3`）与 AK-SK 鉴权未验证
- Seed 2.1 Turbo / Lite / 2.0 Pro 的模态差异未逐项实测
