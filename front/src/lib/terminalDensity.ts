import { statusLabels } from '../data/dashboard'
import type { HoldingRow, PerformancePoint, PortfolioSummary, SignalRow } from '../types/dashboard'
import { translateTerminalValue } from './runtimeHeartbeat'

export type TerminalDensity = 'active' | 'quiet' | 'empty'
export type EvidenceEmptyModel = { title: string; detail: string; rows: Array<[string, string]> }

export function getPerformanceDensity(data: PerformancePoint[], portfolio: PortfolioSummary | null): TerminalDensity {
  if (!data.length && !portfolio) return 'empty'
  const values = data.map((point) => point.simulated).filter(Number.isFinite)
  const range = values.length ? Math.max(...values) - Math.min(...values) : 0
  const hasResult = Math.abs(portfolio?.pnlAmount ?? 0) > 0.005 || Math.abs(portfolio?.returnPct ?? 0) > 0.005
  return range > 0.005 || hasResult ? 'active' : 'quiet'
}

export function getHoldingsEmptyEvidence({
  generatedAt,
  holdings,
  portfolio,
  signals,
}: {
  generatedAt: string | null
  holdings: HoldingRow[]
  portfolio: PortfolioSummary | null
  signals: SignalRow[]
}): EvidenceEmptyModel {
  const latestClosed = signals.find((signal) => signal.status !== 'pending')
  const cash = portfolio?.ashareAccount?.cashAvailable
  return {
    title: '当前没有模拟持仓',
    detail: '资金保持未部署；等待通过证据与风险门禁的新机会。',
    rows: [
      ['当前敞口', `${holdings.length} 项`],
      ['可用资金', cash === undefined ? '—' : `¥${Math.round(cash).toLocaleString('en-US')}`],
      ['最近关闭', latestClosed ? `${latestClosed.symbol} · ${closedResult(latestClosed)}` : '尚无关闭结果'],
      ['数据时间', formatTimestamp(generatedAt)],
    ],
  }
}

function closedResult(signal: SignalRow) {
  if (signal.status === 'executed') return '结果已写回'
  if (signal.status === 'blocked') return '安全拦截'
  if (signal.status === 'missed') return '自动复盘'
  if (signal.status === 'cancelled') return '自动终止'
  return translateTerminalValue(statusLabels[signal.status])
}

function formatTimestamp(value: string | null) {
  if (!value) return '等待快照'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '时间未知'
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
    hour12: false, timeZone: 'Asia/Shanghai',
  }).format(date)
}
