import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, describe, expect, it } from 'vitest'
import { useTerminalNavigation } from './useTerminalNavigation'
import type { Market, Page, PerformanceRange } from '../types/dashboard'

function Harness() {
  const [page, setPage] = useState<Page>('总览')
  const [market, setMarket] = useState<Market>('All Markets')
  const [range, setRange] = useState<PerformanceRange>('all')
  const [opportunity, setOpportunity] = useState<string | null>(null)
  useTerminalNavigation({ page, market, range, opportunity, setPage, setMarket, setRange, setOpportunity })
  return <><output>{page}|{market}|{range}|{opportunity ?? 'none'}</output><input data-terminal-search aria-label="终端搜索" /><button onClick={() => setOpportunity('opp-1')}>选择机会</button></>
}

describe('terminal navigation', () => {
  afterEach(() => window.history.replaceState(null, '', '/'))

  it('persists page state and supports page and market shortcuts', () => {
    render(<Harness />)
    fireEvent.keyDown(window, { key: '2', altKey: true })
    expect(screen.getByText(/^收益\|/)).toBeInTheDocument()
    expect(new URL(window.location.href).searchParams.get('page')).toBe('收益')

    fireEvent.keyDown(window, { key: 'ArrowRight', altKey: true })
    expect(screen.getByText(/\|A-share\|/)).toBeInTheDocument()
  })

  it('focuses search with slash but ignores shortcuts inside editable fields', () => {
    render(<Harness />)
    fireEvent.keyUp(window, { key: '/' })
    expect(screen.getByRole('textbox', { name: '终端搜索' })).toHaveFocus()

    fireEvent.keyDown(screen.getByRole('textbox'), { key: '4', altKey: true })
    expect(screen.getByText(/^总览\|/)).toBeInTheDocument()
  })

  it('restores page, market and range from browser history state', () => {
    render(<Harness />)
    window.history.replaceState(null, '', '/?page=%E9%A3%8E%E9%99%A9&market=Crypto&range=7d')
    fireEvent.popState(window)

    expect(screen.getByText('风险|Crypto|7d|none')).toBeInTheDocument()
  })

  it('persists and restores selected opportunity context', () => {
    render(<Harness />)
    fireEvent.click(screen.getByText('选择机会'))
    expect(new URL(window.location.href).searchParams.get('opportunity')).toBe('opp-1')

    window.history.replaceState(null, '', '/?page=%E8%BF%87%E7%A8%8B&market=A-share&range=all&opportunity=opp-2')
    fireEvent.popState(window)
    expect(screen.getByText('过程|A-share|all|opp-2')).toBeInTheDocument()
  })
})
