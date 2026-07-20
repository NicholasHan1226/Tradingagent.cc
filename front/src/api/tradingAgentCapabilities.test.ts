import { describe, expect, it } from 'vitest'
import { tradingAgentCapabilities } from './tradingAgentCapabilities'

describe('TradingAgent capability map', () => {
  it('documents the readable project capabilities the dashboard can use', () => {
    expect(tradingAgentCapabilities.map((capability) => capability.id)).toEqual([
      'signals',
      'positions',
      'performance',
      'decisions',
      'risk',
      'execution-readiness',
    ])
  })

  it('keeps execution capability marked as gated instead of directly usable', () => {
    const execution = tradingAgentCapabilities.find((capability) => capability.id === 'execution-readiness')

    expect(execution).toMatchObject({
      display: '分市场执行边界',
      status: 'gated',
      dashboardSurface: '执行状态 / 实盘待接入',
    })
    expect(execution?.readableSources).toContain('shared/governance/market_lanes.yaml')
    expect(execution?.readableSources.join('|')).not.toMatch(/signal_card_schema|fill_card_schema|positions_snapshot_schema/)
  })
})
