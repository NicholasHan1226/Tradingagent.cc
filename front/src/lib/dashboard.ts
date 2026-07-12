import type { HoldingRow, Market, MarketSummary, PerformancePoint, PerformanceRange, PortfolioSummary, SignalRow } from '../types/dashboard'
import type { DomainHealth } from '../types/status'

export function getActionableSignals(rows: SignalRow[]) {
  return rows.filter((signal) => !isPartialOutcome(signal) && (signal.status === 'pending' || signal.status === 'blocked'))
}

export function getClosedSignals(rows: SignalRow[]) {
  return rows.filter((signal) => isPartialOutcome(signal) || signal.status === 'executed' || signal.status === 'missed' || signal.status === 'cancelled')
}

function isPartialOutcome(signal: SignalRow) {
  return signal.queueBucket?.toLowerCase() === 'partial'
}

export function getVisibleSignals(rows: SignalRow[], activeMarket: Market) {
  return rows.filter((signal) => activeMarket === 'All Markets' || signal.market === activeMarket)
}

export function getVisibleHoldings(rows: HoldingRow[], activeMarket: Market) {
  return rows.filter((holding) => activeMarket === 'All Markets' || holding.market === activeMarket)
}

export function getSelectedMarketSummary(rows: MarketSummary[], activeMarket: Market) {
  if (activeMarket === 'All Markets') return undefined
  return rows.find((summary) => summary.market === activeMarket)
}

export function getPortfolioForView({
  activeMarket,
  marketSummaries,
  portfolio,
}: {
  activeMarket: Market
  marketSummaries: MarketSummary[]
  portfolio: PortfolioSummary | null
}) {
  // DECOMMISSIONED: All Markets must never aggregate monetary values
  // (capital, equity, PnL, return, drawdown) across independent markets.
  // Only per-market views carry monetary authority.
  if (activeMarket === 'All Markets') return null

  if (activeMarket === 'A-share') {
    const targetPct = portfolio?.targetPct ?? 8
    if (portfolio) return portfolio
    const summary = marketSummaries.find((row) => row.market === 'A-share')
    if (!summary) return null
    return marketSummaryToPortfolio(summary, targetPct)
  }

  const summary = marketSummaries.find((row) => row.market === activeMarket)
  if (!summary) return null

  return marketSummaryToPortfolio(summary, portfolio?.targetPct ?? 8)
}

function marketSummaryToPortfolio(summary: MarketSummary, targetPct: number): PortfolioSummary {
  return {
    pnlAmount: summary.pnlAmount ?? 0,
    returnPct: summary.returnPct ?? 0,
    capitalBase: summary.capitalBase ?? 0,
    targetPct,
    maxDrawdownPct: Math.abs(summary.maxDrawdownPct ?? 0),
    tradeCount: summary.tradeCount,
    pointCount: 1,
    source: summary.source,
    pnlSource: 'market_summary',
    pnlCurrency: summary.pnlCurrency ?? 'CNY',
    realizedPnl: summary.realizedPnl,
    unrealizedPnl: summary.unrealizedPnl,
    updatedAt: summary.latestAt ?? new Date().toISOString(),
  }
}

export function getLivePerformanceData(now: Date, rows: PerformancePoint[], animateLatest = false) {
  if (!rows.length) return rows
  if (!animateLatest) return rows

  const seconds = now.getSeconds()
  const liveMove = (Math.sin(seconds / 4) + Math.cos(seconds / 7)) * 0.16

  return rows.map((point, index) => {
    if (index !== rows.length - 1) return point

    return {
      ...point,
      simulated: Number((point.simulated + liveMove).toFixed(2)),
      opportunity: Number((point.opportunity + liveMove * 0.2).toFixed(2)),
    }
  })
}

