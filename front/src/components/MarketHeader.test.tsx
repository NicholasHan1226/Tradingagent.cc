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
        pnlCurrency="CNY"
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

  it('shows Crypto profit as native USDT and does not guess a currency when it is unknown', () => {
    const baseProps = {
      accountMode: 'simulated' as const,
      activeMarket: 'Crypto' as const,
      activePage: '总览' as const,
      completedCount: 1,
      hasPerformanceData: true,
      isDemoPreview: false,
      liveProfit: 125,
      liveReturn: 2.5,
      maxDrawdown: 0.5,
      performanceStatus: 'ready' as const,
      positionCount: 1,
      runningCount: 0,
      heartbeat: { state: 'idle' as const, headline: '当前空闲', detail: '等待机会', runningCount: 0, latestEventLabel: '刚刚', snapshotLabel: '刚刚', tone: 'muted' as const },
      setActiveMarket: vi.fn(),
      snapshotGeneratedAt: '2026-07-04T10:00:00.000Z',
      targetReturn: 8,
    }
    const { rerender } = render(<MarketHeader {...baseProps} pnlCurrency="USDT" />)
    expect(screen.getByText('+125 USDT')).toBeInTheDocument()
    expect(screen.queryByText('$125.00')).not.toBeInTheDocument()

    rerender(<MarketHeader {...baseProps} pnlCurrency={undefined} />)
    expect(screen.getAllByText('+2.50%').length).toBeGreaterThan(0)
    expect(screen.queryByText('$125.00')).not.toBeInTheDocument()
    expect(screen.queryByText('¥125')).not.toBeInTheDocument()

    rerender(<MarketHeader {...baseProps} pnlCurrency="USD" />)
    expect(screen.getByText('--')).toBeInTheDocument()
    expect(screen.queryByText('$125.00')).not.toBeInTheDocument()
  })
})
