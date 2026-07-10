import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ContributionPanel } from './ContributionPanel'
import type { SignalRow } from '../../types/dashboard'

const signal: SignalRow = {
  symbol: '000001.SZ',
  name: '平安银行',
  market: 'A-share',
  method: 'buy',
  status: 'executed',
  impact: '--',
  confidence: '70%',
  age: '1小时',
  reason: '已完成',
  next: '进入复盘',
  steps: 6,
}

describe('ContributionPanel', () => {
  it('shows a real empty state when attribution is unavailable', () => {
    render(<ContributionPanel signals={[signal]} />)

    expect(screen.getByLabelText('收益归因状态')).toHaveTextContent('暂无可用收益归因')
    expect(screen.queryByText('buy')).not.toBeInTheDocument()
  })
})
