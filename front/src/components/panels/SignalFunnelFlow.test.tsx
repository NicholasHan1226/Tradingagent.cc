import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { SignalFunnelFlow } from './SignalFunnelFlow'
import type { FunnelEvent } from '../../types/dashboard'

describe('SignalFunnelFlow legacy isolation', () => {
  it('does not present a frozen legacy opportunity log as a live running funnel', () => {
    const events: FunnelEvent[] = [{
      id: 'legacy-event-1',
      opportunityId: 'legacy-opportunity-1',
      symbol: '600519.SH',
      market: 'A-share',
      stage: '发现',
      status: '进入',
      label: '旧漏斗历史',
      source: 'legacy_frozen_opportunity_log',
    }]

    render(<SignalFunnelFlow events={events} hasSignalData={false} holdings={[]} signals={[]} />)

    expect(screen.queryByText('实时运行')).not.toBeInTheDocument()
    expect(screen.getByText('空闲')).toBeInTheDocument()
    expect(screen.getByText(/等待下一轮调度/)).toBeInTheDocument()
  })

  it('does not upgrade a derived queue projection to an explicit live opportunity flow', () => {
    const events: FunnelEvent[] = [{
      id: 'queue-event-1',
      opportunityId: 'queue-opportunity-1',
      symbol: '600519.SH',
      market: 'A-share',
      stage: '发现',
      status: '进入',
      label: '队列状态',
      source: 'signal_queue',
    }]

    render(<SignalFunnelFlow events={events} hasSignalData={false} holdings={[]} signals={[]} />)

    expect(screen.queryByText('实时运行')).not.toBeInTheDocument()
    expect(screen.getByText('空闲')).toBeInTheDocument()
  })
})
