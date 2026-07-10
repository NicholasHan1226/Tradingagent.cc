import { getActionableSignals } from '../../lib/dashboard'
import type { Page, SignalRow } from '../../types/dashboard'
import { PanelTitle } from '../PanelTitle'

export function OpportunityFocus({
  hasSignalData = true,
  setActivePage,
  signals,
}: {
  hasSignalData?: boolean
  setActivePage: (page: Page) => void
  signals: SignalRow[]
}) {
  const topSignals = getActionableSignals(signals).slice(0, 3)

  return (
    <section className="panel rail-panel">
      <PanelTitle action="完整过程" kicker="自动化过程" onAction={() => setActivePage('过程')} title={hasSignalData ? '正在运行' : '运行空闲'} />
      <div className="focus-list">
        {hasSignalData ? (
          topSignals.map((signal, index) => (
            <button className="focus-item" key={`${signal.symbol}-${signal.status}-${signal.age}-${index}`} onClick={() => setActivePage('过程')} type="button">
              <span>
                <strong>{signal.symbol}</strong>
                <em>{signal.reason}</em>
              </span>
              <b className={signal.impact.startsWith('-') ? 'red-text' : 'cyan-text'}>{signal.impact}</b>
            </button>
          ))
        ) : (
          <div className="empty-panel-copy">
            <strong>当前没有运行中的自动过程</strong>
            <span>新一轮扫描启动后，这里会自动显示运行状态。</span>
          </div>
        )}
      </div>
    </section>
  )
}
