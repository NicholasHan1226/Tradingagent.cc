# Hyperliquid Reference Audit

Date: 2026-07-03

Scope: read-only visual and information-architecture review of Hyperliquid app pages for TradingAgentDashboard design direction.

## Pages Reviewed

- `Trade`: `/trade`
  - Screenshot: `reference-hyperliquid/trade.png`
  - Text capture: `reference-hyperliquid/trade.json`
- `Portfolio`: `/portfolio`
  - Screenshot: `reference-hyperliquid/portfolio.png`
  - Text capture: `reference-hyperliquid/portfolio.json`
- `Vaults`: `/vaults`
  - Screenshot: `reference-hyperliquid/vaults.png`
  - Text capture: `reference-hyperliquid/vaults.json`
- `Leaderboard`: `/leaderboard`
  - Screenshot: `reference-hyperliquid/leaderboard.png`
  - Text capture: `reference-hyperliquid/leaderboard.json`
- `Earn`, `Staking`, `Referrals`
  - Text captures were saved. In the current unauthenticated in-app browser session, the visible screenshots rendered mostly as shell/header only, so they are lower-confidence visual references.

## Core Visual Principles

1. Black first
   - The product does not rely on rich gradients or decorative backgrounds.
   - Most depth is created by dark value changes and hairline borders.
   - Panels feel embedded in the terminal, not floated above it.

2. One active accent
   - Active state is usually cyan/teal.
   - The accent appears as text, underline, fine border, or small chip.
   - Large colored backgrounds are rare and reserved for primary actions such as `Connect`.

3. Thin structure
   - Navigation, tables, chart areas, and stats are divided by hairlines.
   - Rows are dense but readable because labels are muted and numbers are bright.
   - Borders are more important than shadows.

4. Plain numeric hierarchy
   - Large numbers carry pages: price, account value, TVL, volume, fees, PNL.
   - Labels stay muted and compact.
   - Numbers use strong contrast and tabular alignment.

5. Functional density
   - Trade is very dense because it is an execution workspace.
   - Portfolio is much calmer: headline, a few large account metrics, one chart/table area.
   - Vaults and Leaderboard are table-first with strong page titles and simple filters.

6. Minimal motion
   - The pages do not depend on decorative motion for quality.
   - Live feeling comes from changing market/account data and chart updates.

## Page-Specific Lessons

### Trade

Useful for TradingAgentDashboard:
- Top symbol strip: selected market + key stats in one horizontal band.
- Central chart dominates the workspace.
- Order book and order panel are compact side modules, not decorative cards.
- Bottom tabs are operational tables with direct labels.

Do not copy:
- Buy/sell execution form. The TradingAgent dashboard is not a manual trading interface.
- Overly dense order book layout for decision intelligence; use only if showing market microstructure.

### Portfolio

Useful:
- Best reference for automated trading dashboard result view.
- Large `Portfolio` title, then a small set of major metrics.
- PNL, Volume, Max Drawdown, Total Equity are shown as plain rows, not fancy cards.
- Tabs below are practical and table-oriented.

Apply to our dashboard:
- Keep `动态实时收益` as the largest result.
- Convert secondary metrics into simple rows/bands.
- Use a holdings panel shaped more like account summary + table, less like dashboard cards.

### Vaults

Useful:
- Large page title + one main total value metric + search + table.
- Very simple hierarchy.
- Strong example that a finance page can be sparse and still feel professional.

Apply:
- For future opportunity/missed-signal pages: title, one big total, one search/filter, one table.
- Avoid adding decorative charts when the user's task is finding an item.

### Leaderboard

Useful:
- Search first, then table.
- Few columns, clear sorting context, muted explanatory note.
- Wide empty space is acceptable when it improves focus.

Apply:
- Signal log and missed opportunities should be table-first.
- Do not fill empty space with decorative panels.

### Earn / Staking / Referrals

Current visual confidence is lower because the unauthenticated browser rendered mostly shell/header. Text indicates action-oriented surfaces (`Supply`, `Withdraw`, `Connect`, referral actions), but these are less relevant to the TradingAgent dashboard.

## Design Rules To Apply

1. Remove decorative microcharts unless they answer a specific user question.
2. Avoid nested cards. Prefer sections separated by hairlines.
3. Use one dominant chart on the page. Other data should be text/table/list.
4. Keep secondary state colors narrow: one-pixel strips, tiny dots, small text.
5. Large result numbers should be more important than panel chrome.
6. Right rails should be alert lists, not stacked cards.
7. Empty space is acceptable if it creates calm and focus.
8. User-facing copy should be result-oriented Chinese; ticker symbols and exchange terms can remain as real labels.

## Implications For Current Dashboard

- Current minimal-terminal direction is closer than previous clean-vivid direction.
- Next UI pass should further reduce:
  - repeated panel borders,
  - card-like side modules,
  - decorative flow thickness,
  - excessive status chips.
- The best next structural target is:
  - top: market/account/status header,
  - primary: realtime return + one chart,
  - secondary: holdings/account summary and decision flow,
  - tertiary: signal/missed-opportunity table and alert rail.

