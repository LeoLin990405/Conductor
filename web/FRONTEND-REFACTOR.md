# Conductor 前端重构计划（frontend-refactor 分支）

目标：在不改变后端 API/路由契约的前提下，分阶段重构 `web/`（约 35 万行、942 个 tsx），
先清地基与死代码，再拆分巨型模块，最后统一视觉与交互。每阶段独立分支提交，用户验收后再合入 `main`。

## 阶段

| 阶段 | 内容 | 状态 |
|---|---|---|
| Phase 1 | 地基 + 死代码清理（独立分支 `frontend-refactor`） | ✅ 本轮完成 |
| Phase 2 | ChatAgent 巨石拆分（`useChatMessages` ~3.6k 行、`ChatView` ~2k 行）、状态层规范化 | 🚧 进行中 |
| Phase 3 | MarketView 共享模块迁移到 `lib/` 后删除美股行情页/订单相关残留（约 2 万行） | 待做 |
| Phase 4 | 逐页视觉重设计（Dashboard、ChatAgent、Welcome/Setup、Settings、模型选择器、移动端），A股 红涨绿跌、中文优先、统一 token/组件语汇 | 待做 |
| Phase 5 | 全量验收（typecheck、lint、unit tests、vite build、关键页面截图、桌面端加载） | 待做 |

## 目录约定（新代码）

- `src/pages/<Feature>/`：只放路由级页面及其私有组件/hooks；页面之间不互相 import，共享逻辑下沉。
- `src/components/`：跨页面复用的 UI 与业务组件；`src/components/ui/` 只放无业务语义的原子组件。
- `src/lib/`：纯逻辑、数据适配、常量、格式化；不 import `pages/`。
- `src/hooks/`：跨页面 hooks；页面私有 hooks 留在页面目录。
- 样式：优先语义 token（`styles/tokens.css`），页面 CSS 只做布局；禁止硬编码品牌色/涨跌色。
- 新文件尽量 < 400 行；超过即拆（hooks / 子组件 / 纯函数）。

## Phase 1 实际改动（2026-10-23）

- 删除美股订单页 `pages/Orders/`（2,000+ 行）及导航入口、路由 chunk；`/orders` 保留重定向到 Dashboard。
- 删除不可达的自定义 Dashboard：`DashboardCustom.tsx`、`DashboardRouter`、`widgets/definitions/`（39 个文件，
  新闻流/财报/自选/图表等美股数据组件）、`widgets/index.ts`、`widgets/presets.ts`，以及只服务于它的
  widget grid/registry/presets/dialogs/prefs 框架文件；Dashboard 路由直接渲染经典版。
- 精简 `appRoutes`（移除 orders section）、`useNavItems`（不再有需要 gate 的入口）。
- 保留聊天侧仍在用的 widget 上下文框架（`contextSnapshot`、`snapshotSerializers`、`WidgetContextDeck` 等）。
- 测试：删除被删功能自己的测试；调整两处借用已删组件作 fixture 的测试（composer subagents、portfolioSummary），
  typecheck 与 build 通过。

## Phase 2 进展（2026-10-23）

- `ChatView.tsx` 拆出 `chatView/ChatHeader.tsx`：顶栏（返回/工作区标题/加载态/分享/文件面板开关）独立组件，
  props 显式传入；返回动作与标题在 ChatView 计算后下传。ChatView 1,991 → 1,947 行。
- 验证：typecheck、build 通过；真实 thread 打开后顶栏渲染「cn-smoke」、消息与输入框正常（截图 41）。

## Phase 2 进展（续）

- `ChatView.tsx` 拆出 `chatView/ChatRightPanel.tsx`：移动端详情/预览底部抽屉、移动端文件面板覆盖层、
  桌面分栏面板与拖拽分隔线全部独立；FilePanel/DetailPanel/PreviewViewer 的 lazy 声明随之迁移。
  ChatView 1,947 → 1,820 行。
- 验证：typecheck、build 通过；真实 thread 点击文件面板开关，抽取后的面板正常打开（截图 42）。

## Phase 2 进展（续 2）

- `ChatView.tsx` 拆出 `chatView/ChatComposerStack.tsx`：恢复/磁盘提示、模型兜底 pill、ThreadOffers、
  chart-selection chips、TodoDrawer、ChatInput、SubagentStatusBar、重连提示全部独立；props 显式下传。
