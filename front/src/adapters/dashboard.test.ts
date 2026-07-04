import { describe, expect, it } from 'vitest'
import { toDashboardState } from './dashboard'
import type { DashboardApiResponse } from '../api/types'

describe('dashboard adapter', () => {
  it('keeps simulated and live-account state separate', () => {
    const response: DashboardApiResponse = {
      mode: 'simulated',
      status: 'ready',
      domains: {
        performance: { status: 'ready', updatedAt: '2026-07-04T10:00:00.000Z' },
        signals: { status: 'empty', updatedAt: '2026-07-04T10:00:00.000Z', message: '当前没有需要处理的机会' },
        holdings: { status: 'ready', updatedAt: '2026-07-04T10:00:00.000Z' },
        decisions: { status: 'stale', updatedAt: '2026-07-04T09:58:00.000Z' },
        risk: { status: 'error', updatedAt: '2026-07-04T10:00:00.000Z', message: '风险数据暂时不可用' },
      },
    }

    const state = toDashboardState(response)

    expect(state.mode).toBe('simulated')
    expect(state.status).toBe('ready')
    expect(state.domains.signals.status).toBe('empty')
    expect(state.domains.decisions.status).toBe('stale')
    expect(state.domains.risk.message).toBe('风险数据暂时不可用')
  })

  it('gates every panel when live account is selected but not enabled', () => {
    const response: DashboardApiResponse = {
      mode: 'live-disabled',
      status: 'ready',
      domains: {
        performance: { status: 'ready', updatedAt: '2026-07-04T10:00:00.000Z' },
        signals: { status: 'ready', updatedAt: '2026-07-04T10:00:00.000Z' },
        holdings: { status: 'ready', updatedAt: '2026-07-04T10:00:00.000Z' },
        decisions: { status: 'ready', updatedAt: '2026-07-04T10:00:00.000Z' },
        risk: { status: 'ready', updatedAt: '2026-07-04T10:00:00.000Z' },
      },
    }

    const state = toDashboardState(response)

    expect(Object.values(state.domains).map((domain) => domain.status)).toEqual([
      'live-gated',
      'live-gated',
      'live-gated',
      'live-gated',
      'live-gated',
    ])
  })
})
