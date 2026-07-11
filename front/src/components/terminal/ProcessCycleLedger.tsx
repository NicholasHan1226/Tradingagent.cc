import type { ProcessCycleRow } from '../../lib/processCycleViewModel'
import { TerminalEmpty } from './ProcessBook'
import { TerminalPanelHeader } from './TerminalPageShell'

export function ProcessCycleLedger({ rows }: { rows: ProcessCycleRow[] }) {
  return (
    <section aria-label="机会周期账本" className="terminal-table-panel process-cycle-ledger">
      <TerminalPanelHeader eyebrow="OPPORTUNITY CYCLES" meta={`${rows.length} 个周期`} title="决策因果链" />
      {rows.length ? <div className="process-cycle-list">{rows.map((row) => <CycleRow key={row.id} row={row} />)}</div> : <TerminalEmpty title="运行空闲" detail="新机会形成后会按发现、研判、风控、待确认和结果聚合。" />}
    </section>
  )
}

function CycleRow({ row }: { row: ProcessCycleRow }) {
  return (
    <article className="process-cycle-row">
      <div className="process-cycle-asset"><strong>{row.symbol}</strong><span>{row.market}</span><small>{row.id}</small></div>
      <ol aria-label={`${row.symbol}阶段`} className="process-cycle-stages">{row.stages.map((stage) => <li className={stage.state} key={stage.label}><i /><span>{stage.label}</span></li>)}</ol>
      <div className="process-cycle-result"><strong>{row.result}</strong><span>{row.evidence} · {row.latency}</span><small>{row.reason}</small></div>
      <div className="process-cycle-meta"><strong>{row.updatedAt}</strong><span>{row.source}</span></div>
    </article>
  )
}
