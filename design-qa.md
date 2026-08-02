# TradingCopilot V6 Design QA

final result: passed

## Source visual truth

- Conversation reference: `Google Chrome Appshot 2026-08-01T19-45-42.827Z.png`, Perplexity Finance desktop stock-detail composition.
- Focused chart reference: `/var/folders/gg/h6vhh_j50tvg5x4ktqwgxy4r0000gn/T/TemporaryItems/NSIRD_screencaptureui_7weerO/截屏2026-08-02 02.19.50.png`.
- Source pixels: `1660 x 770`; desktop dark theme; tabs + dual quote header + chart/volume region.
- Intended adaptation: A股个人辅助决策台，保留来源与人工确认边界，不复制美股盘后或分析师共识语义。

## Browser-rendered implementation

- Screenshot: `/tmp/tradingcopilot-visible-terminal-1440x900-v2.png`.
- Implementation pixels/CSS viewport: `1440 x 900`, `devicePixelRatio=1`.
- URL state: `http://127.0.0.1:5174/?product=copilot`.
- State: 个人申报资金 `0`、关注 `0`、持仓 `0`；默认显示明确标记的只读研究界面预览；`000400.SZ 许继电气`；概述 / 1D / 预测默认隐藏。
- Responsive evidence: `/tmp/tradingcopilot-visible-terminal-390x844.png`; also checked at `1024 x 768`.

## Full-view comparison evidence

The source crop and `1440 x 900` implementation were emitted together in one comparison input. The implementation preserves the reference's visible reading order and density:

- persistent dark navigation and compact top hierarchy;
- stock identity followed by the same seven horizontal reading tabs;
- wide primary quote/chart/volume surface and narrow company/evidence rail;
- compact low-radius surfaces, subtle dividers and muted secondary typography;
- lower sections for significant price changes, stories, announcements/news/sentiment and paired bullish/bearish evidence.

The account strip, A股 search, evidence-source labels and human-intent boundary are intentional product adaptations. The reference's after-hours quote is replaced by A股 previous-close context; analyst consensus is replaced by `Copilot 证据共识`; neither is fabricated.

## Focused region comparison evidence

- Tabs and quote/chart: the source focused crop and implementation were inspected together. Both use tabs above quote context, compact controls, a wide line/volume chart, fine horizontal guides and a dark continuous surface. Exact price-path geometry is intentionally different because the fixtures represent different instruments and markets.
- Right rail: company facts, score, support/oppose counts and event/sentiment temperature remain readable above the fold at `1440 x 900`.
- Empty personal state: account values stay at zero while the complete research preview remains visible; preview text states that it is not watchlist, holding or decision state.
- Responsive: `scrollWidth === clientWidth` at `1024` and `390`; mobile keeps search, stock tabs, quote, chart controls and bottom navigation without horizontal clipping.

## Required fidelity surfaces

- Fonts/typography: existing Inter/system CJK stack retained; compact sizes, optical weights and muted metadata hierarchy match the terminal reference. No actionable wrapping or truncation issue observed.
- Spacing/layout rhythm: primary chart/right-rail proportion, tab spacing, divider cadence and above-the-fold density match the reference intent. The new research bar is a bounded product requirement and does not compress the chart at desktop width.
- Colors/tokens: near-black background, low-contrast borders, teal system accent, amber preview boundary and A股 red-up/green-down semantics remain consistent.
- Image/asset quality: the source contains no required product photography. Icons use the existing Lucide family; chart rendering uses the existing Recharts component, not placeholder art or a raster screenshot.
- Copy/content: preview, source, calibration and human-intent copy explicitly distinguishes demo evidence, unavailable formal data, personal state and non-order actions.

## Primary interactions tested

- Normal URL opens the full read-only research preview while account/watchlist/holdings remain zero.
- Searching `000001.SZ 平安银行` opens its full terminal without adding it to the watchlist and fails closed to unavailable chart/events/analysis.
- `查看界面预览` returns to `000400.SZ` preview.
- `预测` tab exposes the prediction delivery gates.
- Preview decision buttons are disabled and cannot write the personal decision ledger.
- Fresh browser tab console: no warnings or errors.

## Comparison history

1. Earlier V5 normal entry showed only a large empty-account panel. This was a P1 product/fidelity failure because it hid every stock-detail capability that the reference was meant to surface.
2. The fix separated transient research browsing from watchlist/holding authority, restored a full read-only preview on the normal entry, added stock search and explicit `加入关注`, and disabled preview decision writes.
3. Post-fix browser capture at `1440 x 900` shows the full stock terminal above the fold; a fresh-tab console check is clean. No actionable P0/P1/P2 finding remains.

## Residual boundary

This passes repository/local-browser UI and interaction acceptance only. It does not prove production deployment, formal real-time stock projections, calibrated predictive accuracy, broker connectivity, real holdings or profitability.
