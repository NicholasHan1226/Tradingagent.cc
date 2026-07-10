# Hyperliquid-Inspired Read-Only Workbench Design

**Date:** 2026-07-11
**Status:** Proposed for implementation
**Scope:** `TradingAgent/front/` only

## 1. Outcome

Refactor the TradingAgent frontend from a multi-card monitoring dashboard into a continuous, read-only trading workbench inspired by Hyperliquid's information density and spatial hierarchy.

The workbench must answer, in one screen:

1. Which market and account view is selected?
2. What is the current portfolio result and risk distance?
3. Which opportunities are active now?
4. Which positions and completed outcomes explain the result?
5. What needs review next?

The frontend remains a display and review layer. It must not place orders, mutate queues, acknowledge signals, move signal files, call execution routes, expose credentials, or imply that live trading is connected.

## 2. Chosen Approach

Use a targeted workbench refactor rather than a visual-only polish or full rewrite.

- Preserve the existing read-only snapshot API, adapters, types, and server boundaries.
- Introduce one derived `WorkbenchViewModel` so the header, chart, summary metrics, selected market, and account mode use the same portfolio and performance truth.
- Recompose the homepage into a continuous desktop workbench: market strip, primary performance surface, review rail, and bottom blotter.
- Keep theme pages as deep links and compatibility routes, but reuse the same derived view model and row classifiers.
- Add mobile-specific navigation and layouts instead of shrinking the desktop grid.

This approach fixes trust and workflow defects while keeping the existing data contract and rollback surface intact.

## 3. Non-Goals

- No buy, sell, leverage, wallet, broker, cancel, confirm, or order-entry controls.
- No mutation API or changes to `signals/`, account state, callbacks, webhooks, email, or execution code.
- No change to trading strategy thresholds, capital rules, market selection, or simulated fill logic.
- No production deployment, push, or service restart in the implementation phase unless Nicholas separately authorizes it after local verification.
- No new charting or component dependency unless the existing React, Recharts, and Lucide stack cannot meet a verified requirement.

## 4. Information Architecture

### 4.1 Desktop workbench

The desktop page uses four continuous regions separated by hairlines rather than floating cards:

1. **Top navigation:** brand, primary routes, simulated-runtime status, notifications, and settings.
2. **Market strip:** selected market, snapshot freshness, account mode, portfolio PnL/return, target gap, drawdown, active opportunities, and positions.
3. **Primary workspace:**
   - center-left: performance chart with one canonical latest value and range controls;
   - center-right: review rail showing active opportunity or selected position details;
   - no order form or exchange execution affordance.
4. **Bottom blotter:** tabs for active opportunities, positions, completed outcomes, and review items. Tables use compact rows and preserve real empty states.

The opportunity funnel becomes a compact context strip above the blotter or chart rather than the largest first-screen object.

### 4.2 Mobile workbench

At widths below 720px:

- Use a compact header plus a fixed bottom navigation for `主页 / 机会 / 持仓 / 风险`.
- Collapse the market strip into a two-column metric grid with the selected market control above it.
- Stack chart, review rail, and blotter vertically.
- Render funnel stages as a vertical sequence or a horizontally scrollable strip with a visible scroll affordance.
- Never hide off-screen navigation or metrics behind `overflow: hidden`.
- Keep primary touch targets at least 44px high.

At 720-1180px, use a single-column workspace while retaining the desktop top navigation and a horizontally scrollable blotter.

## 5. Canonical View Model

Create a pure derived-data module that returns a `WorkbenchViewModel` from the selected market, snapshot performance, market summaries, portfolio, signals, holdings, funnel events, and account mode.

The model must provide:

- `portfolio`: one canonical selected-view portfolio;
- `performance`: a series whose final point equals the canonical portfolio return and target;
- `headline`: PnL amount, return, target gap, drawdown, capital, and freshness from the same view;
- `opportunities.active`: pending or genuinely reviewable current opportunities only;
- `opportunities.completed`: executed, missed, cancelled, expired, or otherwise terminal records;
- `positions`: selected-market holdings;
- `reviewItems`: risk or evidence items requiring user attention;
- `liveGate`: a dedicated unavailable state when account mode is `live`.

