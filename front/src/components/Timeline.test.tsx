import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Timeline } from './Timeline'

describe('Timeline', () => {
  it('renders every label and marks completed steps', () => {
    const { container } = render(
      <Timeline steps={2} labels={['开盘', '午间', '收盘']} />,
    )

    expect(screen.getByText('开盘')).toBeTruthy()
    expect(screen.getByText('午间')).toBeTruthy()
    expect(screen.getByText('收盘')).toBeTruthy()
    const markers = container.querySelectorAll('.timeline-track b')
    expect(markers).toHaveLength(3)
    expect(
      [...markers].filter((node) => node.className.includes('on')),
    ).toHaveLength(2)
  })

  it('caps the progress width at six steps', () => {
    const { container } = render(
      <Timeline steps={99} labels={['a', 'b', 'c', 'd', 'e', 'f', 'g']} />,
    )

    const track = container.querySelector('.timeline-track i')
    const width = parseFloat(
      (track?.getAttribute('style') ?? '').replace(/[^0-9.]/g, ''),
    )
    expect(width).toBeGreaterThanOrEqual(100)
  })
})
