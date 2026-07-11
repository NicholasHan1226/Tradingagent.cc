# TradingAgent Evidence-Adaptive Terminal Design

**Date:** 2026-07-11
**Status:** Approved through the user's instruction to execute all recommended optimizations
**Scope:** `front/` desktop UI at 1280x720 and 1440x900

## Objective

Move the existing Hyperliquid-inspired shell from fixed visual density to evidence-driven terminal density. Empty, idle, stale, degraded and active states must look and read differently while every visible fact remains derived from the existing read-only snapshot.

## Chosen Approach

Use pure frontend view models on top of the existing snapshot contract. A single runtime-health model owns scheduler state, current activity, latest meaningful event and freshness. A density model decides whether returns and holdings render as active ledgers, compact evidence summaries or trustworthy empty states. Funnel events are grouped into opportunity cycles without mutating or extending execution data.

Alternatives rejected:

1. CSS-only compaction leaves contradictory status language and repeated raw events.
2. A new observability backend is broader than this UI optimization and introduces another production boundary.
3. Fabricated market motion or placeholder holdings would violate the evidence-first product contract.

## Hard Boundaries

- No queue writes, orders, fills, cancellations, callbacks, account actions, capital changes, strategy edits, cron changes, mail or webhooks.
- No fabricated holdings, prices, timestamps, stages or live activity.
- Existing snapshot fields remain authoritative; absent facts render as `—` or an explicit waiting state.
- Mobile remains deferred. Desktop layouts must remain valid at 1280x720 and 1440x900.

## Runtime Truth Model

`createRuntimeHeartbeat` derives one of four presentation states:

- `live`: at least one real pending process;
- `idle`: scheduler/snapshot is healthy but no process is running;
- `stale`: the selected evidence exceeds the existing freshness threshold;
- `degraded`: a domain is error/live-gated or the market reports an execution fault.

It also exposes a concise headline, latest meaningful event age, snapshot age and tone. Header, page metrics and inspectors consume the same model. With zero pending processes the product says `调度正常 · 当前空闲`, never `自动化运行中`.

## Adaptive Density

### Returns

When the selected portfolio has meaningful performance evidence, retain the full chart. When results are flat or have fewer than two meaningful points, reduce the chart surface and add a compact evidence strip showing sample count, last meaningful movement, realized/unrealized availability and snapshot freshness.

### Holdings

When positions exist, retain Portfolio Ledger. When holdings are empty, replace the large blank table with an `EvidenceEmptyState` containing current exposure, latest closed process, why capital is not deployed when sourced, and freshness. It must not reconstruct historical positions that the snapshot does not contain.

## Opportunity Cycle Ledger

Group funnel events by `opportunityId`; fall back to `market + symbol` only when no ID exists. Each cycle contains ordered stages, the latest sourced result, start/update time, total latency, evidence completeness and source labels. The primary process surface shows cycle rows first; the raw event ledger remains below for full audit detail.

Stages are rendered as a compact strip: `发现 -> 研判 -> 风控 -> 待确认 -> 结果`. Missing stages remain visibly absent and are never inferred. Strategy/source codes are translated through one presentation helper so values such as `buy` and `empty` do not leak into user-facing labels.

## Visual System

- Direction: neo-industrial, calm-fintech, evidence-first terminal.
- Canvas: near-black continuous workbench with hairline separators; no card mosaic, gradients or decorative glow.
- Typography: existing sans UI and tabular mono numeric treatment; 11/12/13/16/20 scale.
- Spacing: 4/8/12/16/24 rhythm; radii remain 2-5px.
- State tokens: `--state-live`, `--state-idle`, `--state-stale`, `--state-degraded`.
- Motion: 120ms data-change pulse only when sourced values change; disabled under `prefers-reduced-motion`.

## Components

- `AutomationHeartbeat`: shared runtime truth line and freshness.
- `AdaptiveTerminalSurface`: adds `active`, `quiet` or `empty` density semantics.
- `EvidenceEmptyState`: compact sourced explanation for absent results or positions.
- `ProcessCycleLedger`: opportunity-cycle rows with stage strip and raw-event continuity.

All interactive table controls preserve visible focus, keyboard access and existing URL/navigation behavior.

## Acceptance

- Zero running processes render `调度正常 · 当前空闲` everywhere.
- Stale/error states override idle wording and show their latest timestamp.
- Flat returns no longer consume the full active-chart height.
- Empty holdings do not render a giant blank table or the literal value `empty`.
- Process page groups repeated events into opportunity cycles and keeps the raw ledger below.
- No user-facing `buy`, `sell`, `empty`, `signal_queue`, `sim_ledger` or `opportunity_log` strings remain in the affected surfaces.
- All existing tests, new tests, lint, frontend build and API build pass.
- Browser QA at both desktop viewports has no document overflow or console errors and scores at least 90/100.

