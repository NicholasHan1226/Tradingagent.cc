# Design QA

Latest result: homepage dynamic signal-funnel refactor, live-return refactor,
current-opportunity/review split,
wordmark-first brand reset, page-title-band consolidation, system-word cleanup,
Hyperliquid-restraint polish, first production-readiness split, and shared panel
state handling. The latest pass also merged the homepage right rail into a
single `当前结论` module, added chart target/risk reference lines, added an
opportunity summary strip, split App into page/chart/table/panel components,
added a TradingAgent read-model contract, chart-to-event navigation, a
decreasing decision funnel, a first local read-only TradingAgent snapshot
reader, a reserved `/api/trading-agent/snapshot` integration port, a real
TradingAgent capability map, and split the chart bundle. The dashboard
opens on `主页`, makes live return the primary homepage result, includes a
stronger contour-line background, uses a Hyperliquid-like page-title/KPI band,
and supports navigation to `收益`、`机会`、`持仓`、`决策`、`风险`、`复盘`. The latest
pass specifically makes the homepage funnel a moving channel board with
individual market labels, enlarges the live return chart, reduces cyan overuse, adds
chart terminal labels, reduced boxed active states, panel brightness, row
height, border contrast, status-pill saturation, and replaced the rejected `TA`
monogram / abstract flow mark with a cleaner wordmark-only logo.

## Capture

- URL: `http://127.0.0.1:5173/`
- Browser: Google Chrome via Computer Use state
- Viewport checked: desktop Chrome window
- Console errors: none observed
- Brand check: `.brand-lockup` uses a wordmark-only `TradingAgent` logo with
  one restrained cyan baseline, no icon backplate, glow, descriptor, or forced
  initials. This is a formalized product-identity direction, not legal
  trademark clearance.
- Latest browser refresh: checked through Chrome app state after Browser URL
  policy blocked in-app navigation to `http://127.0.0.1:5173/`.
- Latest in-app browser verification: homepage loaded at
  `http://127.0.0.1:5173/`, no console errors, default state surfaces stayed
  hidden, and the `实盘预留` gate opened with account authorization / risk
  confirmation / receipt-writeback boundary copy.
- Latest in-app browser verification after component split: homepage loaded at
  `http://127.0.0.1:5173/`, `当前结论` replaced the previous stacked rail
  titles, `机会` page showed the summary strip plus BTC/HK rows, the
  `实盘预留` gate opened, and console errors remained empty.
- Latest desktop browser verification after the workbench-density pass:
  homepage loaded at `http://127.0.0.1:5173/`, `全市场总览` and
  the main live-return panel were visible, `机会缺口` appeared in the chart copy,
  the old negative opportunity wording did not appear, the result drilldown and
  chart event bar were present, the workbench radius was `4px`, market header
  height was `154px`, the live-account gate opened, and console errors were
  empty.
- Mobile viewport: not re-run in this pass

## Verified

1. `主页` is selected by default.
2. The duplicated metric-card row was removed from the homepage; the top summary
   strip now carries core numbers with lower visual weight.
3. The homepage performance panel now uses live return as the primary result
   and labels the card `实时收益` with amount plus percentage, with target gap
   shown directly below.
4. The brand area is now a cleaner product lockup: a white `TradingAgent`
   wordmark with one short cyan baseline. It avoids lucide icons, boxed marks,
   glow, descriptors, and forced initial-letter construction.
5. The homepage return chart is visible under the hero and remains the primary
   dynamic chart panel.
6. Navigation to `收益` shows `实时收益与目标`.
7. Navigation to `机会` shows `当前机会`.
8. Navigation to `持仓` shows `模拟盘持仓`.
9. Navigation to `决策` shows `决策影响收益`.
10. Navigation to `风险` shows `风险边界`.
11. Navigation to `复盘` shows `交易复盘`.
12. No visible old secondary-account copy remains; the page uses simulated
    account plus live-account reserved state.
