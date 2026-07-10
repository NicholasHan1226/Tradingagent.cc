import { useMemo, useState, type ReactNode } from 'react'
import { filterAndSortRows, type SortDirection } from '../../lib/terminalTableState'
import { TerminalTableToolbar } from './TerminalTableToolbar'

export type TerminalColumn<T> = {
  key: string
  label: string
  render: (row: T) => ReactNode
  value: (row: T) => string | number | null | undefined
  sortable?: boolean
  className?: string
}

export function TerminalDataTable<T>({ ariaLabel, className = '', columns, rowKey, rows }: { ariaLabel: string; className?: string; columns: TerminalColumn<T>[]; rowKey: (row: T) => string; rows: T[] }) {
  const [query, setQuery] = useState('')
  const [sortKey, setSortKey] = useState('')
  const [direction, setDirection] = useState<SortDirection>('asc')
  const [visibleKeys, setVisibleKeys] = useState(() => new Set(columns.map((column) => column.key)))
  const sortColumn = columns.find((column) => column.key === sortKey)
  const visibleColumns = columns.filter((column) => visibleKeys.has(column.key))
  const visibleRows = useMemo(() => filterAndSortRows(
    rows,
    query,
    (row) => columns.map((column) => column.value(row) ?? '').join(' '),
    sortColumn?.value,
    direction,
  ), [columns, direction, query, rows, sortColumn])

  return <>
    <TerminalTableToolbar columns={columns} direction={direction} onDirectionChange={setDirection} onQueryChange={setQuery} onSortChange={setSortKey} onVisibleChange={setVisibleKeys} query={query} resultCount={visibleRows.length} sortKey={sortKey} visibleKeys={visibleKeys} />
    <div className="terminal-table-scroll">
      <table aria-label={ariaLabel} className={`terminal-table ${className}`}>
        <thead><tr>{visibleColumns.map((column) => <th key={column.key}>{column.label}</th>)}</tr></thead>
        <tbody>{visibleRows.map((row) => <tr key={rowKey(row)}>{visibleColumns.map((column) => <td className={column.className} key={column.key}>{column.render(row)}</td>)}</tr>)}</tbody>
      </table>
    </div>
  </>
}
