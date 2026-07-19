# Explicit Market Attribution & Coverage Observability Design

> **Historical design — endpoint portion superseded on 2026-07-16.** The active reader no
> longer routes to per-provider/special endpoints. Current contract is explicit V1 catalog/query
> with no legacy or SQLite fallback; see `front/docs/integration.md`.

## Goal

Make the next TradingAgent terminal layer operationally useful without fabricating cross-market data: explicit market-data identifiers activate multi-market pulses, single-origin simulated positions become safely attributable to opportunities, and source-coverage observations become visible as a short read-only history.

## Current evidence

The production snapshot has one A-share representative and no current positions. SharedSignals requires exact identifiers for `/crypto`, `/pm_prices`, and futures reads. The A-share local simulation ledger aggregates positions by symbol, so a position with fills from multiple order IDs cannot truthfully be assigned to one opportunity.

## Selected design

### Explicit representative identifier

`SignalRow` and `HoldingRow` gain optional `marketDataSymbol`. The snapshot reader accepts only explicit source fields `market_data_symbol` or `marketDataSymbol` (including the existing source object where applicable). A-share keeps its existing exchange-qualified security code as the compatible explicit identifier. CNFutures, Crypto, PM, US, and HK require `marketDataSymbol`; their displayed trading symbol is never converted or guessed.

The market-pulse reader selects this field in preference to display symbol, sends it unchanged to the matching SharedSignals endpoint, and keeps `no_representative` when it is absent. This gives upstream producers one narrow contract without adding providers, endpoint writes, or static symbol lists.

### Safe single-origin position attribution

The A-share local simulated snapshot preserves `order_id` and `opportunity_id` only when every recorded buy origin contributing to an aggregated open position has the same original order. It preserves the position's existing `unrealized_pnl`; it does not allocate account-level realized PnL across open positions.

If more than one source order remains in an aggregated position, the fields are omitted. The frontend then remains unlinked rather than assigning the position to a convenient same-symbol signal. Existing historical trade facts and signed receipts remain append-only and unchanged.

### Coverage observation trace

The server keeps the most recent bounded sequence of fresh coverage observations in process memory, keyed by the configured SharedSignals base URL. Cached reads reuse the existing history and never add artificial samples. Each snapshot exposes this optional history alongside current coverage. Restarting the service resets it to an honest one-observation state.

The evidence edge of the terminal shows a compact `轨迹 N` indicator and accessible text describing the latest coverage sample. It does not claim durable monitoring or a database-backed SLA; the server's existing SharedSignals health tooling remains the durable source-health authority.

## Constraints

- The frontend snapshot route remains read-only; no queue, order, capital, cron, account, or provider call changes.
- Exact ID propagation may enrich only future simulated position snapshots; it never rewrites historical snapshots, ledgers, receipts, or completed events.
- Multi-order positions, absent mappings, failed reads, and restarts remain visibly incomplete.
- Desktop terminal grammar stays neo-industrial and Hyperliquid-aligned: small semantic evidence labels in the existing tape, no new card grid or action surface.

## Acceptance

- A Crypto/PM/CNFutures signal with `marketDataSymbol` calls the exact expected endpoint; the same signal without it stays unmapped.
- Two fresh source reads produce two history observations; cache hits do not grow history.
- A single-origin open A-share position exposes its explicit order/opportunity ID and unrealized PnL; a mixed-origin position exposes neither attribution ID.
- The UI renders coverage trace and explicit-only PnL context with a clear empty state.
- Frontend and focused Python tests, lint, frontend/API builds, desktop QA, and layered release checks pass.
