import type { ReactNode } from 'react'
import type { RuntimeHeartbeat } from '../../lib/runtimeHeartbeat'
import { AutomationHeartbeat } from './AutomationHeartbeat'

export type TerminalMetric = {
  label: string
  value: string
  detail?: string
  tone?: 'positive' | 'negative' | 'warning' | 'muted'
}

export function TerminalPageShell({
  inspector,
  ledger,
  metrics,
  primary,
  title,
  heartbeat,
}: {
  inspector: ReactNode
  ledger?: ReactNode
  metrics: TerminalMetric[]
  primary: ReactNode
  title: string
  heartbeat: RuntimeHeartbeat
}) {
  return (
    <section aria-label={title} className={`terminal-page${ledger ? ' has-ledger' : ''}`} role="region">
      <div aria-label={`${title}指标`} className="terminal-metrics">
        <div className="terminal-metrics-title">{title}</div>
        {metrics.map((metric) => (
          <div className="terminal-metric" key={`${metric.label}-${metric.value}`}>
            <span>{metric.label}</span>
            <strong className={metric.tone}>{metric.value}</strong>
            {metric.detail && <small>{metric.detail}</small>}
          </div>
        ))}
      </div>
      <div className="terminal-primary-grid">
        <div className="terminal-primary">{primary}</div>
        <aside aria-label={`${title}检查器`} className="terminal-inspector"><AutomationHeartbeat heartbeat={heartbeat} />{inspector}</aside>
      </div>
      {ledger && <div className="terminal-ledger">{ledger}</div>}
    </section>
  )
}

export function TerminalPanelHeader({ eyebrow, title, meta }: { eyebrow: string; title: string; meta?: string }) {
  return (
    <header className="terminal-panel-header">
      <div>
        <span>{eyebrow}</span>
        <h2>{title}</h2>
      </div>
      {meta && <small>{meta}</small>}
    </header>
  )
}

export function TerminalInspectorSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="terminal-inspector-section">
      <h3>{title}</h3>
      {children}
    </section>
  )
}
