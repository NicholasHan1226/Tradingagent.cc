# TradingCopilot Research Terminal — Design QA

Date: 2026-08-03

## Comparison evidence

- Source visual truth: `conversation://browser-comment-1` — Nicholas-provided
  Perplexity Finance AMZN screenshot, 1026×994 px. The annotated left navigation
  rail is explicitly out of scope and was not used as a target.
- Implementation: browser-rendered `http://127.0.0.1:5173/?product=copilot&demo=1`,
  captured at 1026×994 CSS px in the dark demo-research state.
- Density normalization: the source and implementation use the same 1026×994
  viewport; no device frame, browser chrome, or density scaling was included in
  the comparison.
- Focused regions compared: compact masthead/search, current-stock header and
  chart, and the fixed right-side company/evidence rail. A separate focused crop
  was unnecessary because the source and implementation kept those regions
  visible in the same full-view capture.

## Findings

- No actionable P0/P1/P2 differences remain for the requested translation.
  The implementation intentionally uses a 30-stock tracking ribbon and a
  personal-state strip where the reference has product-specific cards; those are
  required TradingCopilot functions rather than copied Perplexity features.
- Typography: compact sans-serif hierarchy, small terminal labels, tabular quote
  treatment, and restrained weights match the reference's dense research rhythm.
  The product name and Chinese copy are intentionally original.
- Spacing and layout rhythm: the old left rail is absent; the header and
  navigation are compact, the chart owns the broad left region, and one fixed
  research rail owns the right region. At 1026 px `scrollWidth === clientWidth`.
- Colors and visual tokens: near-black surfaces, hairline dividers, muted gray
  metadata, teal navigation/state accents, red price direction, and amber
  forecast state retain the reference's visual grammar without reproducing its
  branding.
- Image and icon fidelity: no source logo, illustration, or raster asset was
  copied. The interface uses the existing product mark and Lucide icons; this is
  an intentional original-brand deviation, not a substitute for a required
  source asset.
- Copy and state: the tracking ribbon says `等待现役清单投影` until a verified
  session list exists. The chart exposes `研究演示 · 概率停显` and its 30-minute
  horizon before the user opens the research overlay; it does not present a
  probability, target price, or recommendation as market fact.

## Iteration history

1. **P1 — legacy side navigation competed with the terminal composition.**
   Removed the sidebar markup and stale CSS. Post-fix browser evidence found
   `legacySidebarNodes: 0` and one fixed right research rail.
2. **P2 — forecast state was discoverable only after opening the chart control.**
   Added the third quote summary with the horizon, readiness state, and visible/
   hidden overlay state. Post-fix browser evidence shows the forecast summary in
   the chart header; page tests verify the forecast control and the probability
   stop gate.
3. **P2 — mobile regression risk after adding a third quote summary.**
   Collapsed the quote summary to one column below 760 px. At 390×844,
   `scrollWidth === clientWidth` and no legacy sidebar is present.

## Runtime checks

- Browser-rendered desktop capture at 1026×994: no horizontal overflow; one
  `.copilot-rail`, one `.tracking-ribbon`, and zero `.copilot-sidebar` nodes.
- Browser-rendered 1280 px local demo check after the per-dataset activity-state
  slice: the new strip is visible between A-share rules and detail tabs, renders
  `demo_fixture · 时钟覆盖缺口`, and does not alter the existing right rail or
  chart controls. The coverage-gap text is intentional because the current
  shared projection contract does not yet carry per-dataset clock authority.
- Browser-rendered mobile capture at 390×844: no horizontal overflow.
- Browser console: no warnings or errors observed in the inspected demo state.
- Primary interactions checked: top navigation visible, stock search entry
  visible, forecast control visible, read-only tracking-pool empty state visible.

## Follow-up polish

- P3: when the A-share session writer publishes the verified 30-stock file,
  capture a populated tracking-ribbon state and review its horizontal scrolling
  density at desktop and mobile widths.

## Final result

final result: passed
