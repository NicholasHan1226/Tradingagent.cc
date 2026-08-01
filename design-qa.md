# TradingCopilot V4 Design QA

final result: passed

## Source

- Reference appshot: conversation appshot `Google Chrome Appshot 2026-08-01T19-45-42.827Z.png`
- Reference chart crop: `/var/folders/gg/h6vhh_j50tvg5x4ktqwgxy4r0000gn/T/TemporaryItems/NSIRD_screencaptureui_7weerO/截屏2026-08-02 02.19.50.png` (`1660 x 770`)
- Description: Perplexity Finance stock detail at desktop width, with left navigation, stock header and tabs, a wide quote/chart surface, and a narrow right rail containing company facts and consensus information. Lower-page references include significant moves, story/news cards, and paired bullish/bearish questions.

## Implementation capture

- Screenshot: `/var/folders/gg/h6vhh_j50tvg5x4ktqwgxy4r0000gn/T/tradingcopilot-v4-qa-1224x768.png`
- Same-size chart comparison screenshot: `/var/folders/gg/h6vhh_j50tvg5x4ktqwgxy4r0000gn/T/tradingcopilot-v4-qa-1660x770.png`
- URL state: `http://127.0.0.1:5174/?product=copilot`
- Viewport: `1224 x 768`
- Selected stock: `000400.SZ 许继电气`
- Tab and chart state: `概述`, `1D`, forecast hidden by default
- Data state: explicit `demo_fixture`; no claim of real-time market data

## Side-by-side comparison findings

The 1660 x 770 reference chart crop and 1660 x 770 implementation were emitted together in one visual comparison input; the full product was also checked against the 1224 x 768 conversation appshot. The implementation now matches the reference's important reading hierarchy:

- persistent dark left navigation;
- stock identity followed by a compact horizontal tab row;
- quote summary above a wide, high-density price and volume chart;
- chart range and prediction controls on the chart surface;
- fixed narrow right rail with company facts and a consensus-style decision card;
- muted borders, compact typography, low-radius cards, red A-share rise color and green decline color;
- lower-page significant-price, news/event, and paired bull/bear sections.

Product-specific differences are deliberate and bounded: the compact user-declared account strip remains because manual capital/holding maintenance is a core TradingCopilot task; the reference's analyst consensus is replaced by `Copilot 证据共识` so the product does not fabricate analyst coverage; the watchlist is a drawer so it remains available without permanently shrinking the chart.

## Focused regions

- Header and account strip: verified hierarchy and compact density; no clipping at 1224 px.
- Quote/chart: verified chart fills the primary column and 1D/1M interactions update state.
- Right rail: verified company facts, evidence score, support/oppose counts and sentiment module remain visible without horizontal overflow.
- Lower content: verified significant price change, symbol-bound announcement/news/sentiment cards and key bull/bear questions exist in the DOM and page flow.
- Mobile: verified at `390 x 844`; `scrollWidth === clientWidth === 390` and no horizontal overflow.

## Interaction and runtime checks

- Watchlist drawer opens and closes from the left navigation.
- `1D` forecast can be enabled; changing to `1M` disables the mismatched `m30` forecast.
- Analysis tab renders the buy-condition gate.
- Capital editor opens with the current declared capital and closes without mutation.
- Browser title is `TradingCopilot · A股人工决策台`.
- Browser console contained no warning or error entries.

## Iteration history

1. Initial implementation retained a permanent watchlist column; at 1224 px this compressed the chart and pushed the right rail out of the reference proportion.
2. Watchlist was converted to an accessible drawer and the main grid to chart + right rail.
3. The user account strip was reduced to a compact single row, moving the stock chart upward while preserving the manual-account requirement.
4. Company facts, evidence consensus, sentiment/event temperature, explicit price-move summary and source-bound event presentation were added and reverified.

## Residual boundary

This is a repository and local-browser UI acceptance result. It does not prove production deployment, formal real-time stock projections, calibrated prediction accuracy, broker connectivity, or trading profitability.
