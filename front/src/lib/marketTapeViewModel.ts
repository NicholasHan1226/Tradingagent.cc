import { marketLabels, markets } from '../data/dashboard'
import type { Market, MarketPulse, MarketPulseCoverage, MarketSummary } from '../types/dashboard'
import type { DashboardState, DataDomain, DomainStatus } from '../types/status'

export type MarketTapeRow = {
  market: Market
  label: string
  selected: boolean
  returnLabel: string
  holdingsLabel: string
  runtimeLabel: string
  freshnessLabel: string
  tone: 'positive' | 'negative' | 'warning' | 'muted'
  pulse?: {
    symbol: string
    priceLabel: string
    changeLabel: string
    detailLabel: string
    freshness: MarketPulse['freshness']
    points: number[]
  }
}

export type EvidenceHealthModel = {
  overall: 'positive' | 'warning' | 'negative'
  snapshotLabel: string
  sourceLabel: string
  items: Array<{ domain: DataDomain; label: string; state: string; tone: 'positive' | 'warning' | 'negative' | 'muted' }>
}

export type MarketPulseHealthModel = {
  headline: string
  detail: string
  tone: 'positive' | 'warning' | 'negative' | 'muted'
}

const DOMAIN_LABELS: Record<DataDomain, string> = { performance: '收益', signals: '信号', holdings: '持仓', decisions: '复盘', risk: '风险' }

export function createMarketTapeRows(summaries: MarketSummary[], activeMarket: Market, generatedAt: string | null, pulses: MarketPulse[] = []): MarketTapeRow[] {
  const byMarket = new Map(summaries.map((summary) => [summary.market, summary]))
  const pulseByMarket = new Map(pulses.map((pulse) => [pulse.market, pulse]))
  return markets.map((market) => {
    if (market === 'All Markets') return createAllMarketsRow(summaries, activeMarket, generatedAt)
    const summary = byMarket.get(market)
    if (!summary) return { market, label: marketLabels[market], selected: activeMarket === market, returnLabel: '—', holdingsLabel: '0 持仓', runtimeLabel: '等待数据', freshnessLabel: snapshotTime(generatedAt), tone: 'muted' }
    const returnPct = summary.returnPct
    return {
      market,
      label: marketLabels[market],
      selected: activeMarket === market,
      returnLabel: returnPct === undefined ? '—' : signedPercent(returnPct),
      holdingsLabel: `${summary.holdingCount} 持仓`,
      runtimeLabel: runtimeLabel(summary),
      freshnessLabel: snapshotTime(summary.latestAt ?? generatedAt),
      tone: toneForSummary(summary),
      pulse: createPulse(pulseByMarket.get(market)),
    }
  })
}

function createPulse(pulse?: MarketPulse): MarketTapeRow['pulse'] {
  if (!pulse) return undefined
  return {
    symbol: pulse.symbol,
    priceLabel: formatPrice(pulse.lastPrice),
    changeLabel: pulse.changePct === undefined ? '—' : signedPercent(pulse.changePct),
    detailLabel: `H ${formatOptionalPrice(pulse.high)} · L ${formatOptionalPrice(pulse.low)}`,
    freshness: pulse.freshness,
    points: pulse.points,
  }
}

export function createEvidenceHealth(domains: DashboardState['domains'], generatedAt: string | null, marketSummary?: MarketSummary): EvidenceHealthModel {
  const items = (Object.keys(DOMAIN_LABELS) as DataDomain[]).map((domain) => {
    const health = domains[domain]
    return { domain, label: DOMAIN_LABELS[domain], ...statusCopy(health?.status ?? 'empty', health?.message) }
  })
  const overall = items.some((item) => item.tone === 'negative') ? 'negative' : items.some((item) => item.tone === 'warning') ? 'warning' : 'positive'
  return { overall, snapshotLabel: snapshotTime(generatedAt), sourceLabel: marketSummary?.source ?? '只读快照', items }
}

