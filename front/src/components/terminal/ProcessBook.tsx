import type { ProcessBookRow } from '../../lib/terminalViewModels'
import { TerminalPanelHeader } from './TerminalPageShell'
import { TerminalDataTable, type TerminalColumn } from './TerminalDataTable'

const columns: TerminalColumn<ProcessBookRow>[] = [
  { key: 'process', label: '流程', sortable: true, value: (row) => `${row.symbol} ${row.name} ${row.process}`, render: (row) => <><strong>{row.symbol}</strong>{row.name && <small>{row.name}</small>}<span>{row.process}</span></> },
  { key: 'market', label: '市场', sortable: true, value: (row) => row.market, render: (row) => row.market },
  { key: 'stage', label: '阶段', sortable: true, value: (row) => row.stage, render: (row) => row.stage },
  { key: 'state', label: '状态', sortable: true, value: (row) => row.state, render: (row) => <Tone text={row.state} /> },
  { key: 'evidence', label: '证据', sortable: true, value: (row) => row.evidence, render: (row) => row.evidence },
  { key: 'latency', label: '耗时', sortable: true, value: (row) => Number.parseFloat(row.latency), render: (row) => row.latency },
  { key: 'result', label: '结果', sortable: true, value: (row) => row.result, render: (row) => row.result },
  { key: 'updatedAt', label: '更新', sortable: true, value: (row) => row.updatedAt, render: (row) => row.updatedAt },
]

export function ProcessBook({ mode, rows, title }: { mode: 'running' | 'completed' | 'empty'; rows: ProcessBookRow[]; title: string }) {
  return (
    <section className="terminal-table-panel process-book">
      <TerminalPanelHeader eyebrow={mode === 'running' ? 'LIVE PROCESS' : 'PROCESS HISTORY'} meta={`${rows.length} 条`} title={title} />
      {rows.length ? (
        <TerminalDataTable ariaLabel={`${title}过程账本`} columns={columns} rowKey={(row) => `${row.symbol}-${row.updatedAt}`} rows={rows} />
      ) : <TerminalEmpty title="运行空闲" detail="等待下一轮自动调度；历史结果会在形成后进入过程账本。" />}
    </section>
  )
}

function Tone({ text }: { text: string }) {
  const tone = /执行|成交|写回/.test(text) ? 'positive' : /拦截|取消/.test(text) ? 'negative' : /复盘|等待|部分/.test(text) ? 'warning' : ''
  return <span className={`terminal-state ${tone}`}>{text}</span>
}

export function TerminalEmpty({ title, detail }: { title: string; detail: string }) {
  return <div className="terminal-empty"><strong>{title}</strong><span>{detail}</span></div>
}
