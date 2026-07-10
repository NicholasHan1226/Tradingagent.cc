export type SortDirection = 'asc' | 'desc'

export function filterAndSortRows<T>(
  rows: T[],
  query: string,
  searchAccessor: (row: T) => string,
  sortAccessor: ((row: T) => string | number | null | undefined) | undefined,
  direction: SortDirection,
) {
  const normalized = query.trim().toLocaleLowerCase()
  const filtered = normalized
    ? rows.filter((row) => searchAccessor(row).toLocaleLowerCase().includes(normalized))
    : [...rows]
  if (!sortAccessor) return filtered
  return filtered.sort((left, right) => compare(sortAccessor(left), sortAccessor(right)) * (direction === 'asc' ? 1 : -1))
}

function compare(left: string | number | null | undefined, right: string | number | null | undefined) {
  if (left == null && right == null) return 0
  if (left == null) return 1
  if (right == null) return -1
  if (typeof left === 'number' && typeof right === 'number') return left - right
  return String(left).localeCompare(String(right), 'zh-CN', { numeric: true })
}
