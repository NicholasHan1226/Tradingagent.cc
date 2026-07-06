import { getHomeOutcome } from '../../lib/dashboard'
import { DRAWDOWN_LIMIT_PCT } from '../../lib/dashboardConstants'
import { formatCurrency } from '../../lib/format'
import type { HoldingRow, Page, PortfolioSummary, SignalRow } from '../../types/dashboard'
import { PanelTitle } from '../PanelTitle'
import { SummaryRow } from '../SummaryRow'

export function HomeResultBrief({
  hasHoldingData,
  hasPerformanceData,
  hasSignalData,
  holdings,
  portfolio,
  setActivePage,
  signals,
}: {
  hasHoldingData: boolean
  hasPerformanceData: boolean
  hasSignalData: boolean
  holdings: HoldingRow[]
  portfolio: PortfolioSummary | null
  setActivePage: (page: Page) => void
  signals: SignalRow[]
}) {
  const { blockedSignal, leadSignal, leadingHolding, reviewSignal } = getHomeOutcome(signals, holdings)
  const drawdown = portfolio?.maxDrawdownPct ?? 0
  const drawdownLimit = DRAWDOWN_LIMIT_PCT
  const drawdownDistance = Math.max(0, drawdownLimit - Math.abs(drawdown))
  const drawdownCaption = drawdownDistance > 1 ? `距离 ${drawdownLimit}% 限制 ${drawdownDistance.toFixed(2)}%` : `接近 ${drawdownLimit}% 限制`
  const drawdownValue = hasPerformanceData ? `${Math.abs(drawdown).toFixed(2)}%` : '等待记录'
  const returnValue = portfolio
    ? `${formatCurrency(portfolio.pnlAmount)} / ${portfolio.returnPct >= 0 ? '+' : ''}${portfolio.returnPct.toFixed(2)}%`
    : hasPerformanceData
      ? '有收益曲线'
      : '等待收益'

  return (
    <section className="panel rail-panel home-result-brief">
      <PanelTitle action="看机会" kicker="首页摘要" onAction={() => setActivePage('机会')} title="当前结果" />
      <div className="home-brief-section brief-result-section">
        <span className="section-label">一眼判断</span>
        <div className="summary-list">
          <SummaryRow label="收益" value={returnValue} tone={hasPerformanceData ? 'cyan' : undefined} />
          <SummaryRow label="最大回撤" value={drawdownValue} tone={hasPerformanceData ? 'red' : undefined} />
          <SummaryRow label="风险距离" value={hasPerformanceData ? drawdownCaption : '等待记录'} />
          <SummaryRow label="机会" value={hasSignalData ? `${signals.length} 条` : '暂无机会'} />
          <SummaryRow label="持仓" value={hasHoldingData ? leadingHolding?.symbol ?? '有记录' : '暂无持仓'} />
          {portfolio?.pnlSource && <SummaryRow label="收益来源" value={formatPnlSource(portfolio.pnlSource)} />}
        </div>
      </div>
      <div className="home-brief-section brief-action-section">
        <span className="section-label">下一步</span>
        <div className="decision-list compact">
          {hasSignalData && leadSignal && (
            <button onClick={() => setActivePage('机会')} type="button">
              <span>优先跟进</span>
              <strong>{leadSignal.symbol}</strong>
              <em>{leadSignal.reason}</em>
            </button>
          )}
          {hasSignalData && blockedSignal && (
            <button onClick={() => setActivePage('风险')} type="button">
              <span>先不要追</span>
              <strong>{blockedSignal.symbol}</strong>
              <em>{blockedSignal.reason}</em>
            </button>
          )}
          {hasSignalData && reviewSignal && (
            <button onClick={() => setActivePage('复盘')} type="button">
              <span>回头检查</span>
              <strong>{reviewSignal.symbol}</strong>
              <em>{reviewSignal.reason}</em>
            </button>
          )}
          {!hasSignalData && (
            <div className="empty-panel-copy compact-copy">
              <strong>暂无机会结果</strong>
              <span>新机会进入后，首页只保留最需要看的结果。</span>
            </div>
          )}
        </div>
      </div>
      <button className="primary-action" onClick={() => setActivePage('收益')} type="button">
        查看收益详情
      </button>
    </section>
  )
}

function formatPnlSource(value: string) {
  if (value === 'sim_ledger_mark_to_market') return '当前持仓估算'
  if (value === 'mixed') return '多来源汇总'
  return value
}
