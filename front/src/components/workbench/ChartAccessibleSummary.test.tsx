import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ChartAccessibleSummary } from './ChartAccessibleSummary'

describe('ChartAccessibleSummary', () => {
  it('identifies a single point as not yet forming a history', () => {
    render(
      <ChartAccessibleSummary
        id="summary"
        latest={{ day: '现在', simulated: 1, target: 8, benchmark: 0, opportunity: -1 }}
        pointCount={1}
      />,
    )

    expect(screen.getByLabelText('收益曲线摘要')).toHaveTextContent('历史曲线尚未形成')
    expect(screen.getByLabelText('收益曲线摘要')).toHaveTextContent('当前收益 +1.00%')
  })
})
