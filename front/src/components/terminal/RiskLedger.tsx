import type { RiskLedgerRow } from '../../lib/terminalViewModels'
import { TerminalEmpty } from './ProcessBook'
import { TerminalPanelHeader } from './TerminalPageShell'
import { TerminalDataTable, type TerminalColumn } from './TerminalDataTable'

const columns: TerminalColumn<RiskLedgerRow>[] = [
  { key: 'symbol', label: '资产', sortable: true, value: (row) => row.symbol, render: (row) => <strong>{row.symbol}</strong> },
  { key: 'market', label: '市场', sortable: true, value: (row) => row.market, render: (row) => row.market },
  { key: 'stage', label: '阶段', sortable: true, value: (row) => row.stage, render: (row) => row.stage },
  { key: 'gate', label: '处理', sortable: true, value: (row) => row.gate, render: (row) => <span className={`terminal-state ${gateTone(row.gate)}`}>{row.gate}</span> },
  { key: 'evidence', label: '证据', sortable: true, value: (row) => row.evidence, render: (row) => row.evidence },
  { key: 'reason', label: '原因', value: (row) => row.reason, className: 'reason-cell', render: (row) => row.reason },
  { key: 'updatedAt', label: '更新', sortable: true, value: (row) => row.updatedAt, render: (row) => row.updatedAt },
]

export function RiskLedger({ rows }: { rows: RiskLedgerRow[] }) {
  return (
    <section className="terminal-table-panel risk-ledger">
      <TerminalPanelHeader eyebrow="RISK EVENTS" meta={`${rows.length} 条`} title="风险事件账本" />
      {rows.length ? <TerminalDataTable ariaLabel="风险事件账本" columns={columns} rowKey={(row) => `${row.symbol}-${row.updatedAt}`} rows={rows} /> : <TerminalEmpty title="没有风险事件" detail="当前数据中没有安全拦截、错过或自动终止记录。" />}
    </section>
  )
}

function gateTone(gate: string) {
  return /滞后|隔离|复盘/.test(gate) ? 'warning' : 'negative'
}
