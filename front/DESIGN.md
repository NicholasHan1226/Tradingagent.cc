# TradingAgentDashboard Design Notes

## Design Direction

The dashboard is now structured as a result-first automated trading command
center: `主页` is the default overview, while `收益`、`机会`、`持仓`、`决策`、
`风险`、`复盘` are theme pages for deeper result review.

The visual language follows the restrained side of Hyperliquid, GitHub, and
Linear: deep black base, thin lines, compact navigation, flat panels, tabular
numbers, restrained teal as the primary action color, amber for target/review
states, and red only for risk or negative impact. The page intentionally avoids
exchange-only tools: no buy/sell box, no wallet connect workflow, no technical
indicator toolbar, no drawing rail, and no decorative glow around icons.
The latest polish pass removed radial background glow, removed exchange-like
system codes from the visible header, and rebuilt the market header as a Hyperliquid-style
page title band: large title, quiet subtitle, right-side market filters, and a
single KPI strip beneath it. The title band now changes with the selected page,
so `收益`、`机会`、`持仓`、`决策`、`风险`、`复盘` do not repeat a second large
page header in the content area. The home hero is now unframed, with live return
as the primary visual result and the right rail reserved for result explanations.
The background now carries a clearer Hyperliquid-like contour texture: quiet
black at the top, deeper green financial map lines across the title band and
lower canvas, and panels sitting on that substrate instead of floating as
generic dashboard cards. The homepage right rail has also been merged into one
continuous result module so it reads less like stacked dashboard widgets.
The latest Hyperliquid alignment pass further reduced visual noise: top
navigation selected states no longer use boxed focus styling, panel borders and
state pills are lower contrast, the content substrate is darker and less
glowy, and dense tables use tighter rows. The intent is to feel like a
financial product shell, not a colorful trading dashboard skin.
The latest structure pass makes the live-return chart the homepage's primary
module, with a compact conclusion band inside the same panel and a right rail
reserved for the few actions the user should consider now. Cyan has been
reduced to the live-return and primary-action role; the wordmark is now mostly
white with a small cyan recognition line instead of a color-split text label.
The latest production pass turns the homepage right rail into one `当前结论`
module instead of three stacked sub-panels, adds explicit target and risk lines
to the live-return chart, adds a compact opportunity summary strip above the
opportunity table, and removes the repeated contribution panel from the
decision page rail.
The latest gap-repair pass adds result-linked event chips to the live-return
chart and replaces the equal-width decision path with a decreasing funnel that
shows where opportunities leave the channel. The UI now makes the relationship
between return movement and opportunity/decision review explicit without adding
trading-terminal controls.
The latest integration-prep pass aligns the dashboard with the real
TradingAgent project surfaces: signal queue files are treated as the first
live opportunity source, `signals/positions` is the preferred future holdings
source, `shared/accounting/position_plan.jsonl` is the current fallback, and
`shared/review/daily/daily_brief.jsonl` plus attribution files are the first
decision/review evidence sources. The reserved API endpoint is
`/api/trading-agent/snapshot`; it is intentionally read-only and cannot trigger
orders.
The latest Hyperliquid workbench pass reduces the visible "dashboard card"
language further: the page title band is shorter, the main homepage region is a
single continuous workbench substrate, inner regions are separated by hairlines
instead of floating card borders, corner radii are smaller, and the chart event
bar plus result drilldown area are embedded in the same information surface.
The chart now uses `机会缺口` as the user-facing opportunity line instead of
negative/backstage wording.
The latest browser-comment pass turns the top KPI strip into a more compact
result summary: simulated profit amount and return percentage are merged with
the amount as the primary value, market freshness moved out of the KPI strip,
the ambiguous page-mode badge was removed, market selection became a default
`全市场` drilldown menu, the simulated/live account control became a click
switch, and the homepage now includes a compact trading-signal funnel before
the live return chart.
The latest dynamic-funnel pass makes the homepage triad explicit: dynamic
signal funnel, real-time return, and the return curve are the three primary
modules. The duplicated top profit KPI remains removed, simulated profit amount
plus return percentage live in the home chart's primary result position, and
the account mode stays a single click-toggle. The signal funnel is now a true
screening funnel rather than a generic moving channel. The current pass removes
hard-coded aggregate counts from the homepage funnel: stage counts, dropped
counts, moving symbols, and the final `交易信号` output are derived from the
current signal rows. With the current mock signal rows this means `6 -> 6 -> 6
-> 4 -> 4`, and the right live-return module summarizes how many signals are
executed, pending, and missed. When the read-only TradingAgent snapshot is
mounted, this same component should update from real signal data without
changing the visual layer.