- lazy 面板模块移到 `chatView/panelModules.ts`（ChatView/ChatRightPanel 共享，且满足 react-refresh 约束）。
- ChatView 1,820 → 1,753 行；清掉拆分后 20+ 个未使用 import；eslint 目标文件只剩 1 条既有 effect 依赖警告。
- 验证：typecheck/build 通过；真实 thread 页面顶栏与 composer 正常渲染（截图 44）。

## Phase 3f 设置页视觉（2026-10-23）

- 设置页头部：标题字距收紧 + 新增副标题（中英文案 `settings.subtitle`）；Tab 栏改为 sticky + 毛玻璃背景。
- 验证：typecheck/build 通过；设置-模型页截图 51（改前）/52（改后）。

## Phase 3 进展（移动端）

- 500px 窄屏走查（headless 最小窗口）：Dashboard 指数卡自动切换为可滑动堆叠 + 页点，自选卡两 Tab 正常，
  底部 tab bar 固定；聊天页 composer、消息与身份行正常，无横向溢出（截图 49/50）。
- 自动化价格提醒的示例代码从 AAPL/SPX 改为 A股代码（601857.SH / 000001.SH）。

## Phase 3 进展（视觉）

- 新增 A股/港股 涨跌色 token：`--color-market-up`（红）/`--color-market-down`（绿）及 soft/border/muted 变体，
  深浅色主题各一套；与 `--color-profit/--color-loss`（也用于成功/危险语义）分离，避免错误提示变绿。
- 已切换行情相关界面：Dashboard 指数卡（价格/涨跌/sparkline）、自选/持仓行与汇总、ChatAgent 内联图表与
  K 线（红涨绿跌）；截图 43 验证 上证 +0.89% 红色、招商银行 -0.12% 绿色。
- 下一轮继续迁移其余行情界面（行情页删除前最后一批、聊天行情卡片文案等）并做页面级视觉重设计。

### Phase 3b Dashboard 视觉重设计（2026-10-23）

- 指数卡：价格字号加大（2xl）、涨跌 chip 化、sparkline 改为带渐变填充的面积图（红/绿随涨跌）、
  卡片 hover 抬升 + 阴影；CN 构建下卡片不再跳转（/market 已移除，避免点击无反应）。
- 页面标题区：新增「A股 / 港股实时指数 · 行情来自腾讯」说明行 + 实时圆点，标题字距收紧。
- 验证：typecheck/build 通过；headless 截图 45（红涨绿跌 + 渐变 sparkline + chip 化涨跌）。

### Phase 3c ChatAgent 视觉打磨（2026-10-23）

- 聊天画布加顶部径向微光，不再是纯平色块；用户气泡加圆角/描边/投影；composer 静置阴影 + 聚焦光晕。
- 验证：typecheck/build 通过；真实 thread 截图 47，消息与输入框渲染正常。

### Phase 3d ChatAgent 身份行（2026-10-23）

- 助手消息增加 Conductor Agent 身份行（琥珀色方块 + 名称，每个助手气泡一次），把原始流式文本变成有明确定义的对话。
- 验证：typecheck/build 通过；真实 thread 截图 48（身份行渲染、无运行时错误）。

## Phase 4c（部分）MarketView → features/market-chart（2026-10-23）

- `web/src/pages/MarketView` 整目录迁移为 `web/src/features/market-chart`（`git mv`，保留历史）；
  全仓 `@/pages/MarketView/...` 引用改为 `@/features/market-chart/...`。
- 迁移后修正跨目录相对引用（ChatAgent/Dashboard/ChatAgent util 等）；目录约定：`features/` 放跨页面能力，
  `pages/` 只放路由页面。MarketView 已无路由（Phase 4b），下一步做可达性剪枝删除页面专属文件。
- 验证：typecheck/build 通过（critical path 363.5 kB gz）。
- 删除无路由引用的页面入口 `features/market-chart/MarketView.tsx` 及其专属移动端 composer 测试。
  其余仅被已删页面使用的面板/搜索/新闻组件留待下一轮可达性剪枝。
- 可达性剪枝：从外部入口（ChatAgent 图表/文件面板、Dashboard、SharedChat）做传递闭包，删除 MarketView 页面专属的
  `MarketSidebarPanel`、`MarketChatPanel`、`MarketDetailDialog`、`MarketChatHistoryButton`、`annotationPrompt`、
  `useRestoredWorkspace` 及其 CSS/测试共 12 个文件。

## Phase 4e 无用 Dashboard 组件删除（2026-10-23）

