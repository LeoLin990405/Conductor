# 视频输入管线（Video Input）

**状态**：已实现，并以 StepFun `step-5-preview` / `step-3.7-flash` 做了真实调用验证（2026-10-26）。

Conductor 的 Read 工具此前只支持图片/PDF。本管线把视频纳入同一条链路：
**Read 拦截 → 结构性识别 → OpenAI 兼容 `video_url` 块 → 挂在 ToolMessage 上 → 每轮按目标模型能力剥离**。

改动文件：

| 文件 | 作用 |
|---|---|
| `src/ptc_agent/agent/tools/file_ops.py` | Read 工具把视频扩展名路由到"视觉"分支（返回 `Loading video:` 哨兵），否则会走二进制文件拒绝路径 |
| `src/ptc_agent/agent/middleware/file_operations/multimodal.py` | 视频容器识别、大小上限、`video_url` 块构造 |
| `src/ptc_agent/agent/middleware/file_operations/multimodal_strip.py` | 读侧按目标模型的模态**和协议**保留/替换视频块 |
| `src/llms/attachment_payload.py` | `video_url` 纳入 attachment 判定，避免工具结果被字符串化 |
| `src/ptc_agent/agent/middleware/compaction/offloading.py`、`.../compaction/utils.py` | 压缩摘要前把视频 base64 落盘/替换为占位，避免整段 base64 进入摘要 prompt |
| `src/llms/manifest/models.json` | 仅 `step-5-preview`、`step-3.7-flash` 的 `input_modalities` 增加 `video` |
| `tests/unit/ptc_agent/middleware/file_operations/test_multimodal_video.py` | 新增 48 个测试 |
| `tests/unit/llms/test_stepfun_native_parity.py` | 模态断言更新为 `["text", "image", "video"]` |

## 1. 支持的格式与识别方式

不信任扩展名，**用字节结构判定**（URL 可以没有扩展名，沙箱文件名只代表写入方声称的格式）：

| 扩展名 | 容器 | 结构判定 | MIME |
|---|---|---|---|
| `.mp4` | ISO BMFF | `ftyp` box + 视频品牌（`isom/iso2/mp41/avc1/…`）或 `moov` 内存在 `hdlr` = `vide` 的轨道 | `video/mp4` |
| `.mov` | ISO BMFF / QuickTime | `ftyp` major brand = `qt  ` + 视频轨道 | `video/quicktime` |
| `.webm` | EBML | 魔数 `1A 45 DF A3` + EBML DocType = `webm` | `video/webm` |
| `.mkv` | Matroska | 魔数 `1A 45 DF A3` + EBML DocType = `matroska` | `video/x-matroska` |

拒绝（给模型可读的 `ERROR:` 文本，且**不写入 checkpoint**）：

- `ftyp` 但品牌是 AVIF/HEIF 等静态图（`ftyp` 不是视频专属魔数）；
- 截断视频（只有 `ftyp`、缺少带视频轨道的 `moov`）——否则会变成每轮重放的 400；
- 未知 EBML DocType、随机字节、伪装成 `.mp4` 的其他格式：
  `ERROR: Content is not a readable image, PDF or video: <path>`。

## 2. 写入侧：`video_url` 块

```python
[
    {"type": "text", "text": "[Viewing video: clip.mp4]"},
    {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,<...>"}},
]
```

- 与图片/PDF 一样**挂在 Read 的 ToolMessage 上**，而不是另起一条 HumanMessage（并行批次、压缩切点都不会把附件和产生它的调用拆开）。
- 始终 inline data URI：部分 provider 根本不能抓外链，且抓不到私有 bucket。
- 该块结构经真实调用验证被 StepFun Chat Completions 接受（见 §5）；同一 shape 也是 OpenAI 兼容协议下 `video_url` 的标准写法。

### 大小上限

