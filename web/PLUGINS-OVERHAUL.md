# 插件页完全优化（第一轮）

## 结构

- `/plugins` 仍由 shell（页头 + 侧栏 rail）与六个 section 组成，URL 即状态（`?tab=` / `?view=discover` / `?q=` / `?detail=`）。

## 视觉

- Rail：分组标签改为 kicker 小标题；选中项为香槟金软底 pill + 金色图标；计数改为 pill 徽标；内置折叠保留。
- 页头：标题层级与全站统一；搜索框改用 elevated 表面 + 金色 focus ring；添加按钮与 Yours/Discover 切换统一圆角。
- 列表：内容区从平铺行升级为卡片网格（hairline 边框、12px 圆角、hover 抬升与柔和投影），section 头更轻，筛选 chip 选中态用金色软底。
- 空状态文案修正：区分「自定义插件」与「内置插件」，不再出现「还没有安装插件」却显示 8 个内置的歧义。
- 移动端：rail 变金色选中 pill 横向滚动，卡片单列，底部安全间距。

## 性能

- 六个 section 全部按需加载（PluginsList / McpServers / SkillsList / SubagentsList / Brokerages / PluginSecrets），
  打开插件页只加载 shell；已确认构建产物各自独立 chunk。
- Rail hover / focus 时预取对应 chunk，点击时不再闪 skeleton。
- entry critical path 不变（363.4 kB gz）。

## 交互

- `/` 聚焦当前 section 搜索框（全站快捷键一致），Esc 清空。
- 保留原有：搜索写 URL、清除按钮、Yours/Discover、添加菜单、内置折叠、键盘可达。

## 验收

- `tsc -b` / `pnpm build` 通过
- 截图：68（改前）、69（插件列表）、70（连接器）、71（移动端）
- 页面无运行时错误；各 section 的既有测试路径未改动
