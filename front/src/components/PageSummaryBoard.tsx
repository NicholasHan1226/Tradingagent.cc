import { marketLabels, statusLabels } from '../data/dashboard'
import { getClosedSignals, getSignalFunnel } from '../lib/dashboard'
import { DRAWDOWN_LIMIT_PCT } from '../lib/dashboardConstants'
import { formatCnyCompact, formatCurrency, formatSignedCnyCompact } from '../lib/format'
import { summarizeHoldingExposure } from '../lib/holdings'
import type { HoldingRow, Page, PerformancePoint, PortfolioSummary, SignalRow } from '../types/dashboard'

type Metric = {
  label: string
  value: string
  detail?: string
  tone?: 'cyan' | 'red' | 'amber'
}

export function PageSummaryBoard({
  holdings,
  page,
  performance,
  portfolio,
  signals,
}: {
  holdings: HoldingRow[]
  page: Page
  performance: PerformancePoint[]
  portfolio: PortfolioSummary | null
  signals: SignalRow[]
}) {
  const metrics = getPageMetrics(page, { holdings, performance, portfolio, signals })
  const summary = getPageSummary(page, metrics)

  return (
    <section className="page-summary-board" aria-label={`${page}摘要`}>
      <div className="page-summary-intro">
        <span>{page}摘要</span>
        <strong>{summary.title}</strong>
        <em>{summary.detail}</em>
      </div>
      <div className="page-summary-metrics">
        {metrics.map((metric) => (
          <div className={`page-summary-metric ${metric.tone ?? ''}`} key={`${metric.label}-${metric.value}`}>
            <span>{metric.label}</span>
            <strong>{metric.value}</strong>
            {metric.detail && <em>{metric.detail}</em>}
          </div>
        ))}
      </div>
    </section>
  )
}

function getPageSummary(page: Page, metrics: Metric[]) {
  const first = metrics[0]
  const second = metrics[1]

  if (page === '收益') return { title: `当前结果 ${first.value}`, detail: second ? `${second.label} ${second.value}` : '持续跟踪目标差和回撤' }
  if (page === '机会') return { title: `${first.value} 个机会可继续处理`, detail: '优先看等待确认、风险拦截和正向影响' }
  if (page === '持仓') return { title: `${first.value} 个持仓在模拟盘中`, detail: '先看贡献、集中度和风险偏高项' }
  if (page === '决策') return { title: `${first.value} 条机会进入决策管道`, detail: '看从发现到结果的转化和被保护数量' }
  if (page === '风险') return { title: `当前回撤 ${first.value}`, detail: '先确认距离限制线还有多少空间' }
  return { title: `${first.value} 条记录已关闭`, detail: '复盘已兑现、未兑现和主要来源' }
}