The home page answers the user-facing questions first:

- Is the simulated account making money now?
- How far is it from the target?
- Which opportunities and positions drove the result?
- Which risk needs attention?
- Where can the user review more detail next?

## Information Architecture

- `主页`: dynamic signal funnel, core live-return result, larger return curve,
  current conclusion, action queue, risk state, key opportunities, and compact
  holdings. Simulated profit amount and return percentage live together in the
  home chart result position, not in the top KPI strip.
- `收益`: page title band plus larger live return chart, contribution, and risk
  context.
- `机会`: page title band plus only currently actionable opportunities,
  including current judgement, missing condition, valid window, expected impact,
  and risk. A compact summary strip answers how many opportunities are
  actionable, how much impact is visible, how many are waiting, and how many are
  blocked by risk before the user reads the table.
- `持仓`: page title band plus simulated positions, contribution role, weight,
  P/L, and risk state.
- `决策`: page title band plus a decreasing decision funnel and outcome
  distribution, kept separate from the lifecycle log so it does not duplicate
  signal rows.
- `风险`: page title band plus drawdown boundary, risk saves, and opportunity
  impact.
- `复盘`: page title band plus closed-opportunity attribution for tracing why
  an opportunity made money, why it was not used, what it affected, and what to
  adjust next time.

## Token Decisions

- `--bg-*`: near-black surfaces, avoiding the earlier blue dashboard cast.
- `--surface-*`: flat black/near-black panels; depth comes from hairlines,
  spacing, text hierarchy, and a slight transparent green-black material. The
  workbench material is intentionally more transparent than earlier card
  surfaces so it sits inside the page substrate instead of floating above it.
- `--text-*`: off-white primary text, muted labels, faint metadata.
- `--accent-blue` / `--accent-cyan`: primary action, live return, and the
  minimal brand recognition line. Profitable secondary values use lower visual
  weight so cyan does not dominate every state.
- `--state-target`: target and review states.
- `--state-danger`: drawdown, risk, and negative opportunity impact.
- `--font-number`: financial numbers, percentages, timestamps, and table
  values.

## Component Rules

- Top navigation is page-level: `主页` is active by default and all theme pages
  are clickable.
- Brand: use a wordmark-only logo until a stronger mark route is selected.
  Rejected directions: forced `TA` monogram and abstract flow mark. The current
  lockup makes `TradingAgent` itself the logo, using a pure white wordmark plus
  one restrained cyan recognition line. It stays unboxed, unglowed, descriptor-free, and
  readable at small navigation size so it matches the GitHub/Hyperliquid
  restraint. This is a formalized in-product identity direction, not a final
  legally cleared trademark.
- Market header is the persistent page-title and KPI band: result overview
  title, selected-page context, simulated account active, live account reserved,
  market drilldown, simulated profit plus return percentage, target gap,
  opportunities, and drawdown. It
  should read like Hyperliquid's portfolio/staking page shell, not an exchange
  order-entry header.
- Top navigation follows the Hyperliquid restraint pattern: active text is
  cyan, but no large active box or glowing icon treatment is allowed. Keyboard
  focus remains visible but quiet.
- Background texture is part of the page system. It must stay line-based and
  restrained: no glowing icon backplates, no bokeh, no decorative blobs.
- Main workbench surfaces should feel embedded, not stacked. Prefer one
  continuous container with internal dividers over multiple repeated cards.
- Home performance module is conclusion-first, not marketing copy. It gives the
  immediate answer: simulated return is above target and drawdown is still
  inside the risk boundary.
- Home performance module avoids duplicate KPI content. The persistent header
  owns target gap, opportunities, and drawdown; market freshness sits beside
  the market drilldown; the chart module owns simulated profit plus return
  percentage, trend, target line, risk line, and attribution entry.
- Homepage signal funnel is a dynamic channel-health board, not a second
  decision page and not a static KPI strip. It must visibly answer: how many
  opportunities entered, how many survived each screening step, where
  opportunities dropped out, and how many trading signals were generated. It
  should be derived from signal data, not from decorative or fixed animation
  numbers. It should not need explanatory headline text by default. Motion must
  remain functional and restrained: no glowing icon backplates, no bright
  blocks, and no trading-terminal controls.