- 引用分析确认 `AIDailyBriefCard`、`InsightDetailModal`、`Portfolio`、`Watchlist` 已无任何 import（含测试），
  四个文件删除。typecheck/build 通过（critical path 363.0 kB gz）。

## Phase 4d 无用美股卡片删除（2026-10-23）

- Dashboard 移除 US 分支后，`NewsFeedCard`、`EarningsCalendarCard` 已无任何引用，随组件文件删除；
  `AIDailyBriefCard` 仍被 ChatAgent PTCAgentCard 使用，保留。
- typecheck/build 通过。

## Phase 4b（部分）行情页路由下线（2026-10-23）

- `/market` 不再加载 MarketView chunk：路由恒重定向 `/dashboard`；`APP_SECTIONS` 移除 market，
  主导航移除「行情中心」。MarketView 目录暂保留（ChatAgent 仍引用其图表模块，迁移后再删除）。
- 验证：typecheck/build 通过（critical path 363.5 kB gz）；访问 `/market` 落到 Dashboard，无「行情中心」入口。

## Phase 4a 死代码清理（2026-10-23）

- Dashboard 删除不可达的 US 分支（AIDailyBriefCard / NewsFeedCard / EarningsCalendarCard）及其弹窗与状态，
  `Dashboard.tsx` 精简到 ~168 行；移动端底部抽屉里的 EarningsCalendar 一并移除。
- DashboardHeader 删除从未传入的经典/自定义 layoutToggle 与编辑按钮（Custom Dashboard 已在 Phase 1 删除）。
- 验证：typecheck/build 通过（critical path 364.3 kB gz），Dashboard 截图 53 正常，页面不再出现「自定义」。

## 分支验证汇总（2026-10-23，Phase 1–3f 后）

- `tsc -b`、`pnpm build` 每轮均通过；critical path 稳定在 ~364.5 kB gz。
- 本轮对拆分后的核心文件跑 eslint：0 error；仅剩 4 条既有 warning（ChatView/Dashboard/Settings 的 effect 依赖等），
  非本次拆分引入。
- 真实 thread 页面（headless + 后端）：Dashboard / ChatAgent / 设置页 / 窄屏均截图验证，无运行时错误。
- ChatAgent 组件测试在本环境有大量与重构无关的既有失败（中文文案断言、jsdom 计时），Phase 5 统一处理。
- 桌面端：`/Applications/Conductor.app` 进程正常运行（PID 6264），加载本地 dev server（:5173 200 OK），
  后端 :8000 healthy；桌面壳无需重新打包即可验证分支改动。

## 分支总览（Phase 1–4e，19 个 commit）

```
main..frontend-refactor：228 files changed, +1,049 / -23,907
```

- 死代码：Orders、自定义 Dashboard/widget 定义、NewsFeed/Earnings/AIDailyBrief/InsightDetail/Portfolio/Watchlist、
  MarketView 页面入口与专属面板（可达性剪枝）
- 结构：ChatView 拆出 ChatHeader/ChatRightPanel/ChatComposerStack；MarketView → features/market-chart
- 视觉：A股红涨绿跌 token、Dashboard 指数卡、ChatAgent 身份行、设置页、移动端走查
- 每轮：typecheck + build 通过，关键页面 headless 截图；桌面端加载验证通过
- 唯一剩余架构项：`useChatMessages.ts`（3,572 行）拆分；Phase 5 lint/单测基线待收尾

## Phase 2d 进行中（分支 frontend-refactor-2）

- 第一片：`useChatMessages.ts` 抽出 `chatMessages/refusalMessages.ts`（stale fork / busy / resend 三种 notice 文案），
  保持 `useStableHandler` 的稳定身份与最新语言读取；主 hook 3,572 → 3,561 行。typecheck/build 通过。
- 第二片：抽出 `chatMessages/refusalBanner.ts`（429/403/413/errorInfo/busy 的 composer 拒绝对话映射），
  `useChatMessages` 3,561 → 3,517 行。typecheck/build 通过。
- 第三片：抽出 `chatMessages/stopCompaction.ts`（手动 compaction 停止：清状态/丢弃排队消息/取消后端），
  `useChatMessages` 3,517 → 3,504 行。typecheck/build 通过。
- 第四片：抽出 `chatMessages/stopWorkflow.ts`（硬停止：中止 reader、finalize 当前气泡、取消后端并带一次重试、
  归还被停止的 steer + 双停止幂等），`useChatMessages` 3,504 → 3,361 行。typecheck/build 通过。