13. No exchange-style buy/sell, connect-wallet, chart drawing, or indicator
    toolbar is present.
14. Copy is Chinese-first and result-oriented.
15. Latest polish removes decorative radial background glow and uses a clearer
    lower contour-line texture instead.
16. Market header now says `全市场总览`; visible system codes were removed, and
    the header reads as a page summary rather than a trading-terminal bar.
17. Chart copy uses `机会缺口` to show unrealized opportunity impact without
    falling back to backstage phrasing.
18. Panels now use a more transparent deep green-black material so the page
    feels closer to Hyperliquid's embedded module style.
19. HYPE-specific alert language was reduced to user-facing `入场条件`.
20. Viewport-scaled title sizing was removed from CSS.
21. The market header now has enough vertical space for the large page title;
    the title no longer appears compressed under the navigation.
22. The homepage right rail is visually merged into one result module with
    internal dividers instead of three unrelated cards.
23. The hidden asset-switch chevron was removed from the DOM and the related
    stale CSS was cleaned up.
24. The homepage value card label changed from duplicate `实时收益` to
    `现在收益`, reducing repeated KPI language while keeping live return visible.
25. Theme pages no longer repeat a second large page header inside the content
    area. The top title band now carries the selected page title and explanation,
    while the content area starts directly with the result panel.
26. The opportunity page was visually rechecked in Chrome after consolidation:
    it now reads as a clean Hyperliquid-like table surface instead of a stack of
    separate dashboard cards.
27. Dense tables now use an embedded terminal-panel treatment with sticky header,
    low-contrast row dividers, clearer asset hierarchy, and restrained hover
    feedback.
28. Top navigation selected state now matches Hyperliquid restraint more closely:
    cyan text without a large boxed active outline. Keyboard focus remains
    available but is quieter.
29. Panel material and background contour lines were darkened to reduce the
    earlier green glow and make modules feel embedded in one financial canvas.
30. Table rows and status pills were tightened and desaturated, reducing the
    colorful dashboard feeling while preserving readable outcomes.
31. The header brand now returns to a more mature wordmark-only identity after
    rejecting the weak monogram and abstract-mark directions. Cyan is now a
    recognition line rather than the whole `Agent` word.
32. Unused starter assets (`react.svg`, `vite.svg`) and the rejected mark asset
    were removed so the asset folder does not preserve weak logo directions.
33. User-facing copy was swept for backstage/system wording. Homepage calls to
    action, live-account wording, freshness wording, opportunity conditions,
    and lifecycle/signal-style labels were replaced with result-oriented
    Chinese such as `查看收益原因`, `实盘`, `实时`,
    `等价格和成交量再走强`, and `交易复盘`.
34. `机会` and `复盘` have been split by job: the opportunity page only shows
    currently actionable rows, while the review page only shows closed
    opportunities and attribution.
35. Homepage right rail now separates current result, next actions, and risk
    state, while current opportunities and compact holdings sit below the main
    live-return panel.
36. Production-readiness split completed: React view composition now imports
    typed contracts from `src/types/dashboard.ts`, mock data from
    `src/data/dashboard.ts`, and derived filtering/live movement from
    `src/lib/dashboard.ts`.
37. `README.md` no longer carries the default Vite template copy; it now states
    the dashboard purpose, current architecture, local checks, and production
    gaps.
38. Shared panel state handling is now wired around the main chart, opportunity
    table, holdings table, decision flow, risk timeline, and review table.
    Default mock state is `ready`, so production states do not add visual noise
    until real data reports loading, empty, stale, error, or live-gated.
39. The live-account button is no longer a disabled dead control. It opens a
    compact gate explaining that only simulated results are shown until account
    authorization, risk confirmation, and receipt writeback are complete.
40. Test infrastructure is now installed with Vitest and Testing Library.
    Current coverage includes dashboard view rules, adapter gating, shared panel
    states, and top-level error recovery.