Rules:

- Header and chart latest values must never disagree.
- `All Markets` uses one explicitly labelled aggregate portfolio and aggregate series.
- A single-market selection uses that market summary and its matching series.
- If a matching historical series is unavailable, show the canonical current point as a one-point state and say that history is unavailable; do not substitute another account's series.
- Terminal signal rows must never appear under `当前机会`.
- Empty active opportunities produce an empty state, not a fallback to all signal rows.

## 6. Component Boundaries

### New or extracted components

- `lib/workbenchViewModel.ts`: pure classification and canonical portfolio/performance derivation.
- `components/workbench/WorkbenchShell.tsx`: desktop/tablet region composition.
- `components/workbench/MarketStrip.tsx`: selected view and canonical headline metrics.
- `components/workbench/ReviewRail.tsx`: active opportunity, selected position, or next-review state.
- `components/workbench/WorkbenchBlotter.tsx`: tabbed active opportunities, positions, completed outcomes, and reviews.
- `components/workbench/MobileNav.tsx`: four-item mobile navigation.
- `components/workbench/ChartAccessibleSummary.tsx`: concise chart summary and optional tabular fallback for assistive technology.

### Reused components

- `PerformanceChart` remains the chart renderer but consumes canonical data and exposes an accessible name and description.
- Existing opportunity, holding, and signal row components are reused or simplified inside the blotter.
- Existing theme pages remain available but consume shared classifiers and canonical metrics.

### Components to retire or demote

- `ContributionPanel` must not render a zero-value `buy` chart. It either consumes real PnL attribution or shows a real empty state.
- The large homepage funnel is demoted to contextual status.
- Raw runtime-reason rendering is replaced by a centralized user-copy formatter.

## 7. Live Account Gate

Selecting `实盘` must enter a dedicated gated state:

- Keep the selected simulated market context visible only when labelled `模拟盘参考`.
- Replace portfolio result, review rail, and blotter action language with a clear `实盘待接入` explanation.
- Do not show real-money amounts, connected-account language, or controls that look executable.
- Translate runtime reasons into user language; never expose strings such as `market_data_missing` or `futures_market_data_not_ready`.
- Returning to `模拟盘` restores the prior selected market and blotter tab.

## 8. Visual System

**Direction:** restrained neo-industrial fintech. Hyperliquid supplies the density and continuous workbench hierarchy; TradingAgent keeps its own evidence-first, read-only identity.

### Typography

- Chinese: system sans stack already used by the application.
- Numbers: existing tabular number font token.
- Scale: 11 / 12 / 14 / 16 / 20 / 28 / 36px.
- Body copy is never below 12px on mobile; 11px is reserved for non-critical desktop metadata.

### Color tokens

- `--bg-base`: page canvas.
- `--surface-workbench`: continuous workbench substrate.
- `--surface-elevated`: menus and selected detail surfaces.
- `--text-primary`, `--text-secondary`, `--text-muted`: three readable text levels.
- `--accent-cyan`: selected state and positive result.
- `--state-amber`: waiting, review, and incomplete evidence.
- `--state-red`: negative result, execution fault, and hard risk.
- `--border-hairline`, `--border-strong`: structural separation and focus.

Color is never the only state signal; labels or icons accompany every state.

### Spacing, radius, and shadow

- Spacing scale: 4 / 8 / 12 / 16 / 24 / 32px.
- Radius scale: 3 / 5 / 8px. Workbench regions use 0-5px; overlays may use 8px.
- Shadows are limited to menus and overlays. Main panels use borders and tonal separation.

### Motion

- Hover/focus feedback: 120-140ms.
- Panel/tab transition: 180ms.
- No decorative continuous animation. Live data changes may use a brief opacity highlight and must respect `prefers-reduced-motion`.

### Core component states

