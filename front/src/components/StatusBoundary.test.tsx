import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { StatusBoundary } from './StatusBoundary'

describe('StatusBoundary', () => {
  it('renders ready content without adding state noise', () => {
    render(
      <StatusBoundary status="ready">
        <div>模拟盘收益走势</div>
      </StatusBoundary>,
    )

    expect(screen.getByText('模拟盘收益走势')).toBeInTheDocument()
    expect(screen.queryByText('数据有延迟，后台正在刷新')).not.toBeInTheDocument()
  })

  it('renders an error action when a panel cannot load', () => {
    const onRetry = vi.fn()

    render(
      <StatusBoundary status="error" onRetry={onRetry}>
        <div>模拟盘收益走势</div>
      </StatusBoundary>,
    )

    expect(screen.getByText('这块数据暂时不可用')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重新检查' })).toBeInTheDocument()
  })

  it('explains the live-account gate without exposing execution controls', () => {
    render(
      <StatusBoundary status="live-gated">
        <div>真实资金结果</div>
      </StatusBoundary>,
    )

    expect(screen.getByText('实盘还没有接入')).toBeInTheDocument()
    expect(screen.queryByText('真实资金结果')).not.toBeInTheDocument()
  })
})
