import type { BookTone, SignalRow } from '../../types/dashboard'
import { PanelTitle } from '../PanelTitle'

export function SignalDepth({ signals }: { signals: SignalRow[] }) {
  const signalDepth: { label: string; value: string; total: string; tone: BookTone }[] = [
    { label: '已兑现', value: String(signals.filter((signal) => signal.status === 'executed').length), total: ratio(signals, 'executed'), tone: 'cyan' },
    { label: '观察中', value: String(signals.filter((signal) => signal.status === 'pending').length), total: ratio(signals, 'pending'), tone: 'amber' },
    { label: '已保护', value: String(signals.filter((signal) => signal.status === 'blocked').length), total: ratio(signals, 'blocked'), tone: 'red' },
    { label: '已放弃', value: String(signals.filter((signal) => signal.status === 'cancelled').length), total: ratio(signals, 'cancelled'), tone: 'muted' },
  ]

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

function ratio(signals: SignalRow[], status: SignalRow['status']) {
  if (!signals.length) return '0%'
  return `${Math.round((signals.filter((signal) => signal.status === status).length / signals.length) * 100)}%`
}
