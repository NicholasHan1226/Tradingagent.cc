import { marketLabels, statusLabels } from '../data/dashboard'
import { getClosedSignals } from '../lib/dashboard'
import { DRAWDOWN_LIMIT_PCT } from '../lib/dashboardConstants'
import { formatCnyCompact, formatCurrency, formatSignedCnyCompact } from '../lib/format'
import { summarizeHoldingExposure } from '../lib/holdings'
import type { HoldingRow, Market, MarketSummary, Page, PerformancePoint, PortfolioSummary, SignalRow } from '../types/dashboard'

type Metric = {
  label: string
  value: string
  detail?: string
  tone?: 'cyan' | 'red' | 'amber'
}

export function PageSummaryBoard({
  activeMarket = 'All Markets',
  holdings,
  marketSummary,
  page,
  performance,
  portfolio,
  signals,
}: {
  activeMarket?: Market
  holdings: HoldingRow[]
  marketSummary?: MarketSummary
  page: Page
  performance: PerformancePoint[]
  portfolio: PortfolioSummary | null
  signals: SignalRow[]
}) {
  const metrics = getPageMetrics(page, { activeMarket, holdings, marketSummary, performance, portfolio, signals })
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
  if (page === '过程') return { title: `${first.value} 条自动流程正在运行`, detail: '查看研究、风控、模拟执行和结果写回' }
  if (page === '持仓') return { title: `${first.value} 个持仓在模拟盘中`, detail: '展示贡献、集中度和风险状态' }
  if (page === '风险') return { title: `当前回撤 ${first.value}`, detail: '持续计算与限制线的剩余距离' }
  return { title: `${first.value} 条记录已关闭`, detail: '复盘已兑现、未兑现和主要来源' }
}

