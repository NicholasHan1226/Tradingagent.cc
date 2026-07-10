import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { WorkbenchBlotter } from './WorkbenchBlotter'
import type { HoldingRow, SignalRow } from '../../types/dashboard'

const pending: SignalRow = {
  symbol: '0700.HK',
  name: '腾讯',
  market: 'HK',
  method: '事件',
  status: 'pending',
  impact: '--',
  confidence: '80%',
  age: '1小时',
  reason: '等待确认',
  next: '继续观察',
  steps: 4,
}

const executed: SignalRow = {
  ...pending,
  symbol: 'AAPL.US',
  name: '苹果',
  market: 'US',
  status: 'executed',
  reason: '已经形成结果',
}

const partiallyFilled: SignalRow = {
  ...executed,
  symbol: 'BTC-USD',
  name: 'Bitcoin',
  queueBucket: 'partial',
}

const holding: HoldingRow = {
  symbol: '600519.SH',
  name: '贵州茅台',
  market: 'A-share',
  weight: '¥1万',
  pnl: '+¥20',
  risk: '正常',
  role: '模拟盘持仓',
}

describe('WorkbenchBlotter', () => {
  it('starts on active opportunities without terminal rows', () => {
    render(<WorkbenchBlotter active={[pending]} positions={[holding]} completed={[executed]} review={[]} />)

    const panel = screen.getByRole('tabpanel', { name: '当前机会' })
    expect(within(panel).getByRole('table', { name: '当前机会表' })).toBeInTheDocument()
    expect(within(panel).getAllByText('0700.HK').length).toBeGreaterThan(0)
    expect(within(panel).queryByText('AAPL.US')).not.toBeInTheDocument()
  })

  it('shows a truthful empty state instead of completed rows', () => {
    render(<WorkbenchBlotter active={[]} positions={[holding]} completed={[executed]} review={[]} />)

    expect(screen.getByText('当前没有待处理机会')).toBeInTheDocument()
    expect(screen.queryByText('AAPL.US')).not.toBeInTheDocument()
  })

  it('switches to completed outcomes', () => {
    render(<WorkbenchBlotter active={[pending]} positions={[holding]} completed={[executed]} review={[]} />)

    fireEvent.click(screen.getByRole('tab', { name: '已完成 1' }))

    expect(screen.getByRole('tabpanel', { name: '已完成' })).toHaveTextContent('AAPL.US')
  })

  it('labels partial fills as partial rather than protected', () => {
    render(<WorkbenchBlotter active={[]} positions={[]} completed={[partiallyFilled]} review={[]} />)

    fireEvent.click(screen.getByRole('tab', { name: '已完成 1' }))

    const panel = screen.getByRole('tabpanel', { name: '已完成' })
    expect(within(panel).getByRole('table', { name: '结果与复盘表' })).toBeInTheDocument()
    expect(within(panel).getByText('部分成交')).toBeInTheDocument()
    expect(within(panel).queryByText('已保护')).not.toBeInTheDocument()
  })
})