41. Mock dashboard state now follows the production-shaped path:
    `DashboardApiResponse` -> `toDashboardState()` -> UI. This keeps real API
    integration scoped to the read-model boundary instead of leaking execution
    logic into panels.
42. First component split completed: `TopNav`, `MarketHeader`, `PanelTitle`,
    `AssetCell`, `SummaryRow`, `OutcomePill`, and `Timeline` moved out of
    `App.tsx` while preserving all existing class names and visual behavior.
43. A top-level error boundary now prevents a failed panel from turning into a
    blank page.
44. Second component split completed: `App.tsx` is now a 67-line state/routing
    shell. Pages, charts, tables, result panels, and formatting helpers live in
    dedicated files.
45. Homepage right rail is now a single `当前结论` module with current result,
    next actions, risk boundary, and one CTA. The old `现在先处理什么` heading no
    longer appears.
46. Live return chart now draws explicit target and risk reference lines and
    computes its domain from data instead of a fixed range.
47. Opportunity page now starts with a compact summary strip before the table,
    making the result visible before row-level details.
48. `src/api/tradingAgentReadModel.ts` documents the TradingAgent read-only
    source boundary: capital ledger, position ledger, simulated orders, signal
    queue, review data, and router decisions.
49. Recharts is split into a dedicated `charts` build chunk. Production build
    no longer emits the previous oversized single-bundle warning.
50. Live return chart event chips now open related review surfaces. The current
    anchors route to `机会`、`决策` and `风险` without introducing order-entry
    controls.
51. Decision formation is now a decreasing funnel with drop-off percentages,
    including `漏斗留存`, `流失 34.3%`, and outcome distribution labels.
52. `src/server/tradingAgentSnapshot.ts` reads position snapshots, the
    `position_plan.jsonl` fallback, signal queue files, filled signal writeback
    and review evidence into the existing read-model snapshot contract without
    executing or writing state.
53. The TradingAgent read-model sources now align to current project paths:
    `signals/positions`, `signals/filled`, `signals/pending`,
    `shared/accounting/position_plan.jsonl`, `shared/review/daily/daily_brief.jsonl`,
    attribution files and `shared/risk/risk_limits.yaml`.
54. `/api/trading-agent/snapshot` is reserved as the future direct-connect
    read-only endpoint, with a browser client, timeout handling, contract
    validation and server JSON wrapper covered by tests.
55. `src/api/tradingAgentCapabilities.ts` records the usable display surfaces:
    current opportunities and risk are ready; positions, performance and
    decisions are partial; live-account readiness remains gated.
56. Live return chart event chips are now derived from performance movement and
    active opportunities, so new real data can generate review anchors without
    hand-maintained labels.
57. The homepage trading funnel now consumes `funnelEvents[]` from the
    read-only snapshot. Events are derived from signal queue rows or simulated
    ledger replay, so the animated funnel and latest-event tape no longer rely
    only on static signal rows.
58. Subpages now share a result-first summary board pattern: a page-level
    conclusion on the left and four key metrics on the right, matching the
    Hyperliquid-style component rhythm more closely than standalone metric
    strips.
57. The latest workbench pass reduced the visible page-title height, lowered
    contour opacity, reduced workbench border strength, tightened radii, and
    made the homepage result area read as one embedded information surface
    instead of stacked floating dashboard cards.
58. The homepage chart footer now uses `机会缺口`; the previous negative
    opportunity wording no longer appears in the React UI.
59. Desktop browser verification after the workbench-density pass confirmed
    the homepage title, main conclusion, chart SVG, chart event bar, result
    drilldown, live-account gate, and zero console errors at 1440x900.
60. Browser-comment pass: top KPI strip no longer carries duplicate profit
    metrics. Separate `净收益` and `实时收益` header metrics no longer appear.
61. Browser-comment pass: `实时` was removed from the KPI strip and moved to
    the market tools as `实时`.
62. Browser-comment pass: the ambiguous `总览` page-mode badge was removed from
    the title band.
