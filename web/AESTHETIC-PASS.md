# 前端审美重构（第一轮）：克制的专业金融风

## 方向

- 主色从暖橙改为**香槟金**（`#E9954A` → 语义 accent `hsl(34 58% 64%)`），降低饱和度，作为品牌与选中态的强调色。
- 页面底色从纯黑改为**深墨蓝灰**（`hsl(218 30% 8%)`），卡片 `hsl(218 28% 11%)`，并叠加极轻的冷色/金色径向光晕。
- 文本层级更冷、更清楚：主文字 `#E8EEF5`，次级 `#A7B3C1`，三级/四级透明度收窄。
- 边框统一为冷白 hairline（`rgba(228,236,247,0.075)`），取消厚重描边。

## 统一语汇（`styles/base.css`）

| 类 | 用途 |
|---|---|
| `.ui-card` | 卡片标准表面：hairline 边框 + 顶部高光渐变 + 柔和投影 + 16px 圆角 |
| `.ui-card-hover` | 可交互卡片的 hover：表面提亮、边框升级、投影加深 |
| `.ui-elevated` | 浮层/菜单表面（弹层、下拉） |
| `.ui-page-title` | 页面主标题：1.375rem / 600 / -0.02em |
| `.ui-kicker` | 标题上下的全大写小标签 |
| `.ui-num` | 价格/涨跌数字的 tabular-nums |
| `.ui-hairline` | 分隔线 |

## 已覆盖

- **Dashboard**：指数卡、自选股卡、AI 洞察/市场快讯卡、页面标题层级。
- **Sidebar**：墨蓝底 + 顶部微光、导航选中态改为香槟金 pill + 细指示条、账户行 hairline hover。
- **Chat**：输入框表面（顶部高光 + 更深投影 + 香槟金 focus ring）、设置页吸顶 Tab（毛玻璃 + 底部 hairline）。
- **全站**：字体渲染（antialiased/optimizeLegibility）、选区色、滚动条、focus-visible 统一。
- **Light 主题**：保留暖纸风格，沿用原 accent 体系（仅暗色主色变化；浅色下未改）。

## 验收

- `tsc -b` / `pnpm build` 通过（critical path 363.1 kB gz）
- 截图：63 桌面 Dashboard / 64 桌面 Chat / 65 桌面设置 / 66 移动端 Dashboard / 67 移动端 Chat
- 后续可继续：聊天气泡层次、Setup/登录页细节、模型选择器弹层阴影、空态插画。