| 常量 | 值 | 说明 |
|---|---|---|
| `MultimodalMiddleware.MAX_CONTENT_BYTES` | 5 MiB | 图片/PDF，沿用 Anthropic 单图上限 |
| `MultimodalMiddleware.MAX_VIDEO_BYTES` | 10 MiB | 视频专属。真正先到的是 **inline 请求上限**：Gemini inline 请求 20 MiB 上限，10 MiB 视频 base64 后约 13.3 MiB，留出提示词空间。StepFun 对单个 MP4 允许 128 MiB，但那是服务端下载上限，不是请求体上限 |
| URL 流式粗上限 | `max(两者)` | 字节未定型前不能按 5 MiB 拒绝合法视频；定型后 `_attach_bytes` 再按类型二次校验 |

超过上限 → `ERROR: Content is larger than the 10MB limit and was not loaded: <path>`，不产生内容块。

## 3. 读取侧：按能力**和协议**剥离

`MultimodalStripMiddleware` 每轮按请求上真正的目标模型（manifest_model 戳）判断：

- `input_modalities` 不含 `video` → 替换为占位文本
  `[Video attached in a prior turn, not visible to the current model. <guidance>]`；
- 含 `video` **且**目标客户端协议能序列化 `video_url`（`sdk in {"openai"}`）→ 保留；
- 含 `video` 但协议不同（Gemini/MiniMax）→ 仍然替换为占位。

最后一条是必要的：**能力声明 ≠ 线协议**。实测 `langchain_google_genai` 的转换器遇到 `video_url` 会直接
`ValueError: Unrecognized message part type: video_url`，MiniMax 走 Anthropic 协议同样不接受该块——
发出去比占位更糟（会 400/崩溃且被 checkpoint 重放）。这两个模型的 `video` 声明保留，等各自的
协议转换器落地后把对应 sdk 加入 `_VIDEO_URL_SDKS` 即可。

目标 → 实际行为：

| 目标模型 | manifest 模态 | SDK | 视频块 |
|---|---|---|---|
| `step-5-preview` / `step-3.7-flash` | text, image, **video** | openai | 保留 ✅ 已真实调用 |
| 用户自定义模型 + 偏好里勾选 video | 自定义（含 video） | openai（custom） | 保留（用户声明即 OpenAI 兼容契约） |
| `gemini-4-argon` 等 | 声明 video | gemini | 替换为占位（协议未实现） |
| `minimax-m3` / `minimax-m3.1-flash` 等 | 声明 video | anthropic | 替换为占位（协议未实现） |
| 文本模型（如 `glm-5.2`） | text | — | 替换为占位 |

配套的横切修正：

- `attachment_payload.is_attachment_payload` 现在认识 `video_url`，否则 `ToolResultNormalizationMiddleware` 会把整个 block list `json.dumps` 成一段 base64 散文；
- 压缩路径 `aoffload_base64_content` 会把视频 data URI 落盘并替换为
  `[Video saved to <path> — use Read to view]`；无 sandbox 时退化为 `[Video]`；
  token 计数按 `[video]` 计入。否则 `get_buffer_string` 会把整段 base64 渲染进摘要 prompt。

## 4. manifest 变更

只改两个模型，其余模型保持原状：

```json
"step-5-preview":  "input_modalities": ["text", "image", "video"]
"step-3.7-flash":  "input_modalities": ["text", "image", "video"]
```

`tests/unit/llms/test_stepfun_native_parity.py` 同步更新，并断言 `step-3.5-flash` 仍为 text-only。

## 5. 真实调用验证（StepFun）

环境：仓库 `.env` 有 `STEPFUN_PLAN_API_KEY`；provider `stepfun-plan` 走 OpenAI 兼容
`https://api.stepfun.com/step_plan/v1`（`sdk: openai`）。官方文档：

