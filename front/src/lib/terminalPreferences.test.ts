import { beforeEach, describe, expect, it } from 'vitest'
import { readTerminalPreferences, updateTableColumns, writeTerminalPreferences } from './terminalPreferences'

describe('terminal preferences', () => {
  let values: Map<string, string>
  let storage: Pick<Storage, 'getItem' | 'setItem'>

  beforeEach(() => {
    values = new Map()
    storage = { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => { values.set(key, value) } }
  })

  it('reads defaults and persists versioned density and ledger columns', () => {
    expect(readTerminalPreferences(storage)).toEqual({ version: 1, density: 'compact', tableColumns: {} })
    writeTerminalPreferences({ version: 1, density: 'comfortable', tableColumns: {} }, storage)
    updateTableColumns('过程事件账本', ['timestamp', 'symbol', 'result'], storage)

    expect(readTerminalPreferences(storage)).toEqual({ version: 1, density: 'comfortable', tableColumns: { '过程事件账本': ['timestamp', 'symbol', 'result'] } })
  })

  it('fails closed to defaults for malformed or old preference payloads', () => {
    storage.setItem('tradingagent.terminal.preferences.v1', '{bad')
    expect(readTerminalPreferences(storage).density).toBe('compact')
    storage.setItem('tradingagent.terminal.preferences.v1', JSON.stringify({ version: 0, density: 'comfortable' }))
    expect(readTerminalPreferences(storage)).toEqual({ version: 1, density: 'compact', tableColumns: {} })
  })
})
