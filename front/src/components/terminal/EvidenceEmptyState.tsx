import type { EvidenceEmptyModel } from '../../lib/terminalDensity'

export function EvidenceEmptyState({ model }: { model: EvidenceEmptyModel }) {
  return (
    <section aria-label={model.title} className="evidence-empty-state">
      <header><span>NO OPEN EXPOSURE</span><strong>{model.title}</strong><p>{model.detail}</p></header>
      <dl>{model.rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
    </section>
  )
}
