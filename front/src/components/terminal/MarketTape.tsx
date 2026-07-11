import type { EvidenceHealthModel, MarketTapeRow } from '../../lib/marketTapeViewModel'
import type { Market } from '../../types/dashboard'
import { EvidenceHealth } from './EvidenceHealth'
import { MarketSparkline } from './MarketSparkline'

export function MarketTape({ rows, evidence, onSelect }: { rows: MarketTapeRow[]; evidence: EvidenceHealthModel; onSelect: (market: Market) => void }) {
  return (
    <nav aria-label="市场状态带" className="market-tape">
      <div className="market-tape-rows">
        {rows.map((row) => <button aria-current={row.selected ? 'page' : undefined} className={`${row.selected ? 'selected' : ''} ${row.tone} ${row.pulse ? 'has-pulse' : ''}`} key={row.market} onClick={() => onSelect(row.market)} type="button">
          <span className="market-tape-label"><i />{row.label}</span><strong className="market-tape-return">{row.returnLabel}</strong>
          {row.pulse ? <><span className="market-tape-symbol">{row.pulse.symbol}</span><span className="market-tape-price">{row.pulse.priceLabel}<em className={(row.pulse.points.at(-1) ?? 0) >= (row.pulse.points[0] ?? 0) ? 'positive' : 'negative'}>{row.pulse.changeLabel}</em></span><MarketSparkline label={row.pulse.symbol} points={row.pulse.points} tone={(row.pulse.points.at(-1) ?? 0) > (row.pulse.points[0] ?? 0) ? 'positive' : (row.pulse.points.at(-1) ?? 0) < (row.pulse.points[0] ?? 0) ? 'negative' : 'flat'} /><small>{row.pulse.detailLabel} · {row.pulse.freshness === 'live' ? '新鲜' : row.pulse.freshness === 'stale' ? '滞后' : '降级'}</small></> : <><span className="market-tape-symbol muted">暂无代表行情</span><small>{row.holdingsLabel} · {row.runtimeLabel}</small></>}
        </button>)}
      </div>
      <EvidenceHealth model={evidence} />
    </nav>
  )
}