function getPageMetrics(
  page: Page,
  {
    holdings,
    performance,
    portfolio,
    signals,
  }: {
    holdings: HoldingRow[]
    performance: PerformancePoint[]
    portfolio: PortfolioSummary | null
    signals: SignalRow[]
  },
): Metric[] {
  const latest = performance[performance.length - 1]
  const currentReturn = portfolio?.returnPct ?? latest?.simulated ?? 0
  const target = portfolio?.targetPct ?? latest?.target ?? 0
  const gap = currentReturn - target
  const funnel = getSignalFunnel(signals)
  const closed = getClosedSignals(signals)
  const positiveImpact = signals.reduce((total, signal) => total + Math.max(0, readImpact(signal.impact)), 0)
  const topHolding = holdings[0]
  const highRiskCount = holdings.filter((holding) => holding.risk === '偏高').length
  const exposureSummary = summarizeHoldingExposure(holdings)
  const positiveHoldings = holdings.filter((holding) => !holding.pnl.startsWith('-')).length
  const drawdown = Math.abs(portfolio?.maxDrawdownPct ?? 0)
  const drawdownLimit = DRAWDOWN_LIMIT_PCT
  const blockedCount = signals.filter((signal) => signal.status === 'blocked').length
  const pendingCount = signals.filter((signal) => signal.status === 'pending').length
  const missedCount = signals.filter((signal) => signal.status === 'missed').length
  const executedCount = signals.filter((signal) => signal.status === 'executed').length
  const ashareAccount = portfolio?.ashareAccount
  const validSampleLabel = ashareAccount
    ? ashareAccount.totalSampleCount > 0
      ? `${ashareAccount.strategySampleValidCount}/${ashareAccount.totalSampleCount}`
      : '等待样本'
    : ''
  const strategyMetricValue = ashareAccount?.strategySampleValidCount === 0
    ? '暂无有效'
    : ashareAccount?.strategyTotalPnl === undefined
      ? validSampleLabel
      : formatCnyCompact(ashareAccount.strategyTotalPnl)

  if (page === '收益') {
    return [
      { label: '当前收益', value: portfolio ? (ashareAccount ? formatSignedCnyCompact(portfolio.pnlAmount) : formatCurrency(portfolio.pnlAmount)) : formatPercent(currentReturn), detail: formatPercent(currentReturn), tone: currentReturn >= 0 ? 'cyan' : 'red' },
      { label: '目标差', value: formatPercent(gap), detail: `目标 ${formatPercent(target)}`, tone: gap >= 0 ? 'cyan' : 'amber' },
      ashareAccount
        ? { label: '可复盘收益', value: strategyMetricValue, detail: `有效样本 ${validSampleLabel}` }
        : { label: '成交次数', value: String(portfolio?.tradeCount ?? executedCount), detail: `${portfolio?.pointCount ?? performance.length} 个收益点` },
      { label: '最大回撤', value: `-${drawdown.toFixed(2)}%`, detail: `限制 ${drawdownLimit}%`, tone: drawdown > drawdownLimit * 0.8 ? 'red' : 'cyan' },
    ]
  }

  if (page === '机会') {
    return [
      { label: '可处理', value: String(funnel.tradeSignals.length), detail: `${signals.length} 条进入` },
      { label: '等待确认', value: String(pendingCount), detail: '需要继续观察', tone: pendingCount ? 'amber' : undefined },
      { label: '风险拦截', value: String(blockedCount), detail: blockedCount ? '避免追高或过度波动' : '暂无拦截', tone: blockedCount ? 'red' : 'cyan' },
      { label: '预期影响', value: `+${positiveImpact.toFixed(1)}`, detail: '正向机会合计', tone: positiveImpact ? 'cyan' : undefined },
    ]
  }

  if (page === '持仓') {
    return [
      { label: '持仓数量', value: String(holdings.length), detail: `${positiveHoldings} 个正贡献` },
      ashareAccount
        ? { label: '账户总资产', value: formatCnyCompact(ashareAccount.accountEquity), detail: `现金 ${formatCnyCompact(ashareAccount.cashAvailable)}` }
        : { label: exposureSummary.label, value: exposureSummary.value, detail: exposureSummary.detail },
      { label: '最大贡献', value: topHolding?.symbol ?? '等待持仓', detail: topHolding?.pnl ?? '暂无收益', tone: topHolding?.pnl.startsWith('-') ? 'red' : 'cyan' },
      { label: '风险偏高', value: String(highRiskCount), detail: highRiskCount ? '需要优先查看' : ashareAccount ? `有效样本 ${validSampleLabel}` : '暂无偏高', tone: highRiskCount ? 'red' : 'cyan' },
    ]
  }

  if (page === '决策') {
    return [
      { label: '进入漏斗', value: String(signals.length), detail: '全市场机会' },
      { label: '形成结果', value: String(funnel.tradeSignals.length), detail: `${conversion(funnel.tradeSignals.length, signals.length)} 留下`, tone: 'cyan' },
      { label: '已兑现', value: String(executedCount), detail: `${conversion(executedCount, Math.max(1, funnel.tradeSignals.length))} 转化`, tone: 'cyan' },
      { label: '被保护', value: String(blockedCount), detail: blockedCount ? '风险先挡住' : '暂无拦截', tone: blockedCount ? 'red' : undefined },
    ]
  }

  if (page === '风险') {
    return [
      { label: '当前回撤', value: `-${drawdown.toFixed(2)}%`, detail: `距离限制 ${(drawdownLimit - drawdown).toFixed(2)}%`, tone: drawdown > drawdownLimit * 0.8 ? 'red' : 'cyan' },
      { label: '限制线', value: `${drawdownLimit}%`, detail: '模拟盘边界' },
      { label: '风险拦截', value: String(blockedCount), detail: blockedCount ? '已挡住' : '暂无拦截', tone: blockedCount ? 'red' : 'cyan' },
      { label: '需要复盘', value: String(missedCount), detail: missedCount ? '窗口或条件问题' : '暂无错过', tone: missedCount ? 'amber' : undefined },
    ]
  }

  return [
    { label: '已关闭', value: String(closed.length), detail: `${signals.length} 条总记录` },
    { label: '已兑现', value: String(executedCount), detail: statusLabels.executed, tone: 'cyan' },
    { label: '未兑现', value: String(missedCount), detail: missedCount ? '需要复盘' : '暂无', tone: missedCount ? 'amber' : undefined },
    { label: '主要市场', value: topMarket(signals), detail: '关闭记录来源' },
  ]
}

function conversion(value: number, total: number) {
  return `${Math.round((value / Math.max(1, total)) * 100)}%`
}

function formatPercent(value: number) {
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`
}

function readImpact(value: string) {
  const parsed = Number(value.replace('+', '').replace('%', '').trim())
  return Number.isFinite(parsed) ? parsed : 0
}

function topMarket(signals: SignalRow[]) {
  const counts = signals.reduce<Record<string, number>>((acc, signal) => {
    acc[signal.market] = (acc[signal.market] ?? 0) + 1
    return acc
  }, {})
  const [market] = Object.entries(counts).sort((a, b) => b[1] - a[1])[0] ?? []
  return market ? marketLabels[market as keyof typeof marketLabels] : '暂无记录'
}
