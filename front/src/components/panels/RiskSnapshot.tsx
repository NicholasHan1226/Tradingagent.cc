import { DRAWDOWN_LIMIT_PCT } from '../../lib/dashboardConstants'
import type { Page, PortfolioSummary, SignalRow } from '../../types/dashboard'
import { PanelTitle } from '../PanelTitle'

export function RiskSnapshot({
  portfolio,
  setActivePage,
  signals,
}: {
  portfolio: PortfolioSummary | null
  setActivePage: (page: Page) => void
  signals: SignalRow[]
}) {
  const drawdown = Math.abs(portfolio?.maxDrawdownPct ?? 0)
  const drawdownLimit = DRAWDOWN_LIMIT_PCT
  const blockedCount = signals.filter((signal) => signal.status === 'blocked').length
  const missedCount = signals.filter((signal) => signal.status === 'missed').length
  const distance = Math.max(0, drawdownLimit - drawdown)

  return (
    <section className="panel rail-panel">
      <PanelTitle action="看风险" kicker="风险" onAction={() => setActivePage('风险')} title="边界是否安全" />
      <div className="risk-cards">
        <button className="risk-card red" onClick={() => setActivePage('风险')} type="button">
          <span>最大回撤</span>
          <strong>-{drawdown.toFixed(2)}%</strong>
          <em>{distance > 1 ? `距离 ${drawdownLimit}% 限制 ${distance.toFixed(2)}%` : `接近 ${drawdownLimit}% 限制`}</em>
        </button>
        <button className="risk-card cyan" onClick={() => setActivePage('风险')} type="button">
          <span>已挡住</span>
          <strong>{blockedCount}</strong>
          <em>{missedCount ? `${missedCount} 条需要复盘` : '暂无明显错过'}</em>
        </button>
      </div>
    </section>
  )
}
