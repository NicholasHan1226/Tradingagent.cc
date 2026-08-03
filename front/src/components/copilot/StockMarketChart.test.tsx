import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { getDemoStockIntelligence } from '../../copilot/stockIntelligence'
import { StockMarketChart } from './StockMarketChart'

describe('StockMarketChart', () => {
  it('does not plot a forecast whose formal readiness blocks every use', () => {
    const intelligence = getDemoStockIntelligence('000400.SZ')
    if (!intelligence?.forecast) throw new Error('expected demo forecast fixture')
    const blockedIntelligence = {
      ...intelligence,
      forecast: {
        ...intelligence.forecast,
        readiness: {
          ...intelligence.forecast.readiness,
          status: 'blocked' as const,
          usableFor: 'none' as const,
        },
      },
    }

    render(<StockMarketChart
      intelligence={blockedIntelligence}
      range="1D"
      showForecast
      onRangeChange={vi.fn()}
      onToggleForecast={vi.fn()}
    />)

    expect(screen.getByRole('button', { name: '预测已阻断' })).toBeDisabled()
    expect(screen.getByRole('img', { name: '许继电气 1D 行情图' })).toBeInTheDocument()
    expect(screen.queryByText('预测中位线')).not.toBeInTheDocument()
    expect(screen.queryByText('研究情景')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '预测已阻断' }))
    expect(screen.getByRole('img', { name: '许继电气 1D 行情图' })).toBeInTheDocument()
  })
})