63. Browser-comment pass: market filters now open from a default `全市场`
    drilldown trigger and close after selecting a market.
64. Browser-comment pass: the top simulated/live control is now a two-option
    click switch. `实盘预留` still opens the live-account gate rather than
    implying live trading is enabled.
65. Browser-comment pass: homepage now includes a compact trading-signal funnel
    before the live return chart: `发现`、`可做`、`风控通过`、`形成结果`.
66. Signal-flow pass: simulated profit amount and return percentage were moved
    out of the top KPI strip and integrated into the home chart's primary result
    position, with the amount as the dominant value.
67. Signal-flow pass: account mode is now one click-toggle button. It switches
    between `模拟盘` and `实盘预留`; the live state opens the existing
    reserved live-account gate.
68. Signal-flow pass: homepage signal funnel introduced restrained motion while
    keeping the same four channel counts.
69. Dynamic-funnel pass: the homepage funnel is no longer a static count strip.
    Individual signal labels such as `0700.HK`, `BTC`, `AAPL` and `HYPE` move
    through the discovery-to-result lanes, making the funnel visibly alive
    without adding icon glow or colorful dashboard blocks.
70. Dynamic-funnel pass: the homepage live-return module now uses `实时收益`
    copy with amount as the dominant value and percentage as the secondary
    movement value.
71. True-funnel pass: the homepage signal module is now a 5-stage narrowing
    SVG funnel. It shows entry count `1,284`, stage counts `843 / 612 / 487 /
    356`, dropped counts `-441 / -231 / -125 / -131 / -27`, animated market
    labels through the center path, and a final `交易信号` output.
72. Data-driven funnel pass: the homepage funnel no longer uses fixed aggregate
    counts. It derives counts, drops, moving symbols, and final signal output
    from the current `SignalRow[]`. With the current visible rows it shows
    `6 / 6 / 6 / 4 / 4`, final `交易信号 4`, and the live-return block summarizes
    executed, pending, and missed signal counts.

## Checks

- `npm run lint`: passed.
- `npm run build`: passed after chart-to-event linking, decision funnel, local
  snapshot reader, and docs update.
- `npm test`: passed with 10 files and 26 tests after app navigation,
  read-model, snapshot-reader, adapter, integration port, capability map,
  chart-event derivation, state-boundary, and error-boundary coverage.
- Local HTTP check: passed.
- Chrome visual state check: opportunity page passed after the latest polish.
- Chrome visual state check: homepage passed after the latest Hyperliquid
  alignment pass.
- Chrome visual state check: homepage passed after the workbench-density pass
  with `4px` workbench radius, `154px` title band, `机会缺口` copy, and no
  console errors.
- Chrome visual state check: browser-comment pass confirmed no old `净收益` /
  `实时收益` header metrics, moved freshness label, removed mode badge, visible
  signal funnel, opening market drilldown with 6 choices, successful market
  switch to `美股`, live-account gate, and no console errors.
- Chrome visual state check: dynamic-funnel pass confirmed hero profit amount
  and return percentage, no profit metric in the header KPI strip, one account
  toggle button, live-account gate after toggle, moving signal-label funnel
  style, and no console errors.
- Chrome visual state check: latest dynamic-funnel verification confirmed
  `实时收益、目标差、风险边界和当前机会集中在一屏。`, 8 moving signal labels
  using `signal-particle-cross`, visible return chart, no old simulated-return
  headline/copy, and no console errors.
- Chrome visual state check: copyless-funnel pass confirmed the funnel no
  longer has `.module-copy`, the removed headline is not visible, the funnel
  board renders at roughly `694x174`, all 8 signal labels keep
  `signal-particle-cross`, the return chart is still visible, and console
  errors are empty.
- Chrome visual state check: true-funnel pass confirmed 5 SVG funnel segments,
  stage labels `发现机会 / 形成信号 / 交易条件 / 风险筛选 / 执行确认`, counts
  `1,284 / 843 / 612 / 487 / 356`, dropped counts, 8 moving particles, no old
  channel board, visible return chart, and no console errors.
