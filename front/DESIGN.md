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
- `--market-up/down/flat` describe sourced price direction, while `--fresh-live/stale` describe data age and never trading intent;
- no decorative glow, gradients, large radii, floating cards, or artificial asset graphics.

Hyperliquid structures are translated to the product boundary: its market ticker becomes the snapshot strip, chart remains a chart, order book becomes Process Book, order form becomes Automation Inspector, and positions/orders/history become read-only ledgers.

The terminal operations layer adds a three-lane market tape (`A-share`, `CNFutures`, `Crypto`) and evidence-health block below the account header. `All Markets` remains a non-monetary status aggregate. The strip keeps return, holdings count, runtime truth, snapshot freshness and five data-domain states visible without introducing another card row.

The evidence-adaptive layer adds four explicit runtime states: `live`, `idle`, `stale` and `degraded`. Top navigation, market header, page metrics and inspectors consume one heartbeat model, so a healthy scheduler with no pending process reads `调度正常 · 当前空闲` instead of claiming automation is running. Internal values such as `buy`, `sell`, `empty` and raw source codes are translated before rendering.

The market-causal layer adds sourced representative-instrument pulse, persistent opportunity context and local terminal controls. Market pulse is read through the TradingAgent snapshot API from TradingDatas V1 only; its coverage line distinguishes sourced, unmapped, unavailable and degraded markets instead of filling the tape with assumed instruments. Selecting an opportunity cycle writes the explicit `opportunityId` into the URL, highlights the cycle and filters its event stream while preserving the context across all secondary pages. The linked strip may show signals, holdings and PnL only when their explicit opportunity IDs match; no same-symbol attribution is inferred. `Cmd/Ctrl+K` opens the terminal command palette; density and table-column preferences remain browser-local.

The explicit-attribution layer requires `marketDataSymbol` for non-A-share pulse reads, so cross-market display symbols never become implicit API identifiers. It adds a bounded in-process coverage trace to the evidence edge and preserves a local A-share position order ID only when its aggregate contains one recorded buy origin. The trace is a short session observation, not an SLA or persistent history; multi-origin positions remain intentionally unlinked.

## Shared page anatomy

`TerminalPageShell` owns the secondary-page structure:

1. compact context strip: selected market, return, drawdown, process state and holdings;
2. primary data surface: chart or ledger;
3. Automation Inspector: derived status, distribution and evidence, with no actions;
4. optional bottom ledger for related events.

Secondary pages do not use `PageSummaryBoard`. Empty running state reveals recent completed results when available instead of reserving a large blank panel.

## Page content contract

- `总览`: return chart, runtime inspector, compact process strip and `运行中 / 持仓 / 已完成 / 自动复盘` blotter. If running is empty, the blotter opens the most relevant non-empty result tab.
- `收益`: equity/target/benchmark chart as the primary surface; ranked contribution and realized/unrealized result in the inspector. Flat or one-point evidence uses a 300px quiet chart plus sample/realized/unrealized strip; meaningful movement restores the full 520px chart.
- `过程`: opportunity cycles group `funnelEvents[]` into `发现 → 研判 → 风控 → 待确认 → 结果`, preserving missing stages instead of inferring them. The raw event ledger remains below for timestamp/source/reason audit detail; Process Book is the fallback when no explicit events exist.
- `持仓`: Portfolio Ledger with sourced quantity, average/mark price, cost, market value, derived portfolio weight, PnL, contribution and risk. When empty, the surface collapses to sourced exposure, available cash, latest closed process and snapshot time; absent optional evidence remains `—`.
- `风险`: drawdown chart with 5% warning and 7% hard-limit context, market exposure inspector, and Risk Ledger for blocked/missed/cancelled records plus stale/error/live-gated evidence domains.
- `复盘`: completed result ledger with confidence, impact, evidence and automatic calibration. User-facing copy is `自动校准`, never an instruction for manual action.

## Data trust rules

- Use snapshot fields only. Missing facts display `—`, never synthetic zeroes.
- Holdings totals are currency-aware: A-share and CNFutures exposure use their independent CNY authorities, Crypto can use USD, percentages remain percentages, and `All Markets` never creates a false monetary sum.
- Suppress an asset name when it duplicates the ticker.
- Terminal rows preserve result states such as partial fill, safety block, missed and cancelled; terminal records never return to the running queue.
- Display timestamps and source health as observed. Never use the browser clock to disguise stale data.
- The state resolver is authoritative: only `pending` is running; executed/partial are completed; blocked/missed/cancelled are review states. When a snapshot invalidates the active blotter tab, the next useful non-empty tab is selected without overriding deliberate empty-tab inspection.

## Interaction and accessibility

- Navigation, market filtering, account-mode gate, time ranges and blotter tabs remain keyboard operable.
- `Alt+1…6` opens the six pages, `Alt+←/→` moves through markets, and `/` focuses the visible ledger search unless focus is already in an editable control.
- URL query keys `page`, `market` and `range` restore the same terminal view on reload/back-forward.
- Process, event, portfolio and risk ledgers share local-only search, sort direction and native column visibility controls. These controls never mutate snapshot data.
- Every terminal region, table, inspector and tab has an accessible name.
- Focus-visible styling stays present and restrained.
- Motion is functional, subtle and disabled under `prefers-reduced-motion`.
- The live-account surface stays a read-only readiness gate until authorization, risk checks and execution receipts are independently verified.

## Implementation map

- `src/components/terminal/`: shared terminal shell and ledgers.
- `src/lib/terminalViewModels.ts`: currency-safe, read-only display models.
- `src/lib/terminalStateResolver.ts`: authoritative running/completed/review resolution.
- `src/lib/marketTapeViewModel.ts` and `src/lib/processEventViewModel.ts`: market/evidence strip and event-audit rows.
- `src/lib/runtimeHeartbeat.ts`, `terminalDensity.ts` and `processCycleViewModel.ts`: shared runtime truth, evidence-aware density and grouped opportunity cycles.
- `src/server/sharedSignalsMarketPulse.ts`: compatibility-named, bounded, cached, fail-closed-per-dataset TradingDatas V1 HTTP enrichment for representative instruments.
- `src/lib/linkedEvidenceContext.ts` and `terminalPreferences.ts`: explicit opportunity correlation and versioned local view state.
- `src/components/terminal/MarketSparkline.tsx`, `LinkedEvidenceContext.tsx` and `TerminalCommandPalette.tsx`: market pulse, correlation and desktop command surfaces.
- `src/hooks/useTerminalNavigation.ts`: URL and keyboard presentation state.
- `src/components/workbench/`: overview workbench and result-tab selection.
- `src/pages/ThemePage.tsx`: composition for the five secondary pages.
- `src/App.css`: terminal tokens, canvas, grid and dense table rules.
- `src/api/` and `src/server/`: backward-compatible read-only snapshot contract with optional sourced `marketPulses[]` and `marketPulseCoverage`; missing or degraded upstream evidence stays absent rather than becoming synthetic chart movement.

The implementation is accepted only after unit/component tests, lint, frontend/API builds, real-browser desktop checks, no-horizontal-overflow checks and a reference/implementation visual comparison all pass.
