# TradingCopilot V9 Design QA

final result: blocked

## Comparison target

- Source visual truth: the user-provided Perplexity Finance AMZN screenshot from the active conversation. Its previous temporary local path is no longer readable, and a fresh direct capture of `https://www.perplexity.ai/finance/AMZN` timed out on 2026-08-03.
- Implementation: browser-rendered `http://127.0.0.1:5173/?product=copilot&demo=1`, inspected on 2026-08-03 in the Codex in-app browser. The visible state was the 1D chart with the voluntary forecast layer expanded.
- The source image and current implementation cannot be opened together in the same comparison input because the source screenshot is no longer available as a durable file/capture. This blocks a product-design fidelity pass; it does not invalidate the functional checks below.

## Browser-rendered checks

- Desktop terminal renders the chart-led left workspace, narrow right research rail, top search/navigation and the existing A-share account context.
- `研究建议与条件` is visible in the right rail. It groups current posture, recent event coverage, entry conditions and reduce/exit conditions without creating a broker action.
- In the demo state it deliberately renders `等待正式覆盖`; forecast is visibly watermarked and `加入人工计划` remains disabled.
- Toggling the 1D research layer changes the accessible chart state to `行情与研究预测图`; the chart retains the uncalibrated disclaimer.
- Browser console warnings/errors: none observed in the tested state.

## Required fidelity surfaces

- Typography and layout: rendered terminal remains compact and legible at desktop width; no overflow was observed in the visible research rail.
- Colors/tokens: existing near-black canvas, muted metadata, red/green price semantics, teal system accent and amber blocked state remain consistent.
- Images/assets: no new raster, logo or custom-drawn asset was introduced; the existing icon/chart component family is retained.
- Copy: new copy distinguishes research conditions from recommendations, orders and automated execution.

## Blocker and follow-up

Re-capture or reattach the chosen Perplexity reference screenshot, then repeat the same-viewport, side-by-side comparison before changing this file to `final result: passed`. No P0/P1/P2 visual defect is asserted while that comparison artifact is absent.
