import { getHomeOutcome } from '../../lib/dashboard'
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
  const drawdownLimit = 7
  const drawdownDistance = Math.max(0, drawdownLimit - Math.abs(drawdown))
  const drawdownCaption = drawdownDistance > 1 ? `距离 ${drawdownLimit}% 限制 ${drawdownDistance.toFixed(2)}%` : `接近 ${drawdownLimit}% 限制`

  return (
    <section className="panel rail-panel home-result-brief">
      <PanelTitle action="全部机会" kicker="结果摘要" onAction={() => setActivePage('机会')} title="当前结果" />
      <div className="home-brief-section brief-result-section">
        <span className="section-label">账户层</span>
        <div className="summary-list">
          <SummaryRow label="动态收益" value={hasPerformanceData ? '有结果' : '暂无曲线'} tone={hasPerformanceData ? 'cyan' : undefined} />
          <SummaryRow label="交易漏斗" value={hasSignalData ? `${signals.length} 个机会` : '暂无机会'} />
          <SummaryRow label="持仓贡献" value={hasHoldingData ? leadingHolding?.symbol ?? '有记录' : '暂无持仓'} />
        </div>
      </div>
      <div className="home-brief-section brief-action-section">
        <span className="section-label">推进结果</span>
        <div className="decision-list compact">
          {hasSignalData && leadSignal && (
            <button onClick={() => setActivePage('机会')} type="button">
              <span>正在推进</span>
              <strong>{leadSignal.symbol}</strong>
              <em>{leadSignal.reason}</em>
            </button>
          )}
          {hasSignalData && blockedSignal && (
            <button onClick={() => setActivePage('风险')} type="button">
              <span>已被边界挡住</span>
              <strong>{blockedSignal.symbol}</strong>
              <em>{blockedSignal.reason}</em>
            </button>
          )}
          {hasSignalData && reviewSignal && (
            <button onClick={() => setActivePage('复盘')} type="button">
              <span>进入复盘</span>
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
        <span className="section-label">风险距离</span>
        {hasPerformanceData ? (
          <div className="risk-cards compact">
            <button className="risk-card red" onClick={() => setActivePage('风险')} type="button">
              <span>最大回撤</span>
              <strong>-{Math.abs(drawdown).toFixed(2)}%</strong>
              <em>{drawdownCaption}</em>
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
        查看收益贡献
      </button>
    </section>
  )
}
