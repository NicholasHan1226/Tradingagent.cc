import { getActionableSignals, getClosedSignals } from '../../lib/dashboard'
import type { Page, SignalRow } from '../../types/dashboard'
import { PanelTitle } from '../PanelTitle'
import { SummaryRow } from '../SummaryRow'

export function HomeResultBrief({ setActivePage, signals }: { setActivePage: (page: Page) => void; signals: SignalRow[] }) {
  const actionable = getActionableSignals(signals)
  const closed = getClosedSignals(signals)
  const lead = actionable[0]
  const review = closed.find((signal) => signal.status === 'missed') ?? closed[0]
  const protectedSignal = signals.find((signal) => signal.status === 'blocked') ?? actionable[1]

  return (
    <section className="panel rail-panel home-result-brief">
      <PanelTitle action="全部机会" kicker="今日重点" onAction={() => setActivePage('机会')} title="现在关注" />
      <div className="home-brief-section brief-result-section">
        <span className="section-label">结果</span>
        <div className="summary-list">
          <SummaryRow label="主要来源" value="A股、美股趋势" tone="cyan" />
          <SummaryRow label="需要回看" value={review?.symbol ?? '暂无'} tone={review?.status === 'missed' ? 'red' : undefined} />
          <SummaryRow label="已避开风险" value={protectedSignal?.symbol ?? '暂无'} />
        </div>
      </div>
      <div className="home-brief-section brief-action-section">
        <span className="section-label">机会</span>
        <div className="decision-list compact">
          {lead && (
            <button onClick={() => setActivePage('机会')} type="button">
              <span>优先跟进</span>
              <strong>{lead.symbol}</strong>
              <em>{lead.reason}</em>
            </button>
          )}
          {protectedSignal && (
            <button onClick={() => setActivePage('风险')} type="button">
              <span>暂缓跟进</span>
              <strong>{protectedSignal.symbol}</strong>
              <em>{protectedSignal.reason}</em>
            </button>
          )}
          {review && (
            <button onClick={() => setActivePage('复盘')} type="button">
              <span>复盘信号</span>
              <strong>{review.symbol}</strong>
              <em>{review.reason}</em>
            </button>
          )}
        </div>
      </div>
      <div className="home-brief-section brief-risk-section">
        <span className="section-label">边界</span>
        <div className="risk-cards compact">
          <button className="risk-card red" onClick={() => setActivePage('风险')} type="button">
            <span>最大回撤</span>
            <strong>-6.12%</strong>
            <em>接近 -7% 限制</em>
          </button>
          <button className="risk-card cyan" onClick={() => setActivePage('风险')} type="button">
            <span>保护收益</span>
            <strong>$1.24M</strong>
            <em>控制敞口</em>
          </button>
        </div>
      </div>
      <button className="primary-action" onClick={() => setActivePage('收益')} type="button">
        收益归因
      </button>
    </section>
  )
}
