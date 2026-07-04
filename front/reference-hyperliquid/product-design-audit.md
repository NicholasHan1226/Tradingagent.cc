# Hyperliquid Product Design Audit

Date: 2026-07-04

Audit mode: combined UX, visual design, and accessibility risk audit using the Product Design audit framework.

## Audit Scope

Reference product: Hyperliquid web app.

Reviewed surfaces:

1. Trade page
   - Evidence: live Chrome state observed through Computer Use on `app.hyperliquid.xyz/trade`; saved visual reference `trade.png`.
   - Health: strongest reference for dense market workspace and one-accent dark terminal language.
2. Portfolio page
   - Evidence: saved visual reference `portfolio.png`.
   - Health: strongest reference for result-oriented account view.
3. Vaults page
   - Evidence: saved visual reference `vaults.png`.
   - Health: strongest reference for sparse discovery and table-first layout.
4. Leaderboard page
   - Evidence: saved visual reference `leaderboard.png`.
   - Health: useful reference for ranking, search, and list density.
5. Earn, Staking, Referrals
   - Evidence: saved captures exist, but unauthenticated views were mostly shell/header.
   - Health: low confidence as visual references for this dashboard.

## User Goal And Accessibility Target

For TradingAgentDashboard, the goal is not to copy a trading terminal. The user needs an automated trading monitoring surface:

- see real-time return clearly,
- understand current holdings and risk,
- know what decisions happened,
- know which signals were missed,
- keep background operations out of the way.

Accessibility target for the prototype: clear reading order, sufficient contrast, stable chart and table structure, obvious active state, no reliance on glow or color alone for critical status.

## What Hyperliquid Is Really Doing

Hyperliquid feels clean because it does less styling than it first appears to do.

The product relies on five moves:

1. The black base is the main design asset.
   - The page is not decorated with gradients, cards, or lighting effects.
   - Depth comes from small value shifts: black, near-black, muted separators.

2. One accent color carries active intent.
   - Cyan/teal means active, connected, selected, tradable, or actionable.
   - It appears as text, underline, button fill, small tags, and chart elements.
   - It is not spread across many competing decorative surfaces.

3. Numbers lead, labels support.
   - Price, account value, volume, PNL, TVL, and drawdown are visually dominant.
   - Labels are small and muted. They do not fight the numbers.

4. Layout is task-shaped.
   - Trade is dense because trading needs chart, order book, and order form at once.
   - Portfolio is calmer because the task is account review.
   - Vaults and Leaderboard are list-first because the task is comparison.

5. Tables are treated as premium UI.
   - Rows, tabs, filters, and hairline dividers do the work.
   - Hyperliquid does not avoid tables; it makes them feel like the product's native grammar.

## Strengths

The strongest design pattern is restraint. Hyperliquid uses a very narrow visual vocabulary and repeats it everywhere: dark panels, thin borders, muted labels, cyan active states, white numbers, table rows.

The second strength is density control. The Trade page is dense but not visually loud, because the color system is strict. Portfolio is much more open, proving that the same design language can handle both a cockpit and a calm account review page.

The third strength is interaction clarity. Active tab, active market, active account mode, selected interval, and primary action are easy to find because they share the same accent behavior.

## UX Risks

The Trade page is too execution-heavy to copy directly. If TradingAgentDashboard copies the order form, order book, and chart density, it will imply manual trading, which conflicts with the intended product: backend analysis and automated decisions.

The chart-first layout can hide causality. Hyperliquid is built for market action; TradingAgentDashboard must additionally show why the system acted or skipped. That requires decision and signal explanation, but those explanations should use Hyperliquid's table/list grammar instead of more colorful cards.

The product has low onboarding explanation by design. That works for experienced traders, but an automated AI trading dashboard needs clearer Chinese copy for outcomes like "已执行", "错过", "风控拦截", "等待确认", and "实盘预留".

## Accessibility Risks

Some Hyperliquid states rely heavily on color and low-contrast text. From screenshots alone, likely risks are:

- muted labels may be hard to read in dark mode,
- small table rows and controls may become difficult at zoom or smaller displays,
- red/green/cyan status could be ambiguous without text labels,
- TradingView iframe accessibility depends on its embedded implementation and keyboard behavior.

This audit cannot claim WCAG compliance because it did not test keyboard focus order, zoom reflow, screen reader announcements, or all interactive states.

## Opportunity Areas For TradingAgentDashboard

1. Make "动态实时收益" the product's Portfolio page.
   - It should be the calm result surface: large current return, today's change, target distance, drawdown, account state, and one dominant chart.
   - It should not compete with multiple small charts.

2. Treat "持仓与收益来源" as account review, not trading.
   - Use rows and compact totals.
   - Show position, market, exposure, floating P/L, today contribution, risk state.
   - Do not show buy/sell controls.

3. Turn "错过机会" into a table-first decision surface.
   - Hyperliquid's Leaderboard/Vaults style is a better model than alert cards.
   - Columns should answer: what, why missed, estimated impact, whether repeatable, what changed next.

4. Simplify the decision flow.
   - Keep it visually distinct from the signal log.
   - Use one quiet flow visual for system-level formation, then tables for detail.
   - Avoid colored rivers that look decorative unless each band has a clear user question.

5. Reduce the side navigation weight.
   - Hyperliquid's top navigation proves that a finance product can feel powerful without a heavy sidebar.
   - If the sidebar remains, keep it as quiet routing, not a second dashboard.

6. Keep color stricter than the current prototype.
   - Primary accent: cyan/teal.
   - Secondary states: red for risk/loss, amber for warning/missed.
   - Everything else should be neutral black/grey/white.

## Recommended Dashboard Structure

The best structure is not a trading page. It is a monitoring page built from Portfolio plus selected Trade and Leaderboard patterns:

1. Top status band
   - Market scope, account mode, data freshness, live state.
   - Thin, quiet, no dashboard cards.

2. Primary result area
   - Dynamic return hero with one large number and one chart.
   - Target, benchmark, drawdown, and missed-alpha lines are secondary.

3. Account and holdings area
   - Account summary rows plus top positions table.
   - This gives the user "where is my money and what caused the result?"

4. Decision formation area
   - One system-level flow graphic.
   - The purpose is causal overview, not every event.

5. Signal and missed-opportunity tables
   - Row-first, filterable, compact.
   - This is where detail belongs.

6. Right rail
   - Live alert queue only.
   - No decorative mini charts, no stacked marketing cards.

## Direct Implications For The Current Prototype

What is already aligned:

- near-black base,
- reduced decorative cards,
- simulated account active with live account reserved,
- real-time return as the top decision surface,
- right rail as alert list,
- Chinese-first outcome copy.

What still needs improvement:

- the sidebar still takes more visual attention than Hyperliquid would allow,
- the decision flow can be calmer and more structural,
- signal log and missed-opportunity detail should become more table-native,
- color should be even more disciplined: fewer semantic colors visible at once,
- typography should make numbers feel more premium than labels and chips.

## Product Design Recommendation

The next design pass should not add more polish. It should subtract:

- fewer panels,
- fewer accents,
- fewer chips,
- fewer micro visualizations,
- stronger numeric hierarchy,
- more table/list structure,
- one unmistakable real-time return area.

The reference direction is: Hyperliquid Portfolio for results, Hyperliquid Leaderboard/Vaults for tables, and Hyperliquid Trade only for market-density discipline.

