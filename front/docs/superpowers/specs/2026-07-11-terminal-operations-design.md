# TradingAgent Terminal Operations Design

**Date:** 2026-07-11  
**Status:** Approved through the user's instruction to execute all recommended optimizations  
**Scope:** `front/` desktop UI and additive read-only snapshot fields; 1280×720 and 1440×900

## Objective

Evolve the Hyperliquid-inspired visual shell into a trustworthy automated-trading observatory. Every visible count, label, inspector, ledger and shortcut must describe the same current state; each page must expose the evidence required to understand an automated result without adding order entry or human decision controls.

## Alternatives Considered

1. **Visual polish only:** lowest risk, but it preserves contradictory runtime states and shallow ledgers. Rejected.
2. **New dedicated backend observability service:** strongest long-term data model, but too broad for the current frontend optimization and would create a new runtime boundary. Rejected.
3. **Chosen — derived terminal operations layer:** add pure state/view-model resolvers, reuse the existing snapshot and funnel events, and add only optional read-only holding fields where source files already contain them. This gives immediate consistency without changing execution behavior.

## Hard Boundaries

- No queue writes, claim/fill/cancel operations, orders, account actions, capital changes, strategy edits, cron changes, email or webhook calls.
- Snapshot additions are optional display fields only; existing consumers remain valid.
- Never synthesize prices, costs, timestamps, strategy versions or evidence. Missing facts render as `—`.
- All-market currency values remain normalized to CNY according to the existing server contract.
- Mobile remains out of scope.

## Architecture

### TerminalStateResolver

One pure resolver owns runtime categorization and presentation priority. It returns:

- `running`: `status=pending` only;
- `completed`: executed and partial-fill terminal results;
- `review`: blocked, missed and cancelled records;
- `runtimeItem`: running first, otherwise a clearly labelled recent event, otherwise idle;
- `preferredTab`: running, completed, positions, review, then active-empty;
- `counts`: the same values consumed by the header, inspector and tabs.

When a snapshot arrives after initial render, the blotter automatically moves away from an empty selected tab to the resolver's preferred non-empty tab. A user's explicit selection remains stable while that tab still contains data.

### MarketTape

A persistent 44px market tape sits below the market header. It renders `全市场 / A股 / 美股 / 加密 / 预测 / 期货` from `marketSummaries[]`, with selected state, return, holding count, runtime state and freshness. Clicking a segment updates the existing market filter; no trade action is introduced.

### EvidenceHealth

The runtime inspector and secondary-page inspectors share one evidence-health block derived from `domains`, snapshot time, market summary and source references. It distinguishes ready, stale, partial, error and missing states and identifies the relevant read-only source category.

### ProcessEventLedger

`funnelEvents[]` becomes the process event stream. Events are ordered by timestamp/sequence and expose asset, stage, event result, source, latency, reason and time. Process Book still shows the current/most relevant process rows, while the event ledger provides the audit trail beneath it.

### Additive Holding Evidence

`HoldingRow` gains optional `quantity`, `averagePrice`, `markPrice`, `costBasis`, `marketValue`, `dayPnl`, `currency`, `updatedAt` and `source`. Server parsers populate these only from existing position snapshots or simulated ledgers. Portfolio Ledger displays available cost/mark/quantity and keeps `—` for absent fields.

### Terminal Table Controls

Process, holdings, risk and review ledgers use a shared compact toolbar:

- row search by ticker/name/reason/strategy;
- sortable numeric/time/status columns;
- a native disclosure menu for column visibility;
- visible result count and keyboard-focus styling.

Controls are local presentation state and never mutate server data.

### URL and Keyboard State

The URL query stores `page`, `market` and returns `range`. Browser reload/back-forward restores the same view. Keyboard shortcuts are:

- `Alt+1…6`: the six pages;
- `Alt+ArrowLeft/ArrowRight`: previous/next market;
- `/`: focus the visible terminal-table search.

Shortcuts do nothing inside inputs, selects or editable elements and expose `aria-keyshortcuts` where applicable.

## Page Content

- **总览:** authoritative current state, latest event when idle, evidence health, market tape and auto-selected useful ledger.
- **收益:** cumulative/current result, realized/unrealized, target gap, benchmark, drawdown, contribution, range in URL and currency/source context.
- **过程:** current Process Book plus timestamped Process Event Ledger, evidence completeness and stage latency.
- **持仓:** quantity, cost, mark, market value, weight, day/cumulative PnL, contribution, risk, timestamp and source when available.
- **风险:** drawdown, boundary distance, blocked/review events, market and currency exposure, stale-domain risk and concentration.
- **复盘:** result, expected confidence, actual impact, reason, evidence state/gap, automatic calibration and strategy version only when sourced.

## Visual System

Preserve the current neo-industrial terminal system: near-black continuous canvas, hairline dividers, tabular mono numbers, 4px spacing rhythm, 2–5px radii, cyan/red/amber state semantics and no decorative gradients or glow. New controls must look embedded in table headers, not like SaaS cards.

## Error and Empty States

- Empty running state reveals completed or review data when present.
- Stale/error domains remain visible in evidence health without replacing valid data from other domains.
- Missing optional holding/process facts display `—`.
- A market with no summary remains selectable and shows `等待数据`, not fabricated zero performance.

## Acceptance

- Header count, runtime rail, market tape, Process Book and blotter agree for the same snapshot.
- Production scenario with `running=0` never shows an empty running tab while completed/review rows exist.
- All six pages expose the specified content using only sourced fields.
- Market tape, URL restoration, keyboard navigation, search, sort and column visibility work at both desktop viewports.
- No horizontal document overflow, no console errors and no new mutation endpoint/control.
- Existing and new tests, lint, frontend build and API build pass.
- Browser QA compares the 1440×900 implementation with the Hyperliquid reference and records a design score of at least 85/100.
