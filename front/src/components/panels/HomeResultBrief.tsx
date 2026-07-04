import { getActionableSignals, getClosedSignals } from '../../lib/dashboard'
import type { HoldingRow, Page, SignalRow } from '../../types/dashboard'
import { PanelTitle } from '../PanelTitle'
import { SummaryRow } from '../SummaryRow'

export function HomeResultBrief({
  hasHoldingData,
  hasPerformanceData,
  hasSignalData,
  holdings,
  setActivePage,
  signals,
}: {
  hasHoldingData: boolean
  hasPerformanceData: boolean
  hasSignalData: boolean
  holdings: HoldingRow[]
  setActivePage: (page: Page) => void
  signals: SignalRow[]
}) {
  const actionable = getActionableSignals(signals)
  const closed = getClosedSignals(signals)
  const lead = actionable[0]
  const review = closed.find((signal) => signal.status === 'missed') ?? closed[0]
  const protectedSignal = signals.find((signal) => signal.status === 'blocked')
  const pausedSignal = protectedSignal ?? actionable[1]
  const topHolding = holdings[0]

  return (
    <section className="panel rail-panel home-result-brief">
      <PanelTitle action="全部机会" kicker="今日重点" onAction={() => setActivePage('机会')} title="现在关注" />
      <div className="home-brief-section brief-result-section">
        <span className="section-label">结果</span>
        <div className="summary-list">
          <SummaryRow label="收益曲线" value={hasPerformanceData ? '已更新' : '等待写入'} tone={hasPerformanceData ? 'cyan' : undefined} />
          <SummaryRow label="机会通道" value={hasSignalData ? `${signals.length} 个` : '暂无新机会'} />
          <SummaryRow label="持仓记录" value={hasHoldingData ? topHolding?.symbol ?? '已更新' : '暂无持仓'} />
        </div>
      </div>
      <div className="home-brief-section brief-action-section">
        <span className="section-label">机会</span>
        <div className="decision-list compact">
          {hasSignalData && lead && (
            <button onClick={() => setActivePage('机会')} type="button">
              <span>优先跟进</span>
              <strong>{lead.symbol}</strong>
              <em>{lead.reason}</em>
            </button>
          )}
          {hasSignalData && pausedSignal && (
            <button onClick={() => setActivePage('风险')} type="button">
              <span>{protectedSignal ? '风险已挡住' : '暂缓观察'}</span>
              <strong>{pausedSignal.symbol}</strong>
              <em>{pausedSignal.reason}</em>
            </button>
          )}
          {hasSignalData && review && (
            <button onClick={() => setActivePage('复盘')} type="button">
              <span>机会复盘</span>
              <strong>{review.symbol}</strong>
              <em>{review.reason}</em>
            </button>
          )}
          {!hasSignalData && (
            <div className="empty-panel-copy compact-copy">
              <strong>等待机会进入</strong>
              <span>有机会进入后，首页只保留最需要处理的几项。</span>
            </div>
          )}
        </div>
      </div>
      <div className="home-brief-section brief-risk-section">
        <span className="section-label">边界</span>
        {hasPerformanceData ? (
          <div className="risk-cards compact">
            <button className="risk-card red" onClick={() => setActivePage('风险')} type="button">
              <span>最大回撤</span>
              <strong>-6.12%</strong>
              <em>接近 -7% 限制</em>
            </button>
            <button className="risk-card cyan" onClick={() => setActivePage('风险')} type="button">
              <span>风险保护</span>
              <strong>$1.24M</strong>
              <em>控制敞口</em>
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
        收益归因
      </button>
    </section>
  )
}