- 第五片：抽出 `chatMessages/finalizeStreaming.ts`（停止时合成终态事件：主气泡 settle + 子 agent 卡片收尾，
  wasStoppedRef 幂等），`useChatMessages` 3,504 → 3,296 行（含此前片）。typecheck/build 通过，
  并重新用真实线程「发送→停止」验证停止路径正常。

## Phase 2d 计划（useChatMessages 拆分）

`useChatMessages.ts` 3,572 行，全部逻辑在单一 hook 内。按「deps 对象 + 处理器工厂」拆分到
`src/pages/ChatAgent/hooks/chatMessages/`，每次只移一个处理器并跑真实聊天回归：

| 目标文件 | 源函数（当前行号） | 规模 |
|---|---|---|
| `streamFromCheckpoint.ts` | `streamFromCheckpoint` 2927–3572 | ~645 行 |
| `handleSendMessage.ts` | `handleSendMessage` 2238–2631 | ~393 行 |
| `resumeWithHitlResponse.ts` | `resumeWithHitlResponse` 2631–2827 | ~196 行 |
| `handleSendSteering.ts` | `handleSendSteering` 1965–2106 | ~141 行 |
| `stopWorkflow.ts` | `stopWorkflow` / `stopCompaction` 1728–1909 | ~181 行 |

约定：每个工厂接收显式 `ChatMessagesDeps`（refs、setState、queryClient、runtime、callbacks），
hook 只做组装与返回；移动后立即 `tsc -b` + 打开真实 thread 发送/停止/编辑一次验证。

## Phase 5 基线（2026-10-23，需 triage）

- `pnpm exec eslint .`：**0 error / 348 warning**（warning 主要为既有 hooks 依赖、react-refresh 等）。
- `pnpm exec vitest run`：**6544 passed / 1045 failed（713 文件：543 通过 / 170 失败）**。
  **已与 main 基线对比**：main = 6838 passed / 1096 failed（7934 tests，739 文件）。
  分支少 345 个测试（来自已删除功能的测试文件）、失败数反而少 51 个 → **剩余失败全部为 main 上已有的历史失败，
  重构没有引入新的测试失败**；测试 mock 路径扫描也确认没有测试引用已删除模块。Phase 5 单测 triage 完成。
- `tsc -b`、`pnpm build` 均通过。

## Phase 2d-8/9 已合入 main（2026-10-23）

- merge commit **73e8f5fc**：新增 `settleSteeredSend`、`sendSteering` 两个模块，`useChatMessages` 3,173 → 3,025（累计 −547）。
- 合并后 `tsc -b` 通过。
- 剩余仍留在 hook 内的大处理器：`handleSendMessage`、`streamFromCheckpoint`、`resumeWithHitlResponse`（已在计划中，后续按同模式继续）。

## Phase 2d 已合入 main（2026-10-23）

- `frontend-refactor-2` 以 merge commit **51b93d08** 合入 `main`：抽出 7 个职责模块
  （refusalMessages / refusalBanner / stopCompaction / stopWorkflow / finalizeStreaming /
  notRunningQueue / plainSendTracker），`useChatMessages` 3,572 → 3,173 行（-399）。
- 合并后 `tsc -b` / `pnpm build` 通过（critical path 362.3 kB gz）。
- 第九片（分支 `frontend-refactor-3`）：抽出 `chatMessages/sendSteering.ts`（发送 steer：stream 事件、
  not_running 重排队、duplicate/stop 拒绝处理），`useChatMessages` 3,149 → 3,025 行。typecheck/build 通过。
- 第八片（分支 `frontend-refactor-3`）：抽出 `chatMessages/settleSteeredSend.ts`
  （steered send 的 duplicate_request 折叠），`useChatMessages` 3,173 → 3,149 行。typecheck/build 通过。
- 剩余大处理器（`handleSendMessage` / `streamFromCheckpoint` / `resumeWithHitlResponse` /
  `handleSendSteering`）继续按 `chatMessages/` 模式拆分。

## 已合入 main（2026-10-23）

- `frontend-refactor` 以 merge commit **7f59a505** 合入 `main` 并 push（26 个前端重构 commit 保留）。
- 合并范围：253 files changed, +1,090 / -26,396 lines；合并后 `tsc -b` / `pnpm build` 通过。
- 后续剩余项：`useChatMessages.ts` 拆分、既有单测失败 triage（与 main 基线一致，未新增失败）。

