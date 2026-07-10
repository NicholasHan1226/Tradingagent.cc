import { Columns3, Search } from 'lucide-react'
import type { SortDirection } from '../../lib/terminalTableState'

export type TableColumnOption = { key: string; label: string; sortable?: boolean }

export function TerminalTableToolbar({ columns, direction, onDirectionChange, onQueryChange, onSortChange, onVisibleChange, query, resultCount, sortKey, visibleKeys }: {
  columns: TableColumnOption[]
  direction: SortDirection
  onDirectionChange: (direction: SortDirection) => void
  onQueryChange: (query: string) => void
  onSortChange: (key: string) => void
  onVisibleChange: (keys: Set<string>) => void
  query: string
  resultCount: number
  sortKey: string
  visibleKeys: Set<string>
}) {
  return (
    <div className="terminal-table-toolbar">
      <label><Search aria-hidden="true" size={13} /><span className="sr-only">搜索账本</span><input aria-label="搜索账本" data-terminal-search onChange={(event) => onQueryChange(event.target.value)} placeholder="搜索资产 / 原因 / 策略" type="search" value={query} /></label>
      <span>{resultCount} 条结果</span>
      <select aria-label="排序字段" onChange={(event) => onSortChange(event.target.value)} value={sortKey}>
        <option value="">默认顺序</option>
        {columns.filter((column) => column.sortable).map((column) => <option key={column.key} value={column.key}>{column.label}</option>)}
      </select>
      <button aria-label="切换排序方向" disabled={!sortKey} onClick={() => onDirectionChange(direction === 'asc' ? 'desc' : 'asc')} type="button">{direction === 'asc' ? '升序' : '降序'}</button>
      <details>
        <summary><Columns3 aria-hidden="true" size={13} />列</summary>
        <div>{columns.map((column) => <label key={column.key}><input checked={visibleKeys.has(column.key)} onChange={() => {
          const next = new Set(visibleKeys)
          if (next.has(column.key) && next.size > 1) next.delete(column.key)
          else next.add(column.key)
          onVisibleChange(next)
        }} type="checkbox" />{column.label}</label>)}</div>
      </details>
    </div>
  )
}
