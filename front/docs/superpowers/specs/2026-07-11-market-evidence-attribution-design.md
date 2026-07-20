# Market Evidence & Attribution Design

> **Historical naming notice (2026-07-20):** This document preserves implementation history. The current upstream product is TradingDatas, and TradingAgent consumes only `GET /v1/catalog` and `POST /v1/query`. Old SharedSignals runtime routes, SQLite access, and dual registries are not dependencies or fallbacks. Compatibility file and function identifiers may retain historical names.

## Goal

Extend the read-only TradingAgent terminal with verifiable multi-market market-data coverage and explicit opportunity-to-position/PnL attribution, without guessing symbols or changing any execution path.

## Evidence from the current snapshot

The public snapshot at the start of this iteration contains four A-share signals, no holdings, and one stale A-share market pulse. It contains no safe Crypto, CNFutures, or PM representative identifier. Therefore the terminal must communicate coverage rather than manufacture a wider market tape.

## Selected approach

Three approaches were considered:

1. Guess canonical instruments per market (for example BTCUSDT or a liquid futures contract). This gives a fuller strip quickly but fabricates a relationship to TradingAgent evidence and is rejected.
2. Add a static market-symbol configuration. This is useful only after an owned mapping contract and operational maintenance path exist, neither of which is in scope.
3. Reuse only explicit symbols already present in holdings or signals, and expose exact coverage diagnostics for every market. This is selected because it preserves the read-only evidence boundary and becomes richer automatically as verified markets enter the snapshot.

## Read model contract

`readSharedSignalsMarketPulses` becomes a bounded result reader rather than returning only an array. It returns:

- `pulses`: normalized source rows exactly as today.
- `coverage`: one item for each supported market with `sourced`, `no_representative`, `unavailable`, or `degraded` state; a representative symbol is included only when it came from an existing holding or signal.
- cache state, source fetch time, and total request latency. Cached data keeps its original source time and is labelled cached rather than pretending to be fresh.

All requests remain parallel, use the existing 900ms timeout and 15-second cache, and remain fail-soft. The snapshot remains valid when SharedSignals is unavailable.

## Explicit attribution contract

`HoldingRow` gains optional `opportunityId`, `realizedPnl`, and `unrealizedPnl` fields. Position parsers copy them only from explicit source fields (`opportunity_id`, `opportunityId`, trace/signal/order identifiers where the existing source schema carries them). No same-symbol join is allowed.

The linked opportunity context consumes funnel events, signals, and holdings. It displays related signal count, related holding count, and attributable PnL only for equal explicit opportunity IDs. If no linked holding exists, the UI states that PnL attribution is not yet available.

## Terminal composition

The market tape keeps its Hyperliquid-style compact instrument rows. Its evidence edge adds one concise `行情读模型` status line: sourced/requested count, missing-mapping count, cache state, latency, and source time. The line is diagnostic, not a trading signal.

The persistent opportunity context retains its compact horizontal strip and gains `信号`, `持仓`, and `可归因盈亏` facts. Empty attribution remains a dash and does not become zero.

## Constraints and acceptance

- Read-only frontend and snapshot API only; no queue, account, capital, strategy, cron, execution, or upstream writes.
- Do not infer or normalize an identifier across markets without an explicit source contract.
- Missing data displays `—`; market coverage makes the absence explainable.
- Tests cover all coverage states, cache truthfulness, explicit holding parsing, explicit-only attribution, and component copy.
- Lint, full frontend tests, client build, API build, and desktop visual checks at 1280x720 and 1440x900 must pass.

## Design-system compatibility

This is a neo-industrial calm-fintech compatibility enhancement: existing 12px/11px terminal type hierarchy, semantic cyan/amber/red state colors, 4px spacing rhythm, restrained 120/180/240ms motion, visible focus states, and desktop-first layout stay unchanged. The new facts use existing tape/context components rather than adding card rows or decorative treatments.
