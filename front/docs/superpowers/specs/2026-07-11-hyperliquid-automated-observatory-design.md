# Hyperliquid-Inspired Automated Observatory Design

**Date:** 2026-07-11  
**Status:** Approved
**Scope:** TradingAgent desktop frontend in `front/`  
**Reference:** Hyperliquid Trade desktop structure, translated to a read-only automated-system observatory

## 1. Product Position

TradingAgent is a fully automated trading-process and result observatory. The
frontend does not ask Nicholas to choose trades, approve opportunities, or
decide the next action. It shows what the automated system did, what stage it
is in, what result it produced, and why a process waited, failed, or was
blocked.

The frontend remains strictly read-only. This redesign does not add queue
writes, execution controls, order inputs, callbacks, account mutations, or a
real-money switch. Existing backend automation remains the only source of
process state and results. The reserved live-account mode remains gated until
the existing authorization and safety requirements are independently met.

## 2. Design Direction

The visual direction is restrained neo-industrial fintech with stronger
Hyperliquid structural fidelity:

- a 56px global navigation bar;
- a compact market and account strip instead of a report-style title block;
- a chart-led primary workspace with an approximately 72/28 left/right split;
- a right-side runtime rail that borrows the density of an order panel without
  borrowing order controls;
- a compact automated pipeline band below the chart;
- terminal-like result tables below the pipeline;
- charcoal-blue surfaces, hairline borders, compact typography, and restrained
  cyan, amber, and red state colors.

Hyperliquid is a layout and density reference, not a product-behavior template.
TradingAgent remains evidence-first, automated, and read-only.

## 3. Navigation and Information Architecture

The primary navigation becomes:

1. `总览`
2. `收益`
3. `过程`
4. `持仓`
5. `风险`
6. `复盘`

The existing `机会` and `决策` pages merge into `过程`. Opportunity discovery
is a pipeline stage, not a request for user action. Decision formation is an
automated system event, not a separate human-decision surface.

The shared navigation and market strip apply to all pages. The complete
Hyperliquid-like workspace geometry is required on `总览`; secondary pages
inherit the shell, tokens, compact tables, and language but keep their focused
content.

## 4. Homepage Regions and Content Contract

### 4.1 Global Navigation

The navigation shows the TradingAgent identity, the six page destinations,
automation state, notifications, and settings. It must not show a trading
action or imply manual execution.

### 4.2 Market and Account Strip

This strip answers: what system view is selected, how fresh is it, and what is
the latest result?

It shows:

- selected market;
- simulated or gated-live account mode;
- snapshot freshness;
- current return;
- target gap;
- open position count;
- running-process count;
- completed-result count;
- maximum drawdown.

It does not show a report headline, user recommendation, or duplicate review
count. A stale or unavailable snapshot remains visible as a first-class state.

### 4.3 Result Chart

The primary chart answers: how did the automated system's result change?

It shows:

- simulated return;
- target return;
- market benchmark;
- opportunity-cost series;
- selected time range;
- snapshot timestamp;
- automated process events tied to chart time.

The existing standalone return summary card is merged into the chart header so
the same return is not repeated in the market strip, card, and chart. The chart
retains its accessible summary and does not contain interactive descendants
inside `role="img"`.

### 4.4 Current Runtime Rail

The right rail answers: what is the automation doing now?

It replaces `当前审阅` with `当前运行` and displays one canonical current item,
selected in this order:

1. an active automated process;
2. a runtime wait or safety block;
3. the most recent completed result;
4. an idle state.

The rail shows:

- market and symbol;
- process name or strategy;
- current stage;
- start or last-update time;
- evidence state;
- runtime result or wait reason;
- total running-process count.

It must not show `下一步`, `还差什么`, a recommendation, an approval button, or
an order-like control. Navigation to a detailed process record may use a plain
text link or row selection, but the rail itself has no primary action button.

### 4.5 Automated Pipeline Band

The pipeline answers: where are automated jobs moving or stopping?

Its stages are:

