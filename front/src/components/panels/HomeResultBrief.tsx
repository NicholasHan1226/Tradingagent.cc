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
  const returnValue = portfolio
    ? `${formatCurrency(portfolio.pnlAmount)} / ${portfolio.returnPct >= 0 ? '+' : ''}${portfolio.returnPct.toFixed(2)}%`
    : hasPerformanceData
      ? '有收益曲线'
      : '暂无曲线'

  return (
    <section className="panel rail-panel home-result-brief">
      <PanelTitle action="看机会" kicker="首页摘要" onAction={() => setActivePage('机会')} title="现在该看什么" />
      <div className="home-brief-section brief-result-section">
        <span className="section-label">核心结果</span>
        <div className="summary-list">
          <SummaryRow label="收益" value={returnValue} tone={hasPerformanceData ? 'cyan' : undefined} />
          <SummaryRow label="机会" value={hasSignalData ? `${signals.length} 条` : '暂无机会'} />
          <SummaryRow label="持仓" value={hasHoldingData ? leadingHolding?.symbol ?? '有记录' : '暂无持仓'} />
          {portfolio?.pnlSource && <SummaryRow label="口径" value={formatPnlSource(portfolio.pnlSource)} />}
        </div>
      </div>
      <div className="home-brief-section brief-action-section">
        <span className="section-label">优先查看</span>
        <div className="decision-list compact">
          {hasSignalData && leadSignal && (
            <button onClick={() => setActivePage('机会')} type="button">
              <span>最靠前机会</span>
              <strong>{leadSignal.symbol}</strong>
              <em>{leadSignal.reason}</em>
            </button>
          )}
          {hasSignalData && blockedSignal && (
            <button onClick={() => setActivePage('风险')} type="button">
              <span>风险挡住</span>
              <strong>{blockedSignal.symbol}</strong>
              <em>{blockedSignal.reason}</em>
            </button>
          )}
          {hasSignalData && reviewSignal && (
            <button onClick={() => setActivePage('复盘')} type="button">
              <span>需要复盘</span>
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
      <div className="home-brief-section brief-risk-section">
        <span className="section-label">风险边界</span>
        {hasPerformanceData ? (
          <div className="risk-cards compact">
            <button className="risk-card red" onClick={() => setActivePage('风险')} type="button">
              <span>最大回撤</span>
              <strong>-{Math.abs(drawdown).toFixed(2)}%</strong>
              <em>{drawdownCaption}</em>
            </button>
            <button className="risk-card cyan" onClick={() => setActivePage('风险')} type="button">
              <span>限制线</span>
              <strong>{drawdownLimit}%</strong>
              <em>仍在范围内</em>
            </button>
          </div>
        ) : (
          <div className="empty-panel-copy compact-copy">
            <strong>等待风险记录</strong>
            <span>有回撤和风控结果后，会在这里显示边界距离。</span>
          </div>
        )}
      </div>
      <button className="primary-action" onClick={() => setActivePage('收益')} type="button">
        查看收益原因
      </button>
    </section>
  )
}

function formatPnlSource(value: string) {
  if (value === 'sim_ledger_mark_to_market') return '模拟账本盯市'
  if (value === 'mixed') return '混合口径'
  return value
}
