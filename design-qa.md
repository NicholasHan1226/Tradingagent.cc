# TradingCopilot V7 Design QA

final result: passed

## Source visual truth

- Primary reference: `/var/folders/gg/h6vhh_j50tvg5x4ktqwgxy4r0000gn/T/TemporaryItems/NSIRD_screencaptureui_7weerO/截屏2026-08-02 02.19.50.png`.
- Reference pixels: `1660 x 770`; dark stock-detail terminal with horizontal tabs, compact quote context, chart controls, line/volume chart and a narrow evidence rail.
- Product adaptation: A股个人辅助决策台。账户申报、A股交易约束、证据来源和人工确认边界是必须保留的 TradingCopilot 层；美股盘后、分析师共识和自动交易语义不复制。

## Browser-rendered implementation

- Desktop final: `/Users/nicholashan/.codex/visualizations/2026/08/01/019fbe3b-6b29-7bb0-a2d4-50172cb760b5/tradingcopilot-v7-qa/desktop-final.png` at `1660 x 770`.
- Mobile final: `/Users/nicholashan/.codex/visualizations/2026/08/01/019fbe3b-6b29-7bb0-a2d4-50172cb760b5/tradingcopilot-v7-qa/mobile-overview.png` at `390 x 844`.
- Mobile watchlist: `/Users/nicholashan/.codex/visualizations/2026/08/01/019fbe3b-6b29-7bb0-a2d4-50172cb760b5/tradingcopilot-v7-qa/mobile-watchlist.png` at `390 x 844`.
- Same-frame comparison: `/Users/nicholashan/.codex/visualizations/2026/08/01/019fbe3b-6b29-7bb0-a2d4-50172cb760b5/tradingcopilot-v7-qa/reference-comparison-pass2.png`.
- Verified URL state: `http://127.0.0.1:5174/?product=copilot&demo=1`.

## Same-frame comparison judgment

The reference and final implementation were inspected together at the same `1660 x 770` viewport. The implementation matches the reference's visual language and reading hierarchy:

- near-black continuous workspace, restrained borders, compact tabs and low-radius controls;
- wide chart-led primary surface with a narrow company/evidence rail;
- A股 red-up/green-down semantics, muted metadata and a single teal system accent;
- stock identity, quote context, range controls, line/volume chart and evidence context in one continuous terminal.

The source image is a focused chart crop, while TradingCopilot keeps its account summary, stock search and A股 rules above the chart. Those are deliberate product requirements rather than fidelity defects. Forecast output is hidden by default and demo output is permanently watermarked.

## Scientific and boundary checks

- Demo and generic observations no longer expose a fabricated numeric score.
- Evidence strength is independent from probability, win rate and expected return.
- Formal prediction eligibility requires detached receipt verification, point-in-time input, frozen out-of-sample evidence, calibration, coverage, baseline comparison and post-cost utility.
- `加入人工计划` remains disabled unless the four-layer readiness gate reaches `eligible_for_human_review`.
- Announcements, news and sentiment show source confidence, publish/retrieval time, novelty, impact horizon and receipt state.
- Financial-data tabs fail closed instead of presenting market metrics as financial statements.
- TradingCopilot records human plans and reviews only; it does not connect to a broker or inherit Quant Core execution authority.

## Interaction acceptance

- Forecast is off by default; explicit reveal changes the accessible chart name and shows the uncalibrated research envelope disclaimer.
- `继续观察` requires a reason, trigger, invalidation condition and optional risk before writing the human-intent ledger.
- The decision ledger displays plan, trigger, invalidation, actual action and review note separately.
- Funds and holdings page shows both demo holdings (`000400.SZ`, `601899.SH`) and does not reduce the portfolio to the currently open stock.
- Mobile bottom navigation reaches watchlist, funds/holdings and decision records; `390 px` viewport has no document-level horizontal overflow.
- Browser console and warning log were empty after the complete interaction path.

## Required fidelity surfaces

- Typography: existing Inter/system CJK stack; secondary text remains readable and semantic hierarchy is preserved.
- Assets: existing Lucide icon family and Recharts chart implementation; no placeholder illustration or handcrafted fake asset.
- Responsive behavior: desktop two-column terminal becomes a single-column stock workspace with fixed bottom navigation on mobile.
- Accessibility: named tabs, buttons, chart images, form fields and regions were verified through the DOM snapshot.

## Acceptance boundary

Repository, local-browser, interaction and visual acceptance are complete. Formal real-data recommendation remains intentionally fail-closed until an upstream TradingAgent projection and its detached receipt satisfy the published contract; this is the completed safety behavior, not a hidden demo fallback.
