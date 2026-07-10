# TradingAgent Front Design System

## Product position

TradingAgent Front is a read-only observatory for an automated trading system. It explains what the system saw, how a process moved, what was written back, how the simulated portfolio changed, and where risk controls intervened. It never exposes order entry, queue mutation, capital controls, strategy editing, or account credentials.

The active desktop information architecture is `总览 / 收益 / 过程 / 持仓 / 风险 / 复盘`. The supported design-QA viewports are 1280×720 and 1440×900; mobile is intentionally deferred.

## Design language

The interface uses a Hyperliquid-inspired terminal grammar rather than a generic dashboard skin:

- one continuous near-black canvas with internal hairline dividers;
- compact market/account metrics instead of hero cards;
- a primary data surface, 316px read-only inspector, and optional bottom ledger;
- tabular mono numbers, 12px primary UI copy, and 11px only for metadata;
- cyan for positive/live/selected, red for loss or risk, amber for waiting/review, muted gray for unavailable facts;
- no decorative glow, gradients, large radii, floating cards, or artificial asset graphics.

Hyperliquid structures are translated to the product boundary: its market ticker becomes the snapshot strip, chart remains a chart, order book becomes Process Book, order form becomes Automation Inspector, and positions/orders/history become read-only ledgers.

## Shared page anatomy

`TerminalPageShell` owns the secondary-page structure:

1. compact context strip: selected market, return, drawdown, process state and holdings;
2. primary data surface: chart or ledger;
3. Automation Inspector: derived status, distribution and evidence, with no actions;
4. optional bottom ledger for related events.

Secondary pages do not use `PageSummaryBoard`. Empty running state reveals recent completed results when available instead of reserving a large blank panel.

## Page content contract

- `总览`: return chart, runtime inspector, compact process strip and `运行中 / 持仓 / 已完成 / 自动复盘` blotter. If running is empty, the blotter opens the most relevant non-empty result tab.
- `收益`: equity/target/benchmark chart as the primary surface; ranked contribution and realized/unrealized result in the inspector.
- `过程`: Process Book with process, market, stage, state, evidence, latency, result and update time; compact stage distribution in the inspector.
- `持仓`: Portfolio Ledger with market value, derived portfolio weight, floating PnL, contribution and risk; horizontal exposure bars replace the donut.
- `风险`: drawdown chart with 5% warning and 7% hard-limit context, boundary distance inspector, and Risk Ledger for blocked/missed/cancelled records.
- `复盘`: completed result ledger and automatic calibration field. User-facing copy is `自动校准`, never an instruction for manual action.

## Data trust rules

- Use snapshot fields only. Missing facts display `—`, never synthetic zeroes.
- Holdings totals are currency-aware: A-share-only exposure is CNY, USD-only exposure is USD, percentages remain percentages, and mixed currencies display `多币种` without a false sum.
- Suppress an asset name when it duplicates the ticker.
- Terminal rows preserve result states such as partial fill, safety block, missed and cancelled; terminal records never return to the running queue.
- Display timestamps and source health as observed. Never use the browser clock to disguise stale data.

## Interaction and accessibility

- Navigation, market filtering, account-mode gate, time ranges and blotter tabs remain keyboard operable.
- Every terminal region, table, inspector and tab has an accessible name.
- Focus-visible styling stays present and restrained.
- Motion is functional, subtle and disabled under `prefers-reduced-motion`.
- The live-account surface stays a read-only readiness gate until authorization, risk checks and execution receipts are independently verified.

## Implementation map

- `src/components/terminal/`: shared terminal shell and ledgers.
- `src/lib/terminalViewModels.ts`: currency-safe, read-only display models.
- `src/components/workbench/`: overview workbench and result-tab selection.
- `src/pages/ThemePage.tsx`: composition for the five secondary pages.
- `src/App.css`: terminal tokens, canvas, grid and dense table rules.
- `src/api/` and `src/server/`: unchanged read-only snapshot contract.

The implementation is accepted only after unit/component tests, lint, frontend/API builds, real-browser desktop checks, no-horizontal-overflow checks and a reference/implementation visual comparison all pass.
