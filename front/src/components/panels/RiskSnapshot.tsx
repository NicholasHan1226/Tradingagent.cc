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
      <PanelTitle action="风险详情" kicker="风险" onAction={() => setActivePage('风险')} title="风险边界状态" />
      <div className="risk-cards">
        <button className="risk-card red" onClick={() => setActivePage('风险')} type="button">
          <span>最大回撤</span>
          <strong>{formatDrawdown(drawdown)}</strong>
          <em>{distance > 1 ? `距离 ${drawdownLimit}% 限制 ${distance.toFixed(2)}%` : `接近 ${drawdownLimit}% 限制`}</em>
        </button>
        <button className="risk-card cyan" onClick={() => setActivePage('风险')} type="button">
          <span>已挡住</span>
          <strong>{blockedCount}</strong>
          <em>{missedCount ? `${missedCount} 条已进入自动复盘` : '暂无明显错过'}</em>
        </button>
      </div>
    </section>
  )
}

function formatDrawdown(value: number) {
  const cleanValue = Math.abs(value) < 0.005 ? 0 : Math.abs(value)
  return cleanValue === 0 ? '0.00%' : `-${cleanValue.toFixed(2)}%`
}
