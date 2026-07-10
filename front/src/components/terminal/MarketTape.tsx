import type { EvidenceHealthModel, MarketTapeRow } from '../../lib/marketTapeViewModel'
import type { Market } from '../../types/dashboard'
import { EvidenceHealth } from './EvidenceHealth'

export function MarketTape({ rows, evidence, onSelect }: { rows: MarketTapeRow[]; evidence: EvidenceHealthModel; onSelect: (market: Market) => void }) {
  return (
    <nav aria-label="市场状态带" className="market-tape">
      <div className="market-tape-rows">
        {rows.map((row) => <button aria-current={row.selected ? 'page' : undefined} className={`${row.selected ? 'selected' : ''} ${row.tone}`} key={row.market} onClick={() => onSelect(row.market)} type="button">
          <span><i />{row.label}</span><strong>{row.returnLabel}</strong><small>{row.holdingsLabel} · {row.runtimeLabel}</small>
        </button>)}
      </div>
      <EvidenceHealth model={evidence} />
    </nav>
  )
}