export function createMarketPulseHealth(coverage?: MarketPulseCoverage): MarketPulseHealthModel | undefined {
  if (!coverage) return undefined
  const unmapped = coverage.entries.filter((entry) => entry.status === 'no_representative').length
  const unavailable = coverage.entries.filter((entry) => entry.status === 'unavailable').length
  const degraded = coverage.entries.filter((entry) => entry.status === 'degraded').length
  const detail = [
    unmapped ? `${unmapped} 市场待映射` : undefined,
    unavailable ? `${unavailable} 请求不可用` : undefined,
    degraded ? `${degraded} 源已降级` : undefined,
    coverage.cacheState === 'cached' ? '已缓存' : '实时读取',
    `${coverage.sourceLatencyMs}ms`,
  ].filter((value): value is string => Boolean(value)).join(' · ')
  const tone = coverage.sourcedCount === coverage.requestedCount && !unavailable && !degraded
    ? 'positive'
    : coverage.sourcedCount > 0 || unmapped > 0
      ? 'warning'
      : coverage.requestedCount > 0
        ? 'negative'
        : 'muted'
  return { headline: `${coverage.sourcedCount}/${coverage.requestedCount} 已取到`, detail, tone }
}

function createAllMarketsRow(summaries: MarketSummary[], activeMarket: Market, generatedAt: string | null): MarketTapeRow {
  const capital = summaries.reduce((sum, row) => sum + (row.capitalBase ?? 0), 0)
  const pnl = summaries.reduce((sum, row) => sum + (row.pnlAmount ?? 0), 0)
  const returnPct = capital > 0 ? (pnl / capital) * 100 : undefined
  const hasAttention = summaries.some((row) => row.executionFault || row.runtimeState === 'needs_attention')
  const isWaiting = summaries.length > 0 && summaries.every((row) => row.runtimeState === 'strategy_wait' || row.runtimeState === 'empty')
  return {
    market: 'All Markets', label: marketLabels['All Markets'], selected: activeMarket === 'All Markets',
    returnLabel: returnPct === undefined ? '—' : signedPercent(returnPct),
    holdingsLabel: `${summaries.reduce((sum, row) => sum + row.holdingCount, 0)} 持仓`,
    runtimeLabel: hasAttention ? '需要关注' : isWaiting ? '策略等待' : summaries.length ? '正常' : '等待数据',
    freshnessLabel: snapshotTime(generatedAt), tone: hasAttention ? 'negative' : isWaiting ? 'warning' : summaries.length ? returnPct !== undefined && returnPct < 0 ? 'negative' : 'positive' : 'muted',
  }
}

function runtimeLabel(summary: MarketSummary) {
  if (summary.executionFault || summary.runtimeState === 'needs_attention') return '需要关注'
  if (summary.runtimeState === 'strategy_wait') return '策略等待'
  if (summary.runtimeState === 'empty' || summary.status === 'empty') return '等待数据'
  if (summary.status === 'partial') return '证据有限'
  if (summary.status === 'paused') return '已暂停'
  return '正常'
}

function toneForSummary(summary: MarketSummary): MarketTapeRow['tone'] {
  if (summary.executionFault || summary.runtimeState === 'needs_attention') return 'negative'
  if (summary.runtimeState === 'strategy_wait' || summary.status === 'partial') return 'warning'
  if (summary.status === 'empty' || summary.status === 'paused') return 'muted'
  return (summary.returnPct ?? 0) < 0 ? 'negative' : 'positive'
}

function statusCopy(status: DomainStatus, message?: string) {
  if (status === 'ready') return { state: '正常', tone: 'positive' as const }
  if (status === 'stale') return { state: '快照滞后', tone: 'warning' as const }
  if (status === 'loading') return { state: '载入中', tone: 'muted' as const }
  if (status === 'empty') return { state: '等待数据', tone: 'muted' as const }
  if (status === 'live-gated') return { state: '实盘隔离', tone: 'warning' as const }
  return { state: message ?? '读取异常', tone: 'negative' as const }
}

function signedPercent(value: number) { return `${value > 0 ? '+' : ''}${Math.abs(value) < 0.005 ? '0.00' : value.toFixed(2)}%` }
function formatPrice(value: number) { return new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: value < 10 ? 4 : 2 }).format(value) }
function formatOptionalPrice(value?: number) { return value === undefined ? '—' : formatPrice(value) }
function snapshotTime(value?: string | null) { if (!value) return '等待快照'; const date = new Date(value); return Number.isNaN(date.getTime()) ? '时间未知' : new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Shanghai' }).format(date) }
