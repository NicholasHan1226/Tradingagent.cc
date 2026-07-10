import { useState, type ReactNode } from 'react'
import { marketLabels, statusLabels } from '../../data/dashboard'
import { filterAndSortRows, type SortDirection } from '../../lib/terminalTableState'
import type { SignalRow } from '../../types/dashboard'
import { AssetCell } from '../AssetCell'
import { TerminalTableToolbar } from '../terminal/TerminalTableToolbar'
import { Timeline } from '../Timeline'

const stages = ['发现', '成形', '复核', '风控', '推进', '结果']

type SignalColumn = {
  key: string
  label: string
  width: string
  sortable?: boolean
  value: (signal: SignalRow) => string | number
  render: (signal: SignalRow) => ReactNode
}

const columns: SignalColumn[] = [
  { key: 'asset', label: '标的', width: '128px', sortable: true, value: (row) => `${row.symbol} ${row.name}`, render: (row) => <AssetCell symbol={row.symbol} name={row.name} /> },
  { key: 'market', label: '市场', width: '54px', sortable: true, value: (row) => marketLabels[row.market], render: (row) => marketLabels[row.market] },
  { key: 'result', label: '结果', width: '72px', sortable: true, value: (row) => row.status, render: (row) => <span className={`status ${row.status}`}>{row.queueBucket?.toLowerCase() === 'partial' ? '部分成交' : statusLabels[row.status]}</span> },
  { key: 'strategy', label: '策略', width: '112px', sortable: true, value: (row) => `${row.strategyName ?? row.method} ${row.signalSource ?? ''}`, render: (row) => <span title={row.signalSource ? `来源：${row.signalSource}` : undefined}>{row.strategyName ?? row.method}</span> },
  { key: 'reason', label: '为什么', width: 'minmax(180px, 1fr)', value: (row) => row.reason, render: (row) => row.reason },
  { key: 'process', label: '过程', width: '76px', value: (row) => row.steps, render: (row) => <Timeline steps={row.steps} labels={stages} /> },
  { key: 'impact', label: '影响', width: '54px', sortable: true, value: (row) => Number.parseFloat(row.impact.replace(/[^0-9.-]/g, '')), render: (row) => <span className={row.impact.startsWith('-') ? 'red-text' : 'cyan-text'}>{row.impact}</span> },
  { key: 'confidence', label: '置信度', width: '58px', sortable: true, value: (row) => Number.parseFloat(row.confidence), render: (row) => row.confidence },
  { key: 'evidence', label: '证据', width: '50px', sortable: true, value: (row) => formatEvidence(row.stageEvidence), render: (row) => formatEvidence(row.stageEvidence) },
  { key: 'calibration', label: '自动校准', width: '130px', value: (row) => row.next, render: (row) => row.next },
  { key: 'age', label: '时间', width: '48px', sortable: true, value: (row) => Number.parseFloat(row.age), render: (row) => row.age },
]

export function SignalTable({ signals, showTools = false }: { signals: SignalRow[]; showTools?: boolean }) {
  const [query, setQuery] = useState('')
  const [sortKey, setSortKey] = useState('')
  const [direction, setDirection] = useState<SortDirection>('asc')
  const [visibleKeys, setVisibleKeys] = useState(() => new Set(columns.map((column) => column.key)))
  const visibleColumns = showTools ? columns.filter((column) => visibleKeys.has(column.key)) : columns
  const sortColumn = columns.find((column) => column.key === sortKey)
  const visibleSignals = showTools
    ? filterAndSortRows(signals, query, (row) => columns.map((column) => column.value(row)).join(' '), sortColumn?.value, direction)
    : signals
  const gridTemplateColumns = visibleColumns.map((column) => column.width).join(' ')

  return <>
    {showTools && <TerminalTableToolbar columns={columns} direction={direction} onDirectionChange={setDirection} onQueryChange={setQuery} onSortChange={setSortKey} onVisibleChange={setVisibleKeys} query={query} resultCount={visibleSignals.length} sortKey={sortKey} visibleKeys={visibleKeys} />}
    <div aria-label="结果与复盘表" className="terminal-table signal-table" role="table">
      <div className="terminal-row terminal-head" role="row" style={{ gridTemplateColumns }}>
        {visibleColumns.map((column) => <span key={column.key} role="columnheader">{column.label}</span>)}
      </div>
      {visibleSignals.map((signal, index) => <div className="terminal-row" key={`${signal.symbol}-${signal.status}-${signal.age}-${index}`} role="row" style={{ gridTemplateColumns }}>
        {visibleColumns.map((column) => <div key={column.key} role="cell">{column.render(signal)}</div>)}
      </div>)}
    </div>
  </>
}

function formatEvidence(value?: SignalRow['stageEvidence']) {
  if (value === 'full') return '完整'
  if (value === 'partial') return '有限'
  if (value === 'replay') return '回放'
  return '—'
}
