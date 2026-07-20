import { marketLabels, statusLabels } from '../data/dashboard'
import type { HoldingRow, SignalRow } from '../types/dashboard'
import type { DataDomain, DomainStatus } from '../types/status'
import { parseHoldingExposure } from './holdings'
import { translateTerminalValue } from './runtimeHeartbeat'

export type ProcessBookRow = {
  symbol: string
  name: string
  market: string
  process: string
  stage: string
  state: string
  evidence: string
  latency: string
  result: string
  updatedAt: string
  reason: string
}

export type PortfolioLedgerRow = {
  symbol: string
  assetName: string
  market: string
  role: string
  marketValue: string
  weight: string
  pnl: string
  contribution: string
  risk: HoldingRow['risk']
  quantity: string
  averagePrice: string
  markPrice: string
  costBasis: string
  dayPnl: string
  source: string
  updatedAt: string
}

export type RiskLedgerRow = {
  symbol: string
  market: string
  stage: string
  gate: string
  evidence: string
  reason: string
  updatedAt: string
}

export function createProcessBookRows(running: SignalRow[], completed: SignalRow[]) {
  const source = running.length ? running : completed
  const mode = running.length ? 'running' : completed.length ? 'completed' : 'empty'

  return {
    mode,
    title: mode === 'running' ? '运行中' : mode === 'completed' ? '最近完成' : '运行空闲',
    rows: source.map(toProcessRow),
  } as const
}

export function createPortfolioLedgerRows(holdings: HoldingRow[]): PortfolioLedgerRow[] {
  const currencies = holdings.map(holdingCurrency)
  const scopes = holdings.map((holding, index) => holding.accountScope?.trim()
    ? `${holding.market}:${holding.accountScope.trim()}:${currencies[index]}`
    : undefined)
  const values = holdings.map((holding) => parseHoldingExposure(holding.weight).value)
  const totalsByScope = values.reduce((totals, value, index) => {
    const scope = scopes[index]
    if (!scope) return totals
    totals.set(scope, (totals.get(scope) ?? 0) + value)
    return totals
  }, new Map<string, number>())
  const pnlValues = holdings.map((holding) => parseSignedValue(holding.pnl))
  const totalAbsPnlByScope = pnlValues.reduce((totals, value, index) => {
    const scope = scopes[index]
    if (!scope) return totals
    totals.set(scope, (totals.get(scope) ?? 0) + Math.abs(value))
    return totals
  }, new Map<string, number>())

  return holdings.map((holding, index) => {
    const currency = currencies[index]
    const scope = scopes[index]
    const total = scope ? totalsByScope.get(scope) ?? 0 : 0
    const totalAbsPnl = scope ? totalAbsPnlByScope.get(scope) ?? 0 : 0
    return {
      symbol: holding.symbol,
      assetName: normalizeAssetName(holding),
      market: marketLabels[holding.market],
      role: holding.role,
      marketValue: holding.marketValue === undefined ? holding.weight : formatHoldingMoney(holding.marketValue, currency),
      weight: scope && total > 0 ? `${((values[index] / total) * 100).toFixed(1)}%` : '—',
      pnl: holding.pnl,
      contribution: scope && totalAbsPnl > 0 ? `${pnlValues[index] >= 0 ? '+' : '-'}${((Math.abs(pnlValues[index]) / totalAbsPnl) * 100).toFixed(1)}%` : '—',
      risk: holding.risk,
      quantity: formatQuantity(holding.quantity),
      averagePrice: formatHoldingMoney(holding.averagePrice, currency, 2),
      markPrice: formatHoldingMoney(holding.markPrice, currency, 2),
      costBasis: formatHoldingMoney(holding.costBasis, currency),
      dayPnl: holding.dayPnl === undefined ? '—' : `${holding.dayPnl > 0 ? '+' : ''}${formatHoldingMoney(holding.dayPnl, currency)}`,
      source: holdingSourceLabel(holding.source),
      updatedAt: holding.updatedAt ?? '—',
    }
  })
}

export function summarizePortfolioCurrency(holdings: HoldingRow[]): { currency: 'CNY' | 'USD' | 'USDT' | 'percent' | 'mixed' | 'empty'; label: string } {
  if (!holdings.length) return { currency: 'empty', label: '—' }
  if (new Set(holdings.map((holding) => holding.market)).size > 1) {
    return { currency: 'mixed', label: '多账户不可汇总' }
  }
  const accountScopes = holdings.map((holding) => holding.accountScope?.trim()).filter(Boolean) as string[]
  if (accountScopes.length !== holdings.length) return { currency: 'mixed', label: '账户范围不可用' }
  if (new Set(accountScopes).size > 1) return { currency: 'mixed', label: '多账户不可汇总' }
  const kinds = new Set(holdings.map((holding) => holding.currency ?? currencyKind(holding.weight)))
  if (kinds.size !== 1) return { currency: 'mixed', label: '多币种' }
  const [currency] = [...kinds]
  const total = holdings.reduce((sum, holding) => sum + (holding.marketValue ?? parseHoldingExposure(holding.weight).value), 0)
  if (currency === 'CNY') return { currency, label: `¥${Math.round(total).toLocaleString('en-US')}` }
  if (currency === 'USDT') return { currency, label: `${Math.round(total).toLocaleString('en-US')} USDT` }
  if (currency === 'percent') return { currency, label: `${total.toFixed(1)}%` }
  if (currency === 'USD') return { currency: 'mixed', label: '币种不可用' }
  return { currency: 'empty', label: '—' }
}