## Phase 2d-10：handleSendMessage 抽出（2026-10-23）

- 分支 `frontend-refactor-4`：新增 `chatMessages/sendMessage.ts`（334 行，显式 `SendMessageDeps` 50 项），
  主 hook 调用工厂创建发送处理器；`useChatMessages` 3,024 → **2,745 行**。
- Phase 2d 累计：3,572 → 2,745（**−827 行，10 个职责模块**）。
- 验证：`tsc -b` / `pnpm build` 通过；真实 thread 发送→回复 E2E 正常，无运行时错误。
- 剩余：`streamFromCheckpoint`、`resumeWithHitlResponse` 仍在 hook 内。

## Phase 2e 进展（2026-10-29）

- 从 `useChatMessages.ts` 拆出 HITL resume 流程到
  `hooks/chatMessages/resumeHitl.ts`，用显式 `ResumeHitlDeps` 注入依赖；
  `useChatMessages` 2,744 → 2,564 行。
- 清理拆分后未使用的 import；该文件 eslint 0 error，warning 47 → 28。
- 验证：typecheck/build 通过，unit 7,823 passed，Playwright 143 passed /
  7 skipped / 0 failed。

## 最终验收（2026-10-23，main）

| 项 | 结果 |
|---|---|
| `tsc -b` | 通过 |
| `pnpm build` | 通过（critical path 362.3 kB gz） |
| `eslint .` | **0 error / 363 warning** |
| `vitest run` | **6528 passed / 1050 failed**（7578 tests） |
| 对照重构前 main | 6838 passed / **1096 failed**（7934 tests）→ 失败数下降、无新增失败 |
| 关键页面截图 | Dashboard 55 / ChatAgent 56 / 设置页 57，均无运行时错误 |
| 桌面端 | Conductor.app 运行中并加载 :5173；后端 :8000 healthy |

### 残余（后续可继续，不影响验收）

- `useChatMessages.ts` 仍有 3,025 行；`handleSendMessage`、`streamFromCheckpoint`、
  `resumeWithHitlResponse` 三个大处理器留在 hook 内（已抽出 9 个职责模块，累计 −547 行）。
- 1050 个单测失败为 main 既有历史失败（中文文案断言/jsdom 环境等），非本次重构引入。
- Remaining ESLint warnings 为既有 hooks 依赖与 react-refresh 类提示（0 error）。

## Phase 2f 进展（2026-10-30）

- 从 `useChatMessages.ts` 拆出 retry handler（`chatMessages/retryHandler.ts`）以及
  fork-stream 之外的 send-context 组装逻辑；
- 从 `ChatView.tsx` 拆出 `chatView/sendContext.ts`：附件、skills、swarm、
  market-watch、widget snapshots、chart selection 的组装变成纯函数，便于单测；
- `useChatMessages.ts` 2,374 → 2,356 行，`ChatView.tsx` 1,766 → 1,706 行；
- 更新拉取新 UI 后过期的 Dashboard/model-menu E2E 定位：Dashboard 标签、
  model menu 的 thread composer 路由、移除已删除的 mobile FAB 用例；
- 验证：typecheck/build 通过，unit 7,822 passed，定向 E2E 9 passed / 1 skipped。

## Phase 2g 进展（2026-10-30）

- 从 `ChatView.tsx` 拆出 initialMessage 自动发送 effect 到
  `chatView/useInitialMessageSend.ts`，包含 skill merge、swarm directive、
  handoff 立即发送与普通延迟发送；新增 4 条 hook 测试；
- `ChatView.tsx` 1,706 → 1,633 行；
- focus-ring 的 Tab 预算 40 → 80，消除新 UI tab 顺序变长后的偶发失败；
- 验证：typecheck/build 通过，unit 7,828 passed，focus-ring 28 passed。

## Phase 2h 进展（2026-10-30）

- 从 `ChatView.tsx` 拆出消息文本选择 tooltip 逻辑到
  `chatView/useMessageSelectionTooltip.ts`：选区定位、Add to context、
  mousedown 清理；
- `ChatView.tsx` 1,633 → 1,573 行；
- 验证：typecheck/build 通过，unit 7,828 passed，Playwright 142 passed /
  8 skipped。

## Phase 2i 进展（2026-10-30）

- 从 `ChatView.tsx` 拆出文件下载与 dispatcher 跳转逻辑到
  `chatView/useFileActions.ts`；