- Chrome visual state check: final true-funnel verification confirmed SVG size
  around `692x184`, 5 funnel segments, 8 animated `animateMotion` signal
  particles, stage labels `发现机会 / 形成信号 / 交易条件 / 风险筛选 / 执行确认`,
  counts `1,284 / 843 / 612 / 487 / 356`, drops `-441 / -231 / -125 / -131 /
  -27`, final output `交易信号`, no old channel board, visible return chart,
  and no console errors.
- Chrome visual state check: data-driven funnel verification confirmed current
  visible signal rows drive the funnel: labels `发现机会 / 形成信号 / 交易条件 /
  风控放行 / 交易信号`, counts `6 / 6 / 6 / 4 / 4`, final output `交易信号 4`,
  6 moving symbols, right live-return amount changing with the live percentage,
  visible return chart, and no console errors.
- In-app browser check: homepage content, live-account gate, and console errors
  passed.

## Known Notes

- Mobile layout was intentionally not re-run after this desktop-first refactor
  because the current iteration scope excludes mobile.
- The UI is still using mock data. It is closer to production structure, and a
  local server-side snapshot reader plus reserved endpoint now exist, but the
  endpoint still needs to be mounted in the selected API/server runtime before
  the UI can consume real TradingAgent data.
- Loading, empty, stale-data, error, and live-gated panel states now exist and
  have component/adapter tests. They still need browser route tests with real API
  responses before production use.

## July 4 Homepage Refactor

73. Homepage funnel refactor: `HomeSignalFlow` was split into
    `SignalFunnelFlow`. It now renders a dynamic flow funnel with a main
    opportunity path, a risk-protection branch, a missed-opportunity branch, and
    moving symbols based on the current `SignalRow[]`.
74. Return-card refactor: the separate simulated/live wording was consolidated
    into `RealtimeReturnCard`. Amount is the primary value and percentage is
    shown directly beneath it.
75. Copy cleanup: visible homepage copy no longer uses old judgement wording,
    live-connection wording, or click-instruction language. The visible right
    rail uses `当前结论`, `查看依据`, and business-readable result rows.
76. Account switch cleanup: the top action changed from a two-line button to a
    compact `模拟盘 / 实盘` segmented control with `运行中 / 预留` states.
77. Market header cleanup: the market selector and freshness state now share the
    same 34px control height. Header opportunity counts come from current
    visible signals instead of a fixed aggregate number.

Checks added in this pass:

- `npm run lint`: passed.
- `npm test -- --run`: passed, 10 files / 26 tests.
- `npm run build`: passed.
- Chrome desktop screenshot at 1440x1000 confirmed the homepage renders with
  the new flow funnel, the combined return card, aligned market controls, and
  no old system phrases in the captured DOM.
- Chrome narrow desktop screenshot at 814x837 confirmed the desktop layout
  remains readable on the left-side viewport; mobile/fully responsive treatment
  remains intentionally out of scope.

## July 5 Production Data Refinement

78. Return curve refinement: `style_performance` PnL can now be expanded onto
    matching simulated ledger trade timestamps. The final PnL total remains
    sourced from the review performance file; trade logs only provide timing.
79. Performance integrity guard: trade journals without a PnL/review source
    still leave `performance[]` empty, preventing the UI from using traded
    notional or cost basis as a fake return curve.
80. Documentation cleanup: the active frontend is no longer described as
    mock-only. The current read model consumes the server-side snapshot route
    and keeps execution surfaces out of `front/`.

Checks added in this pass:

- `npm test -- --run src/server/tradingAgentSnapshot.test.ts`: passed, 11 tests.
- `npm run build:api`: passed.
- `npm run lint && npm test -- --run && npm run build && npm run build:api`:
  passed, 14 files / 55 tests.
