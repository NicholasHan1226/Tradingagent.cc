import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { MarketHeader } from './MarketHeader'

describe('MarketHeader freshness', () => {
  it('shows stale snapshot truth instead of calling every timestamp latest', () => {
    render(
      <MarketHeader
        accountMode="simulated"
        activeMarket="All Markets"
        activePage="总览"
        completedCount={0}
        hasPerformanceData
        isCnyAccount={false}
        isDemoPreview={false}
        liveProfit={10}
        liveReturn={1}
        maxDrawdown={0.5}
        performanceStatus="stale"
        positionCount={0}
        runningCount={0}
        heartbeat={{ state: 'stale', headline: '快照滞后 · 等待更新', detail: '最近事件 1小时前', runningCount: 0, latestEventLabel: '最近事件 1小时前', snapshotLabel: '快照 1小时前', tone: 'warning' }}
        setActiveMarket={vi.fn()}
        snapshotGeneratedAt="2026-07-04T10:00:00.000Z"
        targetReturn={8}
      />,
    )

    expect(screen.getByText('快照滞后')).toBeInTheDocument()
    expect(screen.queryByText('最新快照')).not.toBeInTheDocument()
  })
})