- `ChatView.tsx` 1,573 → 1,532 行；
- 验证：typecheck/build 通过，unit 7,831 passed，Playwright 142 passed /
  8 skipped。

## Phase 2j 进展（2026-10-30）

- 从 `useChatMessages.ts` 拆出 conversation reload 与 older-page 加载到
  `chatMessages/historyLoading.ts`，显式注入 history window、runtime、
  feedback、fork refusal 等依赖；
- `useChatMessages.ts` 2,356 → 2,300 行；
- 验证：typecheck/build 通过，unit 7,831 passed，Playwright 142 passed /
  8 skipped。

## Phase 2k 进展（2026-10-30）

- 从 `useChatMessages.ts` 抽出最大剩余块 `streamFromCheckpoint` 到
  `chatMessages/forkStream.ts`，保留原协议：checkpoint fork/retry、SSE
  processor、admission、steer settle、retry notice、abort/finally；
- `settleRetry` 与 stream 互相引用，hook 用 holder 打破初始化环；body 不变；
- `useChatMessages.ts` 2,300 → 2,068 行；`forkStream.ts` 355 行；
- 验证：typecheck/build 通过，unit 7,831 passed，Playwright 142 passed /
  8 skipped。

## Phase 2l 进展（2026-10-30）

- 从 `useChatMessages.ts` 拆出 duplicate-steered 409 结算到
  `chatMessages/settleDuplicateSteer.ts`，含两处修复的回归测试；
- `useChatMessages.ts` 2,066 → 2,016 行；
- 验证：typecheck/build 通过，unit 7,833 passed，Playwright 142 passed /
  8 skipped。

### SSEEvent canonicalization finding（2026-10-30）

直接把 local `SSEEvent` 别名成 `src/types/sse.ts` 的 canonical union +
`_replay` 会产生 **895 个 type errors**：processor handlers 依赖 merged
envelope（同一帧上同时读 content/tool_calls/payload/phase 等），canonical
union 必须先按 `event` 判别收窄。下一步应做的是：

1. 给 processor 增加按 `event` 分派的窄化入口；
2. 每个 handler 使用对应 canonical event 类型；
3. 最后删除 local loose interface，而不是先删。

## Phase 2m 进展（2026-10-30）

- 从 `useChatMessages.ts` 抽出 Thread history load/reconnect bootstrap
  （约 310 行）到 `chatMessages/historyBootstrap.ts`：workflow status、
  replay、steer/mux 接管、reconnect loop、interrupt 恢复、cleanup；
- 调用点放在 `streamRouterDeps`/mux controller 之后，保持依赖初始化顺序；
- `useChatMessages.ts` 2,016 → 1,709 行；
- 验证：typecheck/build 通过，unit 7,833 passed，Playwright 142 passed /
  8 skipped。

## Phase 2n 进展（2026-10-30）

- 从 `useChatMessages.ts` 拆出 handler-refs builder 到
  `chatMessages/streamRefs.ts`，新增默认/reconnect 两条测试；
- `useChatMessages.ts` 1,709 → 1,682 行；
- focus-ring 的 park-and-return case 加 50ms settle + 10s poll，消除
  偶发 `style: none`；
- 验证：typecheck/build 通过，unit 7,835 passed，Playwright 142 passed /
  8 skipped。

## Phase 2o / SSE 进展（2026-10-30）

- `src/types/sse.ts` 补齐 live/replay 的 internal event 类型：`run_end`、
  `context_window`、`compaction_chunk`、`workspace_status`、
  `market_watch_update`、`retry`、`steering_accepted`、`steering_returned`、
  `workflow_lifecycle`，并加入 discriminated union；
- `wireEvents.ts` 新增 `asRunEndEvent` / `asWorkspaceStatusEvent`；
- `processStreamEvent` 的 run_end 与 workspace_status 分支改为使用 canonical
  窄化类型；
- 验证：typecheck/build 通过，unit 7,837 passed，Playwright 142 passed /
  8 skipped。

## Phase 2p / SSE 进展（2026-10-30）

- `wireEvents.ts` 新增 `asToolCallsEvent` / `asToolCallResultEvent`；
- `processStreamEvent` 的 main tool_calls / tool_call_result 分支改为
  canonical 窄化类型，移除 tool_calls 的 `as unknown` 数组转换和 result
  分支的 loose 字段读取；
