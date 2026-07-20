# Market-Causal Terminal Design

> **Historical naming notice (2026-07-20):** This document preserves implementation history. The current upstream product is TradingDatas, and TradingAgent consumes only `GET /v1/catalog` and `POST /v1/query`. Old SharedSignals runtime routes, SQLite access, and dual registries are not dependencies or fallbacks. Compatibility file and function identifiers may retain historical names.

## Goal

Move TradingAgent from a Hyperliquid-inspired read-only shell to an evidence-dense market terminal: real sourced market pulse, a persistent opportunity-to-result context, and faster desktop navigation without adding execution controls.

## Design direction

Keep the existing neo-industrial, calm-fintech canvas and make it more market-native. The next layer is not more cards or routes; it is a continuous information fabric where price movement, process state, portfolio evidence and review context share the same compact grammar.

## P0: sourced market pulse

The TradingAgent snapshot API may read a bounded set of representative symbols from the configured SharedSignals HTTP read model. It never calls providers directly and never writes upstream. A pulse contains market, symbol, latest price, change, session range, volume, update time, source and a short sourced point series. Unsupported or unavailable markets remain explicit `—`; no synthetic movement is rendered.

The market tape becomes a two-line instrument strip. Return and runtime truth remain visible while the selected representative symbol adds last price, change, freshness and a tiny line plot. Pulse requests use a short timeout and an in-process cache so the five-second dashboard refresh cannot create an upstream request storm.

## P1: persistent causal context

`opportunityId` is the primary correlation key. Selecting an opportunity cycle writes `opportunity=<id>` to the URL, highlights the cycle, filters the raw event ledger and exposes a compact linked-context bar on secondary terminal pages. The context resolves symbol, market, current stage, result, evidence completeness and update time from existing funnel events. Clearing the context restores the unfiltered view.

No relationship is fabricated. Holdings and signals may be described as related only when their explicit opportunity ID matches, or when the selected cycle itself supplies the symbol as display context. The URL remains presentation state only.

## P2: local terminal efficiency

Add a keyboard command palette opened with `Cmd/Ctrl+K`. Commands cover the six pages, seven market filters, density and clearing linked context. `Alt+1…6`, `Alt+←/→` and `/` remain intact.

Density has `compact` and `comfortable` modes. It is stored in a versioned local preference object and applied as a root data attribute. Table column visibility is also persisted per ledger key. Local storage failure falls back silently to defaults; no preference reaches the server.

## Design tokens

- `--market-up`, `--market-down`, `--market-flat`: price-direction semantics.
- `--fresh-live`, `--fresh-stale`: source freshness semantics.
- `--delta-pulse`: restrained data-change feedback.
- `--terminal-row-compact`, `--terminal-row-comfortable`: density rhythm.
- Motion remains 120ms feedback / 180ms transition / 240ms reveal and is disabled under reduced motion.

## Component contract

- `MarketSparkline`: pure visual summary with accessible text, never inventing points.
- `MarketTape`: selected-market control plus real pulse display.
- `LinkedEvidenceContext`: global read-only correlation strip with clear action.
- `ProcessCycleLedger`: selectable rows with keyboard and selected state.
- `TerminalCommandPalette`: focus-trapped desktop command chooser with Escape close.
- `TerminalDataTable`: versioned per-ledger column preferences.

## Acceptance

- SharedSignals failure does not fail the TradingAgent snapshot.
- Pulse data is bounded, cached, sourced and truthful when absent.
- Selecting a process cycle updates URL state and filters its raw event stream.
- Reload/back-forward restores page, market, range and opportunity context.
- Command palette and density work with keyboard only.
- No queue, account, capital, strategy, cron or execution path changes.
- 1280×720 and 1440×900 desktop views have no horizontal page overflow or console errors.
