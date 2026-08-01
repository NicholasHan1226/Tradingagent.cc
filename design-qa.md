# TradingCopilot 个股工作台 Design QA

## 对照证据

- source visual truth: `/var/folders/gg/h6vhh_j50tvg5x4ktqwgxy4r0000gn/T/TemporaryItems/NSIRD_screencaptureui_7weerO/截屏2026-08-02 02.19.50.png`
- implementation desktop: `/tmp/trading-copilot-desktop.png`
- implementation mobile: `/tmp/trading-copilot-mobile.png`
- desktop viewport: `1660 x 770` CSS px，device scale factor `1`
- source pixels: `1660 x 770`; implementation desktop pixels: `1660 x 770`; no density normalization required
- mobile viewport/pixels: `390 x 844` CSS/pixels，device scale factor `1`
- state: `?product=copilot`，`demo_fixture`，`000400.SZ`，概述，`1D`，显示预测

## Full-view comparison

同一视觉输入中对照了源图、桌面实现和移动实现。桌面实现保留了源图的七项详情导航、双段报价信息、周期切换、深色行情画布、价格线、成交量和低干扰边框体系；同时保留 TradingCopilot 既有的关注列表、账户摘要和人工决策栏。A 股语义按红涨绿跌实现，预测区域以虚线中位情景和分层区间替代源产品的盘后区域。

源图是单个图表模块裁切，而实现截图包含完整产品壳层；因此只判断相同的个股详情区域，不把应用壳层差异作为偏差。核心区域的信息密度、层级、暗色基调和控件节奏达到同类金融终端的视觉目标。

## Focused-region comparison

重点放大核对了个股详情导航、报价条、周期控件、行情线、预测区间与成交量区域。没有使用商标、Perplexity 标识或其专有资产；图标来自项目现有 Lucide 依赖。源图没有需要复用的摄影、插画或产品图片，因此不存在低清资产或伪造图片问题。

## Required fidelity surfaces

- Fonts and typography: 延续项目现有中文系统字体栈；标题、报价、辅助数据形成清晰三级层级，小号元数据保持可读，没有明显截断或错误换行。
- Spacing and layout rhythm: 桌面三栏比例、个股内容留白、卡片边界和导航节奏稳定；移动端改为单列并保留横向周期控件，无水平溢出。
- Colors and tokens: 黑蓝底、低对比描边、青色品牌强调与源图的安静金融终端气质一致；交易语义按 A 股红涨绿跌做了有意适配。
- Image quality and assets: 无摄影或插画资产；图表由矢量绘制，线条与文字在 1x 截图中清晰；图标均使用现有图标库。
- Copy and content: 文案明确标注“演示数据”“研究情景 · 未校准”“只用于交互验收”，没有把情景权重表述为真实概率，也没有暗示自动下单。

## Findings

当前没有可执行的 P0/P1/P2 视觉问题。

- [P3] 完整桌面壳层使相同图表区域比源图更紧凑。
  - Location: 个股工作台中栏。
  - Evidence: 源图是 1660px 宽的单模块裁切，实现需要同时容纳关注列表和决策证据栏。
  - Impact: 大屏下图表单点数据的视觉尺寸略小，但不影响读取和操作。
  - Classification: 可接受的产品约束；不建议为了视觉复制移除 TradingCopilot 的关键人工决策上下文。

## Comparison history

1. First pass — [P2] 成交量柱与价格线争夺主视觉，柱高接近整个画布。
   - Fix: 将成交量轴域调整为数据最大值的七倍，让成交量稳定落在底部区域。
   - Post-fix evidence: `/tmp/trading-copilot-desktop.png`，成交量已回到底部，价格和预测区间成为主层级。
2. Responsive pass — [P2] 初版移动端侧栏占用过多首屏高度，品牌与主操作错位。
   - Fix: 移动端侧栏压缩为 56px 顶栏，保留品牌与量化台切换，隐藏不适合窄屏的次级导航。
   - Post-fix evidence: `/tmp/trading-copilot-mobile.png`，390px 宽无水平溢出，资金、关注列表和个股详情按单列顺序展示。
3. Final pass — 源图与修订后的桌面、移动截图放入同一视觉输入复核；未发现新的 P0/P1/P2 问题。

## Interaction and runtime checks

- verified: 七个个股页签切换、`1D/5D/1M/6M/YTD/1Y` 周期切换、预测显隐、关注股票切换、股票级事件随标的切换。
- responsive: `1660 x 770` 与 `390 x 844` 均无水平溢出。
- browser console: 应用日志无错误；浏览器工具自身出现过一次外部 Statsig 网络错误，不来自 TradingAgent 前端。

## Implementation checklist

- [x] 多周期行情图与成交量
- [x] 未校准情景中位线及 50%/80% 区间
- [x] 公告、新闻、舆情按股票代码关联
- [x] 桌面与移动响应式
- [x] 演示/正式数据边界与人工决策边界

final result: passed
