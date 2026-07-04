import { getActionableSignals } from '../../lib/dashboard'
import type { Page, SignalRow } from '../../types/dashboard'
import { PanelTitle } from '../PanelTitle'

export function OpportunityFocus({ setActivePage, signals }: { setActivePage: (page: Page) => void; signals: SignalRow[] }) {
  const topSignals = getActionableSignals(signals).slice(0, 3)

  return (
    <section className="panel rail-panel">
      <PanelTitle action="全部机会" kicker="当前机会" onAction={() => setActivePage('机会')} title="下一步" />
      <div className="focus-list">
        {topSignals.map((signal) => (
          <button className="focus-item" key={signal.symbol} onClick={() => setActivePage('机会')} type="button">
            <span>
              <strong>{signal.symbol}</strong>
              <em>{signal.reason}</em>
            </span>
            <b className={signal.impact.startsWith('-') ? 'red-text' : 'cyan-text'}>{signal.impact}</b>
          </button>
        ))}
      </div>
    </section>
  )
}
