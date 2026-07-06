import { getHomeOutcome } from '../../lib/dashboard'
import type { HoldingRow, Page, PortfolioSummary, SignalRow } from '../../types/dashboard'
import { PanelTitle } from '../PanelTitle'

export function HomeResultBrief({
  hasSignalData,
  holdings,
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

  return (
    <section className="panel rail-panel home-result-brief">
      <PanelTitle action="看机会" kicker="首页摘要" onAction={() => setActivePage('机会')} title="下一步关注" />
      <div className="home-brief-section brief-action-section">
        <span className="section-label">需要看的三件事</span>
        <div className="decision-list compact">
          {hasSignalData && leadSignal && (
            <button className="decision-mini-row" onClick={() => setActivePage('机会')} type="button">
              <span>优先跟进</span>
              <strong>{leadSignal.symbol}</strong>
              <em>{leadSignal.reason}</em>
            </button>
          )}
          {hasSignalData && !leadSignal && leadingHolding && (
            <button className="decision-mini-row" onClick={() => setActivePage('持仓')} type="button">
              <span>继续观察</span>
              <strong>{leadingHolding.symbol}</strong>
              <em>{leadingHolding.name}</em>
            </button>
          )}
          {hasSignalData && blockedSignal && (
            <button className="decision-mini-row" onClick={() => setActivePage('风险')} type="button">
              <span>先不要追</span>
              <strong>{blockedSignal.symbol}</strong>
              <em>{blockedSignal.reason}</em>
            </button>
          )}
          {hasSignalData && reviewSignal && (
            <button className="decision-mini-row" onClick={() => setActivePage('复盘')} type="button">
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
      <button className="secondary-action" onClick={() => setActivePage('收益')} type="button">
        看完整收益
      </button>
    </section>
  )
}
