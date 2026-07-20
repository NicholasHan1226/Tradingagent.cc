import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { WorkbenchBlotter } from './WorkbenchBlotter'
import type { HoldingRow, SignalRow } from '../../types/dashboard'

const pending: SignalRow = {
  symbol: '600519.SH',
  name: '贵州茅台',
  market: 'A-share',
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
  symbol: 'IF2601.CFFEX',
  name: '沪深300期指',
  market: 'CNFutures',
  status: 'executed',
  reason: '已经形成结果',
}

const partiallyFilled: SignalRow = {
  ...executed,
  symbol: 'BTC-USDT',
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
  it('starts on running automation without terminal rows', () => {
    render(<WorkbenchBlotter active={[pending]} positions={[holding]} completed={[executed]} review={[]} />)

    const panel = screen.getByRole('tabpanel', { name: '运行中' })
    expect(within(panel).getByRole('table', { name: '自动运行过程表' })).toBeInTheDocument()
    expect(within(panel).getByRole('columnheader', { name: '当前阶段' })).toBeInTheDocument()
    expect(within(panel).getAllByText('600519.SH').length).toBeGreaterThan(0)
    expect(within(panel).queryByText('IF2601.CFFEX')).not.toBeInTheDocument()
  })

  it('reveals completed outcomes when the running queue is empty', () => {
    render(<WorkbenchBlotter active={[]} positions={[holding]} completed={[executed]} review={[]} />)

    expect(screen.getByRole('tabpanel', { name: '已完成' })).toHaveTextContent('IF2601.CFFEX')
    expect(screen.queryByText('当前没有运行中的自动过程')).not.toBeInTheDocument()
  })

  it('switches to completed outcomes', () => {
    render(<WorkbenchBlotter active={[pending]} positions={[holding]} completed={[executed]} review={[]} />)

    fireEvent.click(screen.getByRole('tab', { name: '已完成 1' }))

    expect(screen.getByRole('tabpanel', { name: '已完成' })).toHaveTextContent('IF2601.CFFEX')
  })

  it('labels partial fills as partial rather than protected', () => {
    render(<WorkbenchBlotter active={[]} positions={[]} completed={[partiallyFilled]} review={[]} />)

    fireEvent.click(screen.getByRole('tab', { name: '已完成 1' }))

    const panel = screen.getByRole('tabpanel', { name: '已完成' })
    expect(within(panel).getByRole('table', { name: '结果与复盘表' })).toBeInTheDocument()
    expect(within(panel).getByText('部分成交')).toBeInTheDocument()
    expect(within(panel).queryByText('已保护')).not.toBeInTheDocument()
  })

  it('uses process and result tabs without decision language', () => {
    render(<WorkbenchBlotter active={[pending]} positions={[holding]} completed={[executed]} review={[executed]} />)

    expect(screen.getByRole('tab', { name: '运行中 1' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: '自动复盘 1' })).toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: /当前机会|待复盘/ })).not.toBeInTheDocument()
  })

  it('labels the read-only next-step field as automatic calibration', () => {
    render(<WorkbenchBlotter active={[]} positions={[]} completed={[executed]} review={[]} />)

    expect(screen.getByRole('columnheader', { name: '自动校准' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: '置信度' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: '证据' })).toBeInTheDocument()
    expect(screen.queryByRole('columnheader', { name: '下次规则' })).not.toBeInTheDocument()
  })

  it('moves to completed results when an async snapshot leaves running empty', () => {
    const { rerender } = render(<WorkbenchBlotter active={[]} positions={[]} completed={[]} review={[]} />)

    rerender(<WorkbenchBlotter active={[]} positions={[]} completed={[executed]} review={[]} />)

    expect(screen.getByRole('tabpanel', { name: '已完成' })).toHaveTextContent('IF2601.CFFEX')
    expect(screen.getByRole('tab', { name: '已完成 1' })).toHaveAttribute('aria-selected', 'true')
  })
})
