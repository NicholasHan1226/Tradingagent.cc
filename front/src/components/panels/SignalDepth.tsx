import { signalDepth } from '../../data/dashboard'
import { PanelTitle } from '../PanelTitle'

export function SignalDepth() {
  return (
    <section className="panel rail-panel">
      <PanelTitle kicker="机会概览" title="近24小时结果" />
      <div className="signal-depth">
        {signalDepth.map((row) => (
          <div className={`depth-row ${row.tone}`} key={row.label}>
            <span>{row.label}</span>
            <strong>{row.value}</strong>
            <em>{row.total}</em>
          </div>
        ))}
      </div>
    </section>
  )
}
