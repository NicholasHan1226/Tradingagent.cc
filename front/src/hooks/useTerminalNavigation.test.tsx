import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, describe, expect, it } from 'vitest'
import { useTerminalNavigation } from './useTerminalNavigation'
import type { Market, Page, PerformanceRange } from '../types/dashboard'

function Harness() {
  const [page, setPage] = useState<Page>('总览')
  const [market, setMarket] = useState<Market>('All Markets')
  const [range, setRange] = useState<PerformanceRange>('all')
  useTerminalNavigation({ page, market, range, setPage, setMarket, setRange })
  return <><output>{page}|{market}|{range}</output><input data-terminal-search aria-label="终端搜索" /></>
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
    fireEvent.keyDown(window, { key: '/' })
    expect(screen.getByRole('textbox', { name: '终端搜索' })).toHaveFocus()

    fireEvent.keyDown(screen.getByRole('textbox'), { key: '4', altKey: true })
    expect(screen.getByText(/^总览\|/)).toBeInTheDocument()
  })
})