- Buttons: default, hover, focus-visible, active, disabled.
- Tabs: selected state plus `aria-selected`.
- Tables: loading, empty, populated, and error states.
- Charts: loading, one-point, populated, outlier, and unavailable states.
- Live gate: dedicated unavailable state, not a disabled order panel.

## 9. Accessibility

- Every chart has an accessible name, description, and concise text summary.
- Primary navigation and blotter tabs have correct roles and selected states.
- Keyboard focus is visible and is not represented by color alone.
- Mobile touch targets are at least 44px high.
- Critical text meets a readable contrast baseline; muted text may not carry unique instructions.
- Tables preserve semantic row and column relationships where practical; div-based terminal rows receive an accessible table alternative or are converted to semantic tables.
- Responsive validation includes 390x844, 768x1024, and 1280x720.

## 10. Error, Loading, and Empty States

- Snapshot fetch failure keeps the shell visible and names the unavailable domain without showing sample returns.
- A one-point performance series is labelled `历史曲线尚未形成`.
- No active opportunities displays `当前没有待处理机会`; completed results remain available in the completed blotter tab.
- No attribution displays an explanatory empty state instead of a zero-value chart.
- Stale snapshot freshness is visible in the market strip but does not silently change simulated results.

## 11. Testing Strategy

All behavior changes follow test-first development.

### Unit tests

- Canonical headline return equals the final performance point for all-market and single-market views.
- A mismatched raw portfolio/performance input cannot produce conflicting visible results.
- Active opportunities exclude executed, missed, cancelled, expired, and other terminal rows.
- Completed outcomes contain the terminal rows removed from active opportunities.
- Live mode returns the dedicated gated model and does not expose executable actions.
- Runtime reasons map to user-facing Chinese copy.

### Component tests

- Workbench blotter changes tab content and selected state.
- Empty active opportunities do not fall back to completed signals.
- Mobile navigation exposes four primary destinations.
- Performance chart renders its accessible name and summary.
- Live mode clearly labels simulated reference data.

### Rendered validation

- Desktop 1280x720: market strip, chart, review rail, and blotter are visible without overlap.
- Tablet 768x1024: workspace becomes one column and tables remain reachable.
- Mobile 390x844: no clipped primary navigation or off-screen KPI/funnel content.
- Browser console has no relevant errors or warnings.
- Production-like snapshot data verifies headline/chart consistency and opportunity classification.

### Required commands

```bash
npm run lint
npm test -- --run
npm run build
npm run build:api
```

## 12. Documentation

Implementation updates must keep these sources aligned:

- `README.md`: homepage and workbench behavior, active/completed opportunity distinction, and live-gate wording.
- `DESIGN.md`: selected workbench hierarchy, responsive rules, tokens, and final scorecard.
- `docs/integration.md`: only if the snapshot contract or read-only endpoint behavior changes.
- `STATUS.md`: only if implementation changes current project state or leaves a verified gap.

No API contract change is expected. If implementation proves one is required, stop and propose that contract separately before changing the server reader.

## 13. Acceptance Criteria

The implementation is acceptable when all of the following are true:

1. Header, summary, and performance-chart latest return use the same canonical view.
2. `当前机会` contains no executed, missed, cancelled, expired, or other terminal records.
3. The desktop first screen reads as one continuous workbench rather than a stack of equal-weight cards.
4. Live mode is visibly gated and never resembles an enabled execution surface.
5. Raw backend runtime reasons are not visible.
6. 390px mobile view contains no clipped primary controls or inaccessible off-screen metrics.
7. Charts expose an accessible name and text summary.
8. Lint, unit/component tests, frontend build, and API build pass.
9. Desktop, tablet, and mobile browser screenshots support the visual claims.
10. Design Taste score is at least 85/100, with any remaining weaknesses named.

## 14. Rollback

- Keep the existing snapshot API and adapter contracts unchanged.
- Isolate new composition in workbench components so the previous `HomeDashboard` composition can be restored without reverting data readers.
- Land canonical view-model and opportunity-classification fixes before visual composition. These trust fixes remain useful even if the new layout is rolled back.
- Do not delete legacy components until the new shell passes all commands and rendered validation.