- <https://platform.stepfun.ai/docs/en/guides/models/step-5-preview.md>
- <https://platform.stepfun.ai/docs/en/guides/developer/video-chat.md>（`video_url` 块、Base64 data URI）
- <https://platform.stepfun.ai/docs/en/api-reference/chat/chat-completion-create.md>

### 生成 ~1.2s 测试视频

```bash
ffmpeg -hide_banner -loglevel error \
  -f lavfi -i testsrc=duration=1.2:size=160x120:rate=6 \
  -pix_fmt yuv420p -c:v libx264 -y /tmp/video-check/clip.mp4
# 5894 bytes：ftyp(isom/iso2/avc1/mp41) + free + mdat + moov（moov 在尾部）
```

### 验证脚本

走仓库真实路径：`create_llm("step-5-preview")`（真实 manifest + OpenAI 兼容客户端），并用
`MultimodalMiddleware._handle_sandbox_content` 生成与线上完全一致的 Read ToolMessage：

```bash
cd /Users/you/.zcode/workspace/langconductor
.venv/bin/python - <<'PY'
import asyncio, base64, sys
from pathlib import Path

REPO = Path.cwd(); sys.path.insert(0, str(REPO))
from dotenv import load_dotenv; load_dotenv(REPO / ".env")
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from ptc_agent.agent.middleware.file_operations.multimodal import MultimodalMiddleware
from src.llms.llm import create_llm

raw = Path("/tmp/video-check/clip.mp4").read_bytes()

class _Sandbox:  # 只实现 middleware 的沙箱读取面
    def normalize_path(self, p): return f"/home/workspace/{p.lstrip('/')}"
    async def adownload_file_bytes(self, p): return raw

async def build_read_result():
    mw = MultimodalMiddleware(sandbox=_Sandbox())
    return await mw._handle_sandbox_content(
        "clip.mp4",
        ToolMessage(content="Loading video: clip.mp4", tool_call_id="call-read-1"),
        "call-read-1",
    )

async def main():
    read_result = await build_read_result()
    print("block types:", [b.get("type") for b in read_result.content])
    url = read_result.content[1]["video_url"]["url"]
    print("data URI:", url[:32] + "...", "len", len(url))  # 不打印 base64

    for name in ("step-5-preview", "step-3.7-flash"):
        llm = create_llm(name)
        # A. 文档形态：user 消息里的 video_url
        a = await llm.ainvoke([HumanMessage(content=[
            *read_result.content,
            {"type": "text", "text": "What is in this video? Answer in one short sentence."},
        ])])
        print(f"[{name} A]", a.content.strip()[:200], a.usage_metadata)
        # B. 管线形态：Read 的 ToolMessage 携带视频块
        b = await llm.ainvoke([
            HumanMessage(content="Read the video file, then describe it."),
            AIMessage(content="", tool_calls=[{
                "name": "Read", "args": {"file_path": "clip.mp4"},
                "id": "call-read-1", "type": "tool_call"}]),
            read_result,
            HumanMessage(content="What did you see? One short sentence."),
        ])
        print(f"[{name} B]", b.content.strip()[:200], b.usage_metadata)

asyncio.run(main())
PY
```

### 实际返回（2026-10-26）

```
[setup] ffmpeg clip: /tmp/video-check/clip.mp4 (5894 bytes, base64 7860 chars)
[setup] middleware Read result block types: ['text', 'video_url']
[setup] video_url prefix: data:video/mp4;base64,AAAAIGZ0eX... (data URI length 7882)

===== step-5-preview =====
[A user-message video_url]
  'The video shows a television color bar test pattern (SMPTE-style color bars) with
   frame/timecode text overlay.'
  usage: input=234 output=98 total=332
[B Read tool-result video]
  'The video shows a single frame of a colorful SMPTE-style color bar test pattern with
   a small white "0" in the corner and partially visible debug-style text along the bottom.'
  usage: input=285 output=207 total=492

===== step-3.7-flash =====
[A user-message video_url]
  'A pixelated video frame displays a color calibration chart with vertical stripes and
   a horizontal rainbow bar, along with text at the bottom.'
  usage: input=235 output=846 total=1081
[B Read tool-result video]
  'A SMPTE color bars test pattern with a digital counter and timestamp text overlay.'
  usage: input=284 output=130 total=414
```