- 新增 tool guard 测试；
- 验证：typecheck/build 通过，unit 7,839 passed，Playwright 142 passed /
  8 skipped。

## Phase 2q 进展（2026-10-30）

- 从 `useChatMessages.ts` 拆出 settleEditDrafts 到
  `chatMessages/settleEditDrafts.ts`，新增 2 条测试；
- `useChatMessages.ts` 1,682 → 1,654 行；
- focus-ring 的 park-and-return describe 加 1 次 retry，吸收合成焦点事件的
  绘制时序；
- 验证：typecheck/build 通过，unit 7,841 passed，Playwright 142 passed /
  8 skipped。

## Phase 2r / SSE 进展（2026-10-30）

- `wireEvents.ts` 新增 `asContextWindowEvent`；
- `handleContextWindowEvent` 的入参从 loose `SSEEvent` 改为 canonical
  `ContextWindowEvent`；`applyCallUsage` 收窄为 usage 字段 Pick；
- live processor 与 history replay 都通过 guard 进入该 handler；
- 新增 context guard 测试；
- 验证：typecheck/build 通过，unit 7,842 passed，Playwright 141 passed +
  1 flaky retried / 8 skipped。

## Phase 2s / SSE 进展（2026-10-30）

- `ErrorEvent` 补齐 recovery / run_id / code / retry 字段与 index 扩展；
- 新增 `asErrorEvent`，live processor 的 error 分支改用 canonical 类型；
- focus-ring 文件级配置 1 次 retry（合成 focus/paint 时序），keyboard-focus
  用例改为 poll；
- 验证：typecheck/build 通过，unit 7,843 passed，Playwright 142 passed /
  8 skipped。

## Phase 2t / SSE 进展（2026-10-30）

- `MessageChunkEvent` 补齐 content_type / elapsed_ms；
- 新增 `asMessageChunkEvent`；
- main 与 subagent 两条 message_chunk 分支改用 canonical 类型，覆盖
  text / reasoning / reasoning_signal；
- 新增 chunk guard 测试；
- 验证：typecheck/build 通过，unit 7,844 passed，Playwright 142 passed /
  8 skipped。

## Phase 2u / SSE 进展（2026-10-30）

- 新增 `asProvenanceEvent` / `asToolCallChunksEvent`；
- provenance 主分支与 tool_call_chunks 的 main/subagent 分支改用 canonical
  类型，移除 `as unknown` 数组转换；
- 新增 2 组 guard 测试；
- 验证：typecheck/build 通过，unit 7,846 passed，Playwright 142 passed
  （1 flaky retried）/ 8 skipped。

## Phase 2v / SSE 进展（2026-10-30）

- `MetadataEvent` 补上 report_source；新增 `asMetadataEvent`（兼容无 run_id 的
  旧帧）；
- main metadata 分支改用 canonical 类型读取 run_id / turn_index /
  report_source；
- 新增 metadata guard 测试；
- 验证：typecheck/build 通过，unit 7,847 passed，Playwright 142 passed /
  8 skipped。

## Phase 2w / SSE 进展（2026-10-30）

- 新增 `asArtifactEvent`；main 与 subagent artifact 分支改用 canonical
  `ArtifactEvent` 的 artifact_type / artifact_id / payload；
- 新增 artifact guard 测试；
- 验证：typecheck/build 通过，unit 7,848 passed，Playwright 142 passed
  （1 flaky retried）/ 8 skipped。

## Phase 2x 进展（2026-10-30）

- 从 `useChatMessages.ts` 拆出 online 重试与 unmount session reset 到
  `chatMessages/historyLifecycle.ts`，新增 2 条 hook 测试；
- `useChatMessages.ts` 1,654 → 1,621 行；
- 验证：typecheck/build 通过，unit 7,850 passed，Playwright 142 passed /
  8 skipped。

## Phase 2y / SSE 进展（2026-10-30）

- 新增 `asSteeringAcceptedEvent` / `asSteeringDeliveredEvent`；
- main steering_accepted / steering_delivered 分支改用 canonical 类型；
- 新增 steering guard 测试；
- 验证：typecheck/build 通过，unit 7,851 passed，Playwright 142 passed /
  8 skipped。

## Phase 2z / SSE 进展（2026-10-30）

- 新增 `asWorkflowLifecycleEvent`；
- subagent tool_calls / tool_call_result / workflow_lifecycle 分支改用
  canonical guard，移除数组/字段强转；
