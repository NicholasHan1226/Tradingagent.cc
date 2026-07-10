# Terminal Grid Design QA

Date: 2026-07-11

Reference: current Hyperliquid trade screen captured at 1440×900.

Implementation states inspected:

- overview at 1440×900;
- returns, process, holdings, risk and review at 1440×900;
- all six navigation states at 1280×720;
- risk chart and ledger at both desktop viewports.
- terminal operations layer at 1440×900 and 1280×720, including market/evidence tape, process event ledger, sourced holdings, evidence-domain risk rows and review controls.

## Visual comparison

The Hyperliquid reference and final returns-terminal screenshot were inspected together at the same 1440×900 viewport. The implementation matches the intended structural grammar: compact top navigation and market strip, one continuous divided canvas, large primary data surface, fixed right inspector, dense bottom/history surfaces, tabular numbers and restrained cyan/red/amber semantics. TradingAgent intentionally replaces the reference order form with a read-only Automation Inspector and does not reproduce trading controls.

Quantitative design gate: **92/100**. Hierarchy 19/20, continuous-grid fidelity 19/20, information density 18/20, state/data truth 20/20, interaction/accessibility 16/20. The remaining gap from the reference is intentional product translation: TradingAgent uses line/evidence views instead of candlesticks/order entry, and keeps desktop-only inner ledger scrolling for wide evidence tables.

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
- Added a six-market tape and five-domain evidence-health layer without introducing floating cards.
- Added Process Event Ledger source/latency/reason columns and sourced portfolio cost/mark/quantity fields.
- Added compact search, sorting and native column visibility controls to process, event, portfolio, risk and review ledgers.
- Fixed a real-browser `/` shortcut issue found during QA: focus now occurs on key-up so the slash never pollutes the search query.

## Runtime checks

- No document or workspace horizontal overflow on any of the six pages at 1280×720.
- No document horizontal overflow at 1440×900.
- Process Book shows one genuinely pending row in the demo state; blocked records stay in risk/review surfaces.
- Review ledger exposes `自动校准` and contains no `下次规则` header.
- Browser console produced no errors or warnings during the six-page navigation pass.
- URL state restored `page=收益&range=7d` on back and `page=过程&range=7d` on forward; `Alt+1…6`, editable-control guards and `/` search focus were verified.
- All six pages reported `documentElement.scrollWidth === clientWidth` at 1280×720; returns, process, holdings, risk and review also passed at 1440×900.
- Process, portfolio, risk and review ledgers exposed a visible local search and accurate result count; portfolio wide columns remained inside the ledger scroll container rather than widening the document.
- No order, buy/sell, queue-write, capital-control or strategy-edit control was introduced.

## Final result

final result: passed