1. `发现`
2. `研究`
3. `风控`
4. `模拟执行`
5. `结果写回`

It shows stage counts, throughput, the largest current bottleneck, and a compact
latest-event trace. It must distinguish active processes, safety blocks,
strategy waits, completed results, and replay-only evidence. It must not use
decorative particles or conversion language when no real pipeline evidence
exists.

### 4.6 Result Blotter

The bottom surface uses four tabs:

- `运行中`
- `持仓`
- `已完成`
- `自动复盘`

Each tab has its own content contract:

#### Running

- symbol and market;
- automated process or strategy;
- current stage;
- started or last-updated time;
- evidence state;
- runtime state or wait reason.

#### Positions

- symbol and market;
- position value or weight;
- unrealized result;
- portfolio role;
- risk state;
- source freshness when available.

#### Completed

- symbol and market;
- final outcome, including partial fills;
- automated strategy;
- result reason;
- realized or expected impact;
- completion time;
- evidence class.

#### Automated Review

- reviewed process or result;
- attribution or failure reason;
- safety block or anomaly class;
- measured impact;
- automatic rule or calibration change when such evidence exists;
- review timestamp.

The tables retain accessible `table`, `row`, `columnheader`, and `cell`
semantics. Terminal rows must not be reclassified as running work.

### 4.7 Evidence Area

Evidence panels remain below the primary blotter and appear only in the
relevant market context:

- market runtime and freshness;
- A-share opening-auction and closing-momentum evidence;
- A-share capital flow;
- A-share forward validation;
- A-share 50k/100k/200k tier experiments;
- cross-market runtime summaries.

These panels explain result trustworthiness. They do not provide actions,
recommendations, or approval prompts. Missing, stale, and error states remain
explicit.

## 5. Secondary Page Contracts

### Return

Shows amount, return percentage, target gap, maximum drawdown, realized and
unrealized result, trade count, the primary result chart, and contribution by
market or strategy. It does not repeat the same metrics in multiple cards.

### Process

Combines the old opportunity and decision surfaces. It shows current automated
jobs, stage distribution, evidence completeness, processing latency, safety
blocks, wait reasons, and completed process paths. It contains no human
decision language.

### Positions

Shows current automated simulated positions, exposure, contribution, portfolio
role, risk, and freshness. Allocation and concentration are secondary views.

### Risk

Shows drawdown and limit distance, high-risk positions, safety-blocked
processes, upstream data gates, and the risk timeline. It reports guardrail
behavior without inviting manual override.

### Review

Shows completed, partial, missed, cancelled, and blocked outcomes; attribution;
evidence class; measured impact; automatic calibration or rule change when
available; and timestamps. It never describes a terminal row as active work.

## 6. Content and Language Rules

- Use result and process language only.
- Remove `当前机会`, `需要复盘`, `还差什么`, `下一步`, `待处理`, and other
  phrases that imply Nicholas must intervene.
- Prefer `运行中`, `自动等待`, `安全拦截`, `结果写回`, `自动复盘`, and
  `运行空闲`.
- A metric has one primary value per viewport. Secondary occurrences may use a
  label or relationship, not another equally weighted number.
- Runtime reason codes are translated to plain language.
- Demo data remains local-preview-only. Production API failure shows an honest
  unavailable state.

## 7. Component and Data Architecture

`WorkbenchViewModel` remains the canonical selected-market and selected-account
truth. A new `AutomationObservatoryViewModel` composes that canonical output and
provides:

- canonical automation summary counts;
- the current runtime-rail item;
- running, completed, and automatic-review collections;
- pipeline stage summaries;
- translated runtime and evidence labels.

React components consume these derived values and do not independently
reclassify signal queue buckets or recompute process priority. Expensive
collection derivations stay memoized at the page boundary. Static configuration
and label maps remain module-level constants.

The public `Page` union changes to the six approved destinations. Legacy chart
events or internal links targeting `机会` or `决策` are normalized to `过程` in one
adapter so old snapshot records remain readable without keeping duplicate
navigation destinations.

