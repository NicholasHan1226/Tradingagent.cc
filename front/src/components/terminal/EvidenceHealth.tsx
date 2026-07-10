import type { EvidenceHealthModel } from '../../lib/marketTapeViewModel'

export function EvidenceHealth({ model }: { model: EvidenceHealthModel }) {
  return (
    <section aria-label="证据健康" className={`evidence-health ${model.overall}`}>
      <header><span>证据</span><strong>{model.overall === 'positive' ? '完整' : model.overall === 'warning' ? '有限' : '异常'}</strong></header>
      <div className="evidence-health-domains">
        {model.items.map((item) => <span className={item.tone} key={item.domain} title={item.state}><i />{item.label}</span>)}
      </div>
      <small>{model.snapshotLabel} · {model.sourceLabel}</small>
    </section>
  )
}