结论：两个 StepFun 模型都接受 **inline Base64 data URI**，也接受视频块出现在 **tool 角色**的
Read 结果里（与图片块的既有路径一致）。日志/文档中不包含 base64。

### 负面对照：文本模型确实会被 strip 保护

对 `step-3.5-flash`（manifest 只声明 text）直接发同样的视频块：

```
step-3.5-flash modalities: ['text']
rejected as expected: OpenAIInvalidRequestError Error code: 400 -
{'error': {'message': 'The model you requested doesnt support video input.',
           'type': 'model_incompatible'}}
```

这正是读侧 strip 每轮把不支持目标的视频块换成占位文本所要避免的 400。

## 6. 测试

```bash
# 任务要求的两组
.venv/bin/python -m pytest -q tests/unit/ptc_agent/middleware/file_operations tests/unit/llms
# → 1158 passed, 13 skipped

# 新增的视频专项
.venv/bin/python -m pytest -q tests/unit/ptc_agent/middleware/file_operations/test_multimodal_video.py
# → 48 passed

# 相邻回归（Read 工具、多模态、压缩）
.venv/bin/python -m pytest -q tests/unit/ptc_agent/middleware \
  tests/unit/ptc_agent/agent/tools/test_read_tool.py tests/unit/middleware/compaction
# → 469 passed
```

覆盖点：

- 容器识别：mp4/mov/webm/mkv、无品牌但有 `moov` 视频轨道、AVIF 不误判、截断 `ftyp`、未知 EBML DocType、PNG/PDF 不受影响；
- Read 工具路由：`.mp4` 返回 `Loading video:` 哨兵，且与 middleware 的扩展名集合一致；
- block 构造：`video_url` + data URI、MIME 进入 data URI、被认作 attachment payload；
- 写入路径：沙箱视频、无扩展名 URL、文件扩展名与真实字节不一致、图片路径不回归；
- 大小/非法输入：视频独立上限、视频上限不放松图片上限、介于两个上限之间的视频可通过、伪造 mp4 给可读 ERROR、截断视频不写 checkpoint；
- 读侧 strip：无 video 模态即占位、video+openai sdk 保留、Gemini/MiniMax 声明 video 但协议不符仍占位、自定义模型勾选 video 保留、guidance 只出现在第一个占位、checkpoint 不被修改；
- 压缩：video data URI 被识别并落盘为 `[Video saved to …]`、无后端退化为 `[Video]`、token 计 `[video]`。

## 7. 已知限制

1. **只有 StepFun 做了真实调用验证。** 仅实现了 OpenAI 兼容 `video_url` 协议；Gemini/MiniMax 虽然
   manifest 声明 video，但读侧协议门控会把视频块替换为占位，等对应协议转换器 + 真实调用后再放开。
2. **只支持 inline Base64。** 没有 URL 模式或 StepFun `stepfile://` Files API 模式；上限 10 MiB，
   更大的视频需要先切分/抽帧（官方建议 MP4 <128 MiB、<5min，但那是服务端下载限制）。
3. **不做压缩/抽帧。** 视频按原样发送，token 成本由 provider 按解码策略计算；长视频更贵。
4. **前端上传的视频附件未接管。** 聊天里的用户上传走 `src/server/utils/multimodal_context.py`
   （目前 image/pdf/file），本次只覆盖 Agent 用 Read 读工作区视频；前端不在本次改动范围。
5. **压缩后 Read 回视频**：offload 后历史里留下 `[Video saved to <path> — use Read to view]`，
   再次 Read 该路径会重新注入视频块（受沙箱/大小上限约束），这是预期行为。
