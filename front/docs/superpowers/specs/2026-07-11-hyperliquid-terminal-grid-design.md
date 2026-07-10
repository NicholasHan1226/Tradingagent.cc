# Hyperliquid Terminal Grid Design

**Date:** 2026-07-11
**Status:** Approved by the user through the instruction to execute the complete design-language audit
**Scope:** `front/` desktop UI only; 1280×720 and 1440×900

## Objective

Turn every TradingAgent page into one coherent, Hyperliquid-inspired automated trading terminal. The reference is the current Hyperliquid trade screen captured at 1440×900, but TradingAgent remains a read-only observatory: chart and market ticker map directly, order book becomes a Process Book, order form becomes an Automation Inspector, and positions/history become read-only ledgers.

## Product Boundary

- No queue writes, order controls, capital changes, strategy changes, cron changes, or real-money behavior.
- Keep the existing snapshot API contract and client/server boundary.
- Use only real snapshot fields. Missing price, entry, latency, or evidence values render as `—`; the UI must not synthesize facts.
- Mobile remains out of scope.

## Visual System

- Direction: `neo-industrial automated trading terminal`.
- One continuous canvas with hairline dividers; secondary pages must not use stacked SaaS cards or hero summaries.
- Persistent top navigation and market/account strip remain, but the page copy collapses into a one-line instrument-style context row.
- Page content uses a three-region terminal grid: primary data surface, 304–320px read-only inspector, bottom ledger/tabs when applicable.
- Numeric content uses tabular mono typography. UI labels are at least 12px; only timestamps/source metadata may use 11px.
- Cyan means positive/live/selected, red means negative/risk, amber means waiting/review, muted means unavailable. No decorative gradients or glow.

## Hyperliquid Mapping

| Hyperliquid | TradingAgent |
| --- | --- |
| Market ticker | Market/account/snapshot/return/risk strip |
| Main chart | Equity/target/benchmark or drawdown chart |
| Order book and trades | Process Book with stage, evidence, gate, latency and event time |
| Order form | Automation Inspector with current process and evidence, no actions |
| Positions/orders/history | Holdings, completed, automatic-review and attribution ledgers |
| Announcements | Runtime and evidence-health tape |

## Components

### TerminalPageShell

Shared layout for `收益 / 过程 / 持仓 / 风险 / 复盘`. It owns the compact metric strip, primary/inspector grid, optional bottom ledger, and consistent empty-state behavior. It replaces `PageSummaryBoard` on theme pages.

### ProcessBook

Always shows the most relevant rows. If running rows are empty, completed rows become the default visible dataset and the title changes to `最近完成`. Columns: process, market, stage, state, evidence, latency, result, updated time. A compact stage distribution sits in the inspector; no full-width five-bar funnel.

### PortfolioLedger

Columns: symbol, market, role, market value, portfolio weight, floating PnL, contribution and risk. Parse the existing display amounts without changing the API. Totals use the selected portfolio currency; A-share-only amounts display CNY. Duplicate symbol/name text is suppressed. Exposure uses compact horizontal bars instead of a donut.

### RiskLedger

Pairs the drawdown chart with explicit 7% limit and warning bands, then lists exceptions/automatic reviews with symbol, stage, evidence, reason and age. The inspector shows current boundary distance and counts without repeating the same KPI cards.

### ReviewLedger

Reuses terminal rows for completed outcomes and automatic calibration. Replace the user-facing `下次规则` label with `自动校准`; retain the underlying read-only value.

## Page Content

- 总览: chart + Automation Inspector + compact process strip + auto-selected ledger. If running is empty and completed exists, select `已完成` automatically.
- 收益: chart is the primary surface; contribution becomes a compact ranked ledger in the inspector.
- 过程: Process Book as the primary surface; stage distribution and completion metrics in the inspector.
- 持仓: Portfolio Ledger as the primary surface; exposure bars and portfolio totals in the inspector.
- 风险: drawdown chart with limit bands; Risk Ledger below; boundary inspector at right.
- 复盘: completed and automatic-review tabs; attribution/calibration columns; no summary-card band.

## Data Trust Rules

- Determine exposure currency from portfolio/market context and symbols already present in the row; do not format all amounts as USD.
- Mixed-currency totals display `多币种` rather than a false aggregate.
- Missing data is `—`, not zero.
- Empty running state automatically reveals recent completed results instead of a large empty panel.

## Accessibility

- Every tab, table and inspector has an accessible name.
- Focus-visible states remain present.
- Minimum primary/secondary UI text is 12px; contrast is increased for secondary labels.
- Charts keep accessible summaries; reduced-motion behavior remains intact.

## Acceptance

- All six pages share the same terminal canvas language at 1280×720 and 1440×900.
- No page has horizontal overflow or a large unexplained empty region when completed/history data exists.
- A-share holdings totals render in CNY, duplicate ticker/name is removed, and exposure is no longer a giant donut.
- Process and risk data are rendered as ledgers rather than oversized summary graphics.
- Existing tests plus new view-model/component tests pass; lint, frontend build and API build pass.
- Browser QA and source/implementation visual comparison produce `design-qa.md` with `final result: passed`.

