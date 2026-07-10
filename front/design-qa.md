# Terminal Grid Design QA

Date: 2026-07-11

Reference: current Hyperliquid trade screen captured at 1440×900.

Implementation states inspected:

- overview at 1440×900;
- returns, process, holdings, risk and review at 1440×900;
- all six navigation states at 1280×720;
- risk chart and ledger at both desktop viewports.

## Visual comparison

The Hyperliquid reference and final returns-terminal screenshot were inspected together at the same 1440×900 viewport. The implementation matches the intended structural grammar: compact top navigation and market strip, one continuous divided canvas, large primary data surface, fixed right inspector, dense bottom/history surfaces, tabular numbers and restrained cyan/red/amber semantics. TradingAgent intentionally replaces the reference order form with a read-only Automation Inspector and does not reproduce trading controls.

## Issues found and resolved

- Replaced secondary-page hero summaries and stacked cards with `TerminalPageShell`.
- Replaced the empty running-process panel with Process Book and completed-result fallback.
- Removed blocked terminal states from the running Process Book.
- Replaced the holdings donut with exposure bars and a currency-aware Portfolio Ledger.
- Suppressed duplicate ticker/name labels and prevented false mixed-currency totals.
- Added explicit 5% warning and 7% risk-limit context plus a Risk Ledger.
- Renamed the user-facing review field from `下次规则` to `自动校准`.
- Made header, terminal-strip, inspector and chart-derived return/drawdown values use one consistent display result.
- Increased the returns chart surface to remove the unexplained blank lower region.
- Compacted the review ledger so `自动校准` remains visible beside the inspector.

## Runtime checks

- No document or workspace horizontal overflow on any of the six pages at 1280×720.
- No document horizontal overflow at 1440×900.
- Process Book shows one genuinely pending row in the demo state; blocked records stay in risk/review surfaces.
- Review ledger exposes `自动校准` and contains no `下次规则` header.
- Browser console produced no errors or warnings during the six-page navigation pass.
- No order, buy/sell, queue-write, capital-control or strategy-edit control was introduced.

## Final result

final result: passed