- 新增 workflow guard 测试；
- 验证：typecheck/build 通过，unit 7,852 passed，Playwright 142 passed /
  8 skipped。

## Phase 2aa / SSE 进展（2026-10-30）

- 新增 `asModelRetryEvent` / `asModelFallbackEvent` /
  `asMarketWatchUpdateEvent`；
- main model_retry / model_fallback / market_watch_update 分支改用
  canonical 类型；
- MarketWatchUpdateEvent 补齐 symbols/content/timestamp；
- 新增 guard 测试；
- 验证：typecheck/build 通过，unit 7,853 passed，Playwright 142 passed /
  8 skipped。

## Phase 2ab 进展（2026-10-30）

- 从 `useChatMessages.ts` 拆出 threadId 同步、tab suspend/recovery、thread
  switch reset 到 `hooks/useThreadSessionSync.ts`；
- 用 lazy ref proxy 保持 turnCheckpointsRef 的声明顺序安全；
- `useChatMessages.ts` 1,621 → 1,516 行；新增 1 条 hook 测试；
- 验证：typecheck/build 通过，unit 7,854 passed，Playwright 142 passed
  （1 flaky retried）/ 8 skipped。

## Phase 2ac 进展（2026-10-30）

- 从 `useChatMessages.ts` 拆出 compaction 后 queued send flush 与
  not_running steer flush 到 `chatMessages/queuedSends.ts`；
- 新增 3 条 hook 测试（compaction 发送、refused steer 发送、in-flight 阻塞）；
- `useChatMessages.ts` 1,516 → 1,471 行；
- 验证：typecheck/build 通过，unit 7,857 passed，Playwright 142 passed /
  8 skipped。

## Phase 2ad / SSE 进展（2026-10-30）

- `UserMessageEvent` 补齐 run_id / run_completed_at / turn_index；
- 新增 `asUserMessageEvent`（允许空 content 的 HITL resume 对）；
- live subagent 与 history replay 的 user_message 分支改用 canonical 类型；
- 新增 guard 测试；
- 验证：typecheck/build 通过，unit 7,858 passed，Playwright 142 passed /
  8 skipped。

## Phase 2ae 进展（2026-10-30）

- 从 `useChatMessages.ts` 拆出 409 duplicate_request adoption / failed-copy
  retry 到 `chatMessages/duplicateRun.ts`，新增 3 条测试；
- streamFromCheckpoint 通过 lazy wrapper 注入，避免声明顺序问题；
- `useChatMessages.ts` 1,471 → 1,427 行；
- 验证：typecheck/build 通过，unit 7,861 passed，Playwright 142 passed /
  8 skipped。

## Phase 2af / SSE 进展（2026-10-30）

- `RetryEvent` 补齐 run_id；新增 `asRetryEvent`；
- main retry 分支改用 canonical 类型；
- 新增 retry guard 测试；
- 验证：typecheck/build 通过，unit 7,862 passed，Playwright 142 passed /
  8 skipped。

## Phase 2ag / SSE 进展（2026-10-30）

- 新增 `asCompactionChunkEvent` / `asSteeringReturnedEvent`；
- `SteeringReturnedEvent` 补齐 task lane 的 input_id / messages；
- subagent steering_returned 分支改用 canonical 类型；
- 新增 guard 测试；
- 验证：typecheck/build 通过，unit 7,863 passed，Playwright 142 passed /
  8 skipped。

## Phase 2ah / SSE loose interface 移除（2026-10-30）

- 删除 `session/types.ts` 的 merged loose `SSEEvent` interface；
- 本地类型改为 canonical discriminated union + replay/mux extension：

  ```ts
  export type SSEEvent = WireSSEEvent & {
    _replay?: boolean;
    [key: string]: any;
  };
  ```

- canonical union 补齐 internal families：`history_page`、`replay_done`、
  `credit_usage`、`token_usage`、`caught_up`、
  `subagent_followup_injected`、`turn_start`；
- BaseSSEEvent 补 `thread_id`；ArtifactEvent payload 收紧为 Record；
- transport/API 回调边界暂时用 `any` 接住 canonical/legacy frame；
- 删除 replayHistory 中不可达的重复 replay_done 分支；
- 验证：typecheck/build 通过，unit 7,863 passed，Playwright 142 passed /
  8 skipped。

## 验收命令

```bash
cd web
pnpm exec tsc -b
pnpm build
pnpm exec vitest run src/pages/Dashboard src/components/nav
```
