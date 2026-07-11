import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { TopNav } from './TopNav'

describe('TopNav runtime truth', () => {
  it('shows idle scheduler truth instead of claiming automation is running', () => {
    render(<TopNav activePage="总览" heartbeat={{ state: 'idle', headline: '调度正常 · 当前空闲', detail: '尚无过程事件', runningCount: 0, latestEventLabel: '尚无过程事件', snapshotLabel: '快照 刚刚', tone: 'muted' }} setActivePage={vi.fn()} />)

    expect(screen.getByText('调度正常 · 空闲')).toBeInTheDocument()
    expect(screen.queryByText('自动化运行中')).not.toBeInTheDocument()
  })
})
