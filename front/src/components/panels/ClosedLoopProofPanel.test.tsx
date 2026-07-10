import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ClosedLoopProofPanel } from './ClosedLoopProofPanel'
import type { MarketSummary } from '../../types/dashboard'

const summary: MarketSummary = {
  market: 'Crypto',
  status: 'partial',
  runtimeState: 'strategy_wait',
  runtimeReason: 'market_data_missing',
  holdingCount: 0,
  signalCount: 0,
  tradeCount: 0,
  styleCount: 0,
  source: 'runtime',
  headline: '等待数据',
  detail: '等待数据',
}

describe('ClosedLoopProofPanel', () => {
  it('normalizes backend runtime codes into user-facing copy', () => {
    render(<ClosedLoopProofPanel summaries={[summary]} />)

    expect(screen.getByText('等待行情数据')).toBeInTheDocument()
    expect(screen.queryByText('market_data_missing')).not.toBeInTheDocument()
  })
})