export function createRiskLedgerRows(signals: SignalRow[], domains: Partial<Record<DataDomain, DomainStatus>> = {}): RiskLedgerRow[] {
  const signalRows = signals
    .filter((signal) => signal.status === 'blocked' || signal.status === 'missed' || signal.status === 'cancelled')
    .map((signal) => ({
      symbol: signal.symbol,
      market: marketLabels[signal.market],
      stage: formatStage(signal),
      gate: signal.status === 'blocked' ? '安全拦截' : signal.status === 'missed' ? '自动复盘' : '自动终止',
      evidence: formatEvidence(signal.stageEvidence),
      reason: signal.reason,
      updatedAt: signal.age,
    }))
  const domainRows = (Object.entries(domains) as [DataDomain, DomainStatus][])
    .filter(([, status]) => status === 'stale' || status === 'error' || status === 'live-gated')
    .map(([domain, status]) => ({
      symbol: `DATA/${domain.toUpperCase()}`,
      market: '证据域',
      stage: domainLabel(domain),
      gate: status === 'stale' ? '快照滞后' : status === 'live-gated' ? '实盘隔离' : '读取异常',
      evidence: status === 'stale' ? '证据有限' : '证据不可用',
      reason: status === 'stale' ? '该证据域更新时间落后于当前快照' : status === 'live-gated' ? '实盘数据保持隔离' : '该证据域读取失败',
      updatedAt: '当前快照',
    }))
  return [...signalRows, ...domainRows]
}

function toProcessRow(signal: SignalRow): ProcessBookRow {
  return {
    symbol: signal.symbol,
    name: normalizeAssetName(signal),
    market: marketLabels[signal.market],
    process: translateTerminalValue(signal.strategyName ?? signal.method),
    stage: formatStage(signal),
    state: signal.queueBucket?.toLowerCase() === 'partial' ? '部分成交' : statusLabels[signal.status],
    evidence: formatEvidence(signal.stageEvidence),
    latency: signal.stageLatencyMinutes ? `${signal.stageLatencyMinutes}分钟` : '—',
    result: signal.status === 'executed' ? '结果已写回' : signal.status === 'pending' ? '运行中' : signal.status === 'blocked' ? '安全拦截' : signal.status === 'missed' ? '自动复盘' : '自动终止',
    updatedAt: signal.age,
    reason: signal.reason,
  }
}

function formatStage(signal: SignalRow) {
  if (signal.stage === '评分') return '研究'
  if (signal.stage === '待执行') return '模拟执行'
  if (signal.stage === '成交') return '结果写回'
  return signal.stage ?? (signal.status === 'executed' ? '结果写回' : '自动等待')
}

function formatEvidence(value?: SignalRow['stageEvidence']) {
  if (value === 'full') return '证据完整'
  if (value === 'partial') return '证据有限'
  if (value === 'replay') return '历史回放'
  return '—'
}

function normalizeAssetName(row: { symbol: string; name: string }) {
  return row.name.trim().toUpperCase() === row.symbol.trim().toUpperCase() ? '' : row.name
}

function currencyKind(value: string): 'CNY' | 'USD' | 'USDT' | 'percent' | 'empty' {
  if (/¥|￥|CNY|RMB/i.test(value)) return 'CNY'
  if (/USDT/i.test(value)) return 'USDT'
  if (/\$|USD|USDC/i.test(value)) return 'USD'
  if (value.includes('%')) return 'percent'
  return 'empty'
}

function parseSignedValue(value: string) {
  const numeric = Number(value.replace(/[^0-9.-]/g, '').replace(/,/g, ''))
  return Number.isFinite(numeric) ? numeric : 0
}

function formatQuantity(value?: number) {
  return value === undefined ? '—' : value.toLocaleString('en-US', { maximumFractionDigits: 4 })
}

function formatHoldingMoney(value?: number, currency: HoldingRow['currency'] = 'CNY', decimals = 0) {
  if (value === undefined) return '—'
  const sign = value < 0 ? '-' : ''
  const amount = Math.abs(value).toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals })
  if (currency === 'USDT') return `${sign}${amount} USDT`
  if (currency === 'CNY') return `${sign}¥${amount}`
  return '—'
}

function holdingCurrency(holding: HoldingRow): NonNullable<HoldingRow['currency']> {
  return holding.currency ?? (holding.market === 'Crypto' ? 'USDT' : 'CNY')
}

function holdingSourceLabel(source?: HoldingRow['source']) {
  if (source === 'sim_ledger') return '模拟账本'
  if (source === 'position_snapshot') return '持仓快照'
  if (source === 'legacy_position_ledger') return '旧持仓账本'
  return '—'
}

function domainLabel(domain: DataDomain) {
  return ({ performance: '收益证据', signals: '信号证据', holdings: '持仓证据', decisions: '复盘证据', risk: '风险证据' } as Record<DataDomain, string>)[domain]
}
