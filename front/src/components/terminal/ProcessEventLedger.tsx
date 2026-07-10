import type { ProcessEventRow } from '../../lib/processEventViewModel'
import { TerminalEmpty } from './ProcessBook'
import { TerminalPanelHeader } from './TerminalPageShell'
import { TerminalDataTable, type TerminalColumn } from './TerminalDataTable'

const columns: TerminalColumn<ProcessEventRow>[] = [
  { key: 'timestamp', label: '时间', sortable: true, value: (row) => row.timestamp, render: (row) => row.timestamp },
  { key: 'symbol', label: '资产', sortable: true, value: (row) => row.symbol, render: (row) => <strong>{row.symbol}</strong> },
  { key: 'market', label: '市场', sortable: true, value: (row) => row.market, render: (row) => row.market },
  { key: 'stage', label: '阶段', sortable: true, value: (row) => row.stage, render: (row) => row.stage },
  { key: 'result', label: '事件', sortable: true, value: (row) => row.result, render: (row) => <span className={`terminal-state ${eventTone(row.result)}`}>{row.result}</span> },
  { key: 'source', label: '来源', sortable: true, value: (row) => row.source, render: (row) => row.source },
  { key: 'latency', label: '延迟', sortable: true, value: (row) => Number.parseFloat(row.latency), render: (row) => row.latency },
  { key: 'reason', label: '原因', value: (row) => row.reason, className: 'reason-cell', render: (row) => row.reason },
]

export function ProcessEventLedger({ rows }: { rows: ProcessEventRow[] }) {
  return (
    <section className="terminal-table-panel process-event-ledger">
      <TerminalPanelHeader eyebrow="EVENT STREAM" meta={`${rows.length} 条`} title="过程事件" />
      {rows.length ? (
        <TerminalDataTable ariaLabel="过程事件账本" columns={columns} rowKey={(row) => row.id} rows={rows} />
      ) : <TerminalEmpty title="暂无过程事件" detail="形成真实事件后，将按时间与序列展示只读审计轨迹。" />}
    </section>
  )
}

function eventTone(result: string) {
  if (/成交|写回|通过/.test(result)) return 'positive'
  if (/拦截|拒绝|取消|放弃/.test(result)) return 'negative'
  if (/等待|复盘/.test(result)) return 'warning'
  return ''
}