The snapshot API remains read-only. Existing fields are reused first. If the
current read model lacks a process timestamp, evidence class, or automatic
calibration result, the frontend shows an explicit unavailable value instead of
inventing one. No API contract expansion is required unless implementation
proves that an essential approved field cannot be derived safely.

## 8. State, Error, and Safety Behavior

- `loading`: retain compact skeleton geometry.
- `empty`: distinguish idle automation from missing data.
- `stale`: keep the last result visible with a clear delayed-data state.
- `error`: isolate the failed domain and preserve other readable results.
- `live-gated`: gate the entire workspace across all navigation destinations.
- `strategy_wait`: describe an automatic strategy wait, not a task for the
  user.
- `needs_attention`: describe the safety or evidence block without presenting a
  manual override.

The frontend must not write signals, alter queue state, send messages, trigger
execution, or expose credentials. This redesign does not change automated
backend trading behavior.

## 9. Desktop Geometry and Responsive Scope

The accepted viewports are 1280x720 and 1440x900.

At 1280x720, the first viewport must contain:

- global navigation;
- market/account strip;
- chart and current runtime rail;
- complete automated pipeline band;
- blotter tabs, table header, and at least one result or running row.

At 1440x900, the blotter must show at least two rows when data exists. No
horizontal body overflow is allowed. Mobile and phone-specific information
architecture remain deferred by explicit user direction; existing smaller
breakpoints must not be deliberately broken.

## 10. Testing and Acceptance

### Unit and Component Tests

- navigation maps `机会` and `决策` behavior to `过程`;
- running, completed, and review classifications remain mutually consistent;
- partial fills remain terminal and read `部分成交`;
- the runtime rail contains no recommendation or manual-action language;
- stale and empty states retain truthful timestamps and labels;
- live mode remains globally gated;
- chart ARIA and table semantics remain valid.

### Build Checks

- `npm run lint`
- `npm test -- --run`
- `npm run build`
- `npm run build:api`
- `git diff --check`

### Rendered Browser Checks

- compare the accepted Hyperliquid reference and TradingAgent at 1280x720 and
  1440x900;
- verify page identity, non-blank content, no framework overlay, and no relevant
  console errors or warnings;
- verify navigation to every secondary page;
- verify market switching and simulated/live gate behavior;
- verify no buy, sell, order, approval, or confirmation control exists;
- verify the 1280x720 blotter header and first row are visible;
- capture final screenshots outside the repository.

## 11. Design QA Gate

The target score is at least 90/100:

- Visual hierarchy: 19/20
- Typography quality: 14/15
- Color semantics: 14/15
- Spacing rhythm: 14/15
- Interaction feedback: 9/10
- Accessibility baseline: 10/10
- Originality and brand fit: 9/10
- Responsive integrity: 3/5

Mobile remains intentionally deferred, so responsive integrity is capped. A
score below 85 blocks production sync and requires another desktop iteration.

## 12. Release and Three-Surface Sync

After implementation and validation:

1. integrate the feature branch with the latest local `main` without rewriting
   history or losing the newer production-validation documentation;
2. rerun the complete frontend validation on the integrated tree;
3. fast-forward or merge into local `main` using the repository's safe path;
4. push GitHub `main` and verify the remote commit;
5. update `/opt/investment/tradingagent` on the production host using the
   existing repository/deployment shape;
6. preserve the previous `front/dist` and `front/dist-server` as rollback
   artifacts;
7. build the static frontend and read-only API on production;
8. restart only `tradingagent-front-api.service` when the API build changes;
9. verify local API, Nginx/public dashboard, public snapshot route, deployed
   commit, and rendered frontend separately.

Local completion, GitHub `main`, production files, production runtime, and the
public route are separate facts and must be reported separately.

## 13. Out of Scope

- enabling real-money trading;
- adding order-entry controls;
- changing strategy thresholds, capital allocation, execution logic, queues,
  callbacks, or receipts;
- mobile information-architecture redesign;
- Cloudflare Pages as the active deployment source;
- unrelated backend refactors.