export function slicePerformanceData(rows: PerformancePoint[], range: PerformanceRange) {
  if (range === 'all' || rows.length <= 1) return rows

  const timedRows = rows
    .map((point) => ({ point, time: point.timestamp ? Date.parse(point.timestamp) : Number.NaN }))
    .filter((item) => Number.isFinite(item.time))

  if (timedRows.length) {
    const latestTime = Math.max(...timedRows.map((item) => item.time))
    const start = new Date(latestTime)
    if (range === 'today') {
      start.setHours(0, 0, 0, 0)
    } else {
      start.setDate(start.getDate() - (range === '7d' ? 7 : 30))
    }
    const filtered = timedRows.filter((item) => item.time >= start.getTime()).map((item) => item.point)
    if (filtered.length > 1 || range !== 'today') return filtered.length ? filtered : rows.slice(-1)
  }

  const fallbackSize = range === 'today' ? 2 : range === '7d' ? 7 : 30
  return rows.slice(Math.max(0, rows.length - fallbackSize))
}

export function isStaleHealth(health: DomainHealth, maxAgeMs: number, now = new Date()) {
  return now.getTime() - new Date(health.updatedAt).getTime() > maxAgeMs
}

export function getSignalFunnel(rows: SignalRow[]) {
  const discovered = rows
  const formed = rows.filter((signal) => signalStageRank(signal) >= 1)
  const conditioned = rows.filter((signal) => signalStageRank(signal) >= 2)
  const riskPassed = rows.filter((signal) => signalStageRank(signal) >= 3 && signal.status !== 'blocked' && signal.status !== 'cancelled')
  const tradeSignals = riskPassed.filter((signal) => signal.status === 'executed' || signal.status === 'pending' || signal.status === 'missed')
  const stages = [
    { label: '发现', rows: discovered },
    { label: '研判', rows: formed },
    { label: '风控', rows: conditioned },
    { label: '待确认', rows: riskPassed },
    { label: '结果', rows: tradeSignals },
  ]
  const stageDrops = stages.map((stage, index) => index === 0 ? 0 : Math.max(0, stages[index - 1].rows.length - stage.rows.length))
  const hasScreeningEvidence = rows.some((signal) => signal.stageEvidence === 'full' || (signal.stageEvidence === 'partial' && signal.stage !== '成交')) || stageDrops.some((drop) => drop > 0)
  const hasReplayEvidence = rows.length > 0 && rows.every((signal) => signal.stageEvidence === 'replay' || signal.stage === '成交')
  const mode = rows.length === 0 ? 'empty' : hasScreeningEvidence ? 'screening' : hasReplayEvidence ? 'replay' : 'partial'

  return {
    mode,
    hasScreeningEvidence,
    stageDrops,
    stages,
    tradeSignals,
    executed: tradeSignals.filter((signal) => signal.status === 'executed'),
    pending: tradeSignals.filter((signal) => signal.status === 'pending'),
    missed: tradeSignals.filter((signal) => signal.status === 'missed'),
    blocked: conditioned.filter((signal) => signal.status === 'blocked'),
    cancelled: conditioned.filter((signal) => signal.status === 'cancelled'),
  }
}

export function getHomeOutcome(signals: SignalRow[], holdings: HoldingRow[]) {
  const actionable = getActionableSignals(signals)
  const closed = getClosedSignals(signals)
  const leadSignal = actionable.find((signal) => signal.status === 'pending') ?? actionable[0]
  const blockedSignal = signals.find((signal) => signal.status === 'blocked' || signal.status === 'cancelled')
  const reviewSignal = closed.find((signal) => signal.status === 'missed') ?? closed[0]
  const leadingHolding = holdings[0]

  return {
    leadSignal,
    blockedSignal,
    reviewSignal,
    leadingHolding,
  }
}

function signalStageRank(signal: SignalRow) {
  if (signal.stage === '成交' || signal.stage === '错过') return 4
  if (signal.stage === '待执行') return 3
  if (signal.stage === '风控' || signal.stage === '拒绝') return 2
  if (signal.stage === '评分') return 1
  if (signal.stage === '发现') return 0
  if (signal.steps >= 6) return 4
  if (signal.steps >= 5) return 3
  if (signal.steps >= 4) return 2
  if (signal.steps >= 2) return 1
  return 0
}
