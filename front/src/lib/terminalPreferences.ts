export type TerminalDensity = 'compact' | 'comfortable'
export type TerminalPreferences = {
  version: 1
  density: TerminalDensity
  tableColumns: Record<string, string[]>
}

const STORAGE_KEY = 'tradingagent.terminal.preferences.v1'
const DEFAULTS: TerminalPreferences = { version: 1, density: 'compact', tableColumns: {} }
type PreferenceStorage = Pick<Storage, 'getItem' | 'setItem'>

export function readTerminalPreferences(storage: PreferenceStorage | undefined = browserStorage()): TerminalPreferences {
  try {
    const raw = storage?.getItem(STORAGE_KEY)
    if (!raw) return structuredClone(DEFAULTS)
    const value = JSON.parse(raw) as Partial<TerminalPreferences>
    if (value.version !== 1 || (value.density !== 'compact' && value.density !== 'comfortable') || !isColumnMap(value.tableColumns)) return structuredClone(DEFAULTS)
    return { version: 1, density: value.density, tableColumns: value.tableColumns }
  } catch {
    return structuredClone(DEFAULTS)
  }
}

export function writeTerminalPreferences(value: TerminalPreferences, storage: PreferenceStorage | undefined = browserStorage()) {
  try {
    storage?.setItem(STORAGE_KEY, JSON.stringify(value))
  } catch {
    // Local preferences are optional and must never block the terminal.
  }
}

export function updateTableColumns(key: string, columns: string[], storage: PreferenceStorage | undefined = browserStorage()) {
  const current = readTerminalPreferences(storage)
  writeTerminalPreferences({ ...current, tableColumns: { ...current.tableColumns, [key]: [...columns] } }, storage)
}

function browserStorage() {
  try { return typeof window === 'undefined' ? undefined : window.localStorage }
  catch { return undefined }
}

function isColumnMap(value: unknown): value is Record<string, string[]> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  return Object.values(value).every((item) => Array.isArray(item) && item.every((key) => typeof key === 'string'))
}
