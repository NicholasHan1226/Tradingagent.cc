import type { EvidenceHealthModel, MarketPulseHealthModel } from '../../lib/marketTapeViewModel'

export function EvidenceHealth({ model, pulseHealth }: { model: EvidenceHealthModel; pulseHealth?: MarketPulseHealthModel }) {
  return (
    <section aria-label="证据健康" className={`evidence-health ${model.overall}`}>
      <header><span>证据</span><strong>{model.overall === 'positive' ? '完整' : model.overall === 'warning' ? '有限' : '异常'}</strong></header>
      <div className="evidence-health-domains">
        {model.items.map((item) => <span className={item.tone} key={item.domain} title={item.state}><i />{item.label}</span>)}
      </div>
      <small><span>{model.snapshotLabel} · {model.sourceLabel}</span>{pulseHealth && <span aria-label="行情读模型" className={`market-pulse-health ${pulseHealth.tone}`} title={pulseHealth.detail}> · 行情 {pulseHealth.headline}</span>}</small>
    </section>
  )
}
