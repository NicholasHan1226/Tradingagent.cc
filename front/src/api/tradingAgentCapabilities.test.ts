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
      display: '实盘待接入',
      status: 'gated',
      dashboardSurface: '实盘入口',
    })
  })
})