function getPageMetrics(
  page: Page,
  {
    holdings,
    marketSummary,
    performance,
    portfolio,
    signals,
    activeMarket,
  }: {
    activeMarket: Market
    holdings: HoldingRow[]
    marketSummary?: MarketSummary
    performance: PerformancePoint[]
    portfolio: PortfolioSummary | null
    signals: SignalRow[]
  },
): Metric[] {
  const latest = performance[performance.length - 1]
  const currentReturn = marketSummary?.returnPct ?? portfolio?.returnPct ?? latest?.simulated ?? 0
  const target = portfolio?.targetPct ?? latest?.target ?? 0
  const gap = currentReturn - target
  const closed = getClosedSignals(signals)
  const positiveImpact = signals.reduce((total, signal) => total + Math.max(0, readImpact(signal.impact)), 0)
  const topHolding = holdings[0]
  const highRiskCount = holdings.filter((holding) => holding.risk === '偏高').length
  const exposureSummary = summarizeHoldingExposure(holdings)
  const positiveHoldings = holdings.filter((holding) => !holding.pnl.startsWith('-')).length
  const drawdown = Math.abs(marketSummary?.maxDrawdownPct ?? portfolio?.maxDrawdownPct ?? 0)
  const drawdownLimit = DRAWDOWN_LIMIT_PCT
  const blockedCount = signals.filter((signal) => signal.status === 'blocked').length
  const pendingCount = signals.filter((signal) => signal.status === 'pending').length
  const missedCount = signals.filter((signal) => signal.status === 'missed').length
  const executedCount = signals.filter((signal) => signal.status === 'executed').length
  const ashareAccount = activeMarket === 'All Markets' || activeMarket === 'A-share' ? portfolio?.ashareAccount : undefined
  const isCnyPortfolio = portfolio?.pnlCurrency === 'CNY'
  const validSampleLabel = ashareAccount
    ? ashareAccount.totalSampleCount > 0
      ? `${ashareAccount.strategySampleValidCount}/${ashareAccount.totalSampleCount}`
      : '等待复盘'
    : ''
  const strategyMetricValue = ashareAccount?.strategySampleValidCount === 0
    ? '暂无复盘'
    : ashareAccount?.strategyTotalPnl === undefined
      ? validSampleLabel
      : formatCnyCompact(ashareAccount.strategyTotalPnl)

  if (page === '收益') {
    return [
      { label: '当前收益', value: formatReturnMetric({ currentReturn, isCnyPortfolio: isCnyPortfolio || activeMarket === 'A-share', marketSummary, portfolio }), detail: formatPercent(currentReturn), tone: currentReturn >= 0 ? 'cyan' : 'red' },
      { label: '目标差', value: formatPercent(gap), detail: `目标 ${formatPercent(target)}`, tone: gap >= 0 ? 'cyan' : 'amber' },
      ashareAccount
        ? { label: '复盘收益', value: strategyMetricValue, detail: `可复盘 ${validSampleLabel}` }
        : { label: '成交次数', value: String(marketSummary?.tradeCount ?? portfolio?.tradeCount ?? executedCount), detail: `${portfolio?.pointCount ?? performance.length} 个收益点` },
      { label: '最大回撤', value: formatDrawdown(drawdown), detail: `限制 ${drawdownLimit}%`, tone: drawdown > drawdownLimit * 0.8 ? 'red' : 'cyan' },
    ]
  }

  if (page === '过程') {
    return [
      { label: '运行中', value: String(pendingCount), detail: `${signals.length} 条过程记录`, tone: pendingCount ? 'cyan' : undefined },
      { label: '结果写回', value: String(executedCount), detail: `${conversion(executedCount, signals.length)} 完成`, tone: 'cyan' },
      { label: '安全拦截', value: String(blockedCount), detail: blockedCount ? '风控自动终止' : '暂无拦截', tone: blockedCount ? 'red' : 'cyan' },
      { label: '结果影响', value: `+${positiveImpact.toFixed(1)}`, detail: '已记录影响合计', tone: positiveImpact ? 'cyan' : undefined },
    ]
  }

  if (page === '持仓') {
    return [
      { label: '持仓数量', value: String(holdings.length), detail: `${positiveHoldings} 个正贡献` },
      ashareAccount
        ? { label: '账户总资产', value: formatCnyCompact(ashareAccount.accountEquity), detail: `现金 ${formatCnyCompact(ashareAccount.cashAvailable)}` }
        : { label: exposureSummary.label, value: exposureSummary.value, detail: exposureSummary.detail },
      { label: '最大贡献', value: topHolding?.symbol ?? '等待持仓', detail: topHolding?.pnl ?? '暂无收益', tone: topHolding?.pnl.startsWith('-') ? 'red' : 'cyan' },
      { label: '风险偏高', value: String(highRiskCount), detail: highRiskCount ? '已进入风险跟踪' : ashareAccount ? `可复盘 ${validSampleLabel}` : '暂无偏高', tone: highRiskCount ? 'red' : 'cyan' },
    ]
  }

  if (page === '风险') {
    return [
      { label: '当前回撤', value: formatDrawdown(drawdown), detail: `距离限制 ${(drawdownLimit - drawdown).toFixed(2)}%`, tone: drawdown > drawdownLimit * 0.8 ? 'red' : 'cyan' },
      { label: '限制线', value: `${drawdownLimit}%`, detail: '模拟盘边界' },
      { label: '风险拦截', value: String(blockedCount), detail: blockedCount ? '已挡住' : '暂无拦截', tone: blockedCount ? 'red' : 'cyan' },
      { label: '自动复盘', value: String(missedCount), detail: missedCount ? '窗口或条件异常' : '暂无异常', tone: missedCount ? 'amber' : undefined },
    ]
  }

  return [
    { label: '已关闭', value: String(closed.length), detail: `${signals.length} 条总记录` },
    { label: '已兑现', value: String(executedCount), detail: statusLabels.executed, tone: 'cyan' },
    { label: '未兑现', value: String(missedCount), detail: missedCount ? '自动复盘中' : '暂无', tone: missedCount ? 'amber' : undefined },
    { label: '主要市场', value: topMarket(signals), detail: '关闭记录来源' },
  ]
}

function formatDrawdown(value: number) {
  const cleanValue = Math.abs(value) < 0.005 ? 0 : Math.abs(value)
  return cleanValue === 0 ? '0.00%' : `-${cleanValue.toFixed(2)}%`
}

function conversion(value: number, total: number) {
  return `${Math.round((value / Math.max(1, total)) * 100)}%`
}

function formatReturnMetric({
  currentReturn,
  isCnyPortfolio,
  marketSummary,
  portfolio,
}: {
  currentReturn: number
  isCnyPortfolio: boolean
  marketSummary?: MarketSummary
  portfolio: PortfolioSummary | null
}) {
  const pnlAmount = marketSummary?.pnlAmount ?? portfolio?.pnlAmount
  if (pnlAmount === undefined) return formatPercent(currentReturn)
  return isCnyPortfolio ? formatSignedCnyCompact(pnlAmount) : formatCurrency(pnlAmount)
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