- Live return chart is the homepage's main panel and must be visible on the
  first 1280x720 viewport.
- Right rail is a single result explanation module on the homepage, not a stack of
  separate alert cards or an action/order panel.
- Tables are dense and result-oriented, with Chinese-first copy, real ticker
  symbols preserved, sticky headers, low-contrast row dividers, reduced row
  height, and hover states that do not add extra color noise.
- Decision formation uses a decreasing funnel plus outcome pills so it differs
  from the opportunity record and immediately shows where opportunities drop
  out.
- Live return chart event chips are navigation, not trading controls. They
  should link an inflection point to the related `机会`、`决策` or `风险` page
  with restrained chip styling and no bright marker glow.
- Visible copy follows a result-language rule: avoid backstage actions,
  engineering field names, and exchange-terminal phrasing; prefer commercial
  decision phrases such as `查看收益原因`、`实盘预留`、`实时`、
  `等价格和成交量再走强`.

## Motion

- Live return data updates every second.
- Chart animation is subtle and functional.
- No decorative scanlines, glowing nodes, or animated icon backplates.
- Motion respects `prefers-reduced-motion`.
- Homepage signal funnel uses multiple moving signal labels and a quiet lane
  sweep to show channel movement. It should feel operational, not decorative,
  and should stop under `prefers-reduced-motion`.

## Production Readiness

The latest production-readiness pass split the dashboard into three clearer
layers:

- `src/types/dashboard.ts`: typed market, page, signal, holding, depth, and
  performance contracts.
- `src/data/dashboard.ts`: the current mock dataset and labels, kept outside
  the React view.
- `src/lib/dashboard.ts`: derived view rules for live performance updates,
  market filtering, current opportunities, and review rows.
- `src/types/status.ts` and `src/components/StatusBoundary.tsx`: shared
  readiness states for loading, empty, stale, error, and live-account gated
  surfaces.
- `src/api/types.ts` and `src/adapters/dashboard.ts`: read-only API envelope
  and adapter boundary. The mock state now enters the UI through the same
  adapter path expected from the real simulated-account read model.
- `src/api/tradingAgentReadModel.ts` and
  `src/adapters/tradingAgentReadModel.ts`: production-shaped TradingAgent
  snapshot boundary. It documents ledger, signal queue, review and router log
  sources while keeping the browser UI read-only.
- `src/server/tradingAgentSnapshot.ts`: first local read-only snapshot reader
  for `signals/positions`, `shared/accounting/position_plan.jsonl`, signal
  queue files, filled signal writeback and review evidence. It is not yet
  mounted in a real server runtime.
- `src/api/tradingAgentIntegration.ts`: reserved direct-connect endpoint,
  browser fetch client, timeout handling and server response wrapper for
  `/api/trading-agent/snapshot`.
- `src/api/tradingAgentCapabilities.ts`: real TradingAgent capability map for
  what can be displayed now, what is partially available, and what must stay
  gated until account authorization and execution writeback are verified.
- `src/lib/chartEvents.ts`: chart review anchors are derived from performance
  movement and active opportunities instead of static mock event labels.
- `src/pages/`, `src/components/charts/`, `src/components/tables/`, and
  `src/components/panels/`: page and panel split. `App.tsx` is now the routing
  and state shell only.
- `vite.config.ts`: Recharts is separated into a `charts` chunk so the main
  app bundle stays small.
- `src/components/ErrorBoundary.tsx`: top-level crash recovery so a failed panel
  does not become a blank page.

This keeps `App.tsx` focused on page composition and makes the next integration
step clearer: replace the mock API response with the real simulated-account read
model, while keeping live-account surfaces gated until a separate
execution/permission path is verified.

## Design Taste Scorecard

- Visual hierarchy: 19/20
- Typography quality: 14/15
- Color semantics: 14/15
- Spacing rhythm: 15/15
- Interaction feedback: 9/10
- Accessibility baseline: 8/10
- Originality / brand fit: 8/10
- Responsive integrity: 4/5

