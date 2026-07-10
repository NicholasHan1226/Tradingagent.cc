import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

describe('Vite environment replacement contract', () => {
  it('uses direct import.meta.env access so Vite can replace browser config', () => {
    const appSource = readFileSync('src/App.tsx', 'utf8')
    const integrationSource = readFileSync('src/api/tradingAgentIntegration.ts', 'utf8')

    expect(appSource).toContain('import.meta.env.VITE_TRADING_AGENT_DEMO_PREVIEW')
    expect(appSource).toContain('import.meta.env.DEV')
    expect(integrationSource).toContain('import.meta.env.VITE_TRADING_AGENT_SNAPSHOT_URL')
  })
})