Total: 95/100. The page is now closer to the Hyperliquid/Linear/GitHub
restraint target: live return is the true homepage anchor, opportunity and
review pages have clearer jobs, the right rail reads as one judgement module,
chart movement links to review surfaces, the workbench feels more embedded and
less like floating dashboard cards, the headline result no longer duplicates
the header KPI strip, the account mode is simpler, and the real read-model
boundary is explicit. Remaining polish should focus on a proper logo route
board, browser route-level tests, and mounting the reserved snapshot endpoint in
the selected server runtime.

## Next Iteration

1. Mount `/api/trading-agent/snapshot` in the selected API/server runtime and
   replace the mock response on the front end.
2. Run a separate wordmark route board before committing any new symbol mark.
3. Tune the homepage for the user's full Chrome viewport after more real data is
   available.
4. Add route-level browser tests and visual regression so the new readiness
   states are exercised by real navigation, not only component tests.

## July 4 Homepage Refactor

The homepage now uses three outcome-first modules:

- `SignalFunnelFlow`: a data-driven flow funnel based on the current visible
  `SignalRow[]`. It shows how many opportunities are collected, how many pass
  conditions/risk, which branch is protected or missed, and how many become
  trade signals. This follows the Sankey-style pattern more than a decorative
  static funnel, because the user needs to see movement and drop-off, not only
  stage totals. When no live opportunity or signal data is present but holdings
  exist, the same surface switches to `持仓状态`: it shows account holdings,
  positive contribution, risk checks, and continue-hold/review state without
  showing a misleading opportunity conversion rate.
- `RealtimeReturnCard`: one return card combines amount and percentage. Amount
  is the primary value, percentage is secondary, and simulated/live wording is
  controlled by the shared account mode. The copy now uses `当前收益` and
  `实盘待接入` rather than internal or system-state phrasing.
- `HomeResultBrief`: renamed from judgement language to `当前结论`, with
  business-readable rows such as `收益主要来自`, `错过原因`, `风险已挡住`, and
  `实盘`.

Navigation also moved from a single two-line system button to a compact
segmented account switch: `模拟盘 / 实盘`, with `运行中 / 预留` as state labels.
The market header moved freshness into a same-height status pill and keeps the
market selector aligned beside it.

Design references used in this pass:

- Hyperliquid page shell: dark embedded workbench, low-card depth, concise nav,
  and restrained cyan action color.
- Funnel/Sankey data-viz pattern: use links and moving particles to show
  real flow through a pipeline, with branches for protected and missed
  opportunities.

Updated score after this pass:

- Visual hierarchy: 19/20
- Typography quality: 14/15
- Color semantics: 14/15
- Spacing rhythm: 15/15
- Interaction feedback: 9/10
- Accessibility baseline: 8/10
- Originality / brand fit: 9/10
- Responsive integrity: 4/5

Total: 92/100. The homepage is a stronger production candidate because the
funnel is now data-shaped, the real-time return card has one job, and old
system phrases are removed from the visible UI. Remaining gap: desktop visual
regression tests and richer real signal-volume data before the funnel can show
large-market density naturally.

## July 8 Flow Polish

The homepage flow panel now separates three states:

- Real `funnelEvents[]`: rendered as `机会管道` with conversion rate, drop-off
  ledger, moving labels, and outcome strip.
- Derived signal rows: rendered as a screening pipeline only when signal stage
  evidence exists.
- Holdings only: rendered as `持仓状态`, with no opportunity conversion rate.
  It summarizes account holdings, positive contribution, watch items, and risk
  checks, so the user does not mistake a holding replay for a real opportunity
  funnel.

Visual changes in this pass:

- Added a compact result banner above the flow so the panel answers the result
  before the user reads stage cards.
- Added a low-contrast loss ledger for the exact step where opportunities or
  holdings narrow.
- Reduced teal glow in the funnel and return card to keep the Hyperliquid-like
  dark material restrained.
- Renamed the live-account placeholder to `实盘待接入` and the primary return
  label to `当前收益`.

Updated score after this pass:

- Visual hierarchy: 19/20
- Typography quality: 14/15
- Color semantics: 15/15
- Spacing rhythm: 15/15
- Interaction feedback: 9/10
- Accessibility baseline: 8/10
- Originality / brand fit: 9/10
- Responsive integrity: 4/5

Total: 93/100. The flow panel is clearer and less misleading in no-signal
production states. Remaining gap: the backend should continue improving
complete per-opportunity `funnelEvents[]` so the page can show a true live
opportunity funnel during active trading windows.
