import { useEffect } from 'react'
import { markets, pages } from '../data/dashboard'
import type { Market, Page, PerformanceRange } from '../types/dashboard'

const RANGES: PerformanceRange[] = ['today', '7d', '30d', 'all']

type NavigationState = { page: Page; market: Market; range: PerformanceRange }
type NavigationSetters = {
  setPage: (page: Page) => void
  setMarket: (market: Market) => void
  setRange: (range: PerformanceRange) => void
}

export function readTerminalNavigation(search = window.location.search): NavigationState {
  const params = new URLSearchParams(search)
  const page = params.get('page') as Page
  const market = params.get('market') as Market
  const range = params.get('range') as PerformanceRange
  return {
    page: pages.includes(page) ? page : '总览',
    market: markets.includes(market) ? market : 'All Markets',
    range: RANGES.includes(range) ? range : 'all',
  }
}

export function useTerminalNavigation({ page, market, range, setPage, setMarket, setRange }: NavigationState & NavigationSetters) {
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const current = readTerminalNavigation(params.toString())
    if (current.page === page && current.market === market && current.range === range) return
    params.set('page', page)
    params.set('market', market)
    params.set('range', range)
    window.history.pushState(window.history.state, '', `${window.location.pathname}?${params.toString()}${window.location.hash}`)
  }, [market, page, range])

  useEffect(() => {
    const restore = () => {
      const state = readTerminalNavigation()
      setPage(state.page)
      setMarket(state.market)
      setRange(state.range)
    }
    window.addEventListener('popstate', restore)
    return () => window.removeEventListener('popstate', restore)
  }, [setMarket, setPage, setRange])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (isEditable(event.target)) return
      if (event.altKey && /^[1-6]$/.test(event.key)) {
        event.preventDefault()
        setPage(pages[Number(event.key) - 1])
        return
      }
      if (event.altKey && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) {
        event.preventDefault()
        const current = Math.max(0, markets.indexOf(market))
        const offset = event.key === 'ArrowRight' ? 1 : -1
        setMarket(markets[(current + offset + markets.length) % markets.length])
        return
      }
    }
    const onKeyUp = (event: KeyboardEvent) => {
      if (isEditable(event.target) || event.altKey || event.key !== '/') return
      const search = document.querySelector<HTMLInputElement>('[data-terminal-search]')
      if (!search) return
      event.preventDefault()
      search.focus()
    }
    window.addEventListener('keydown', onKeyDown)
    window.addEventListener('keyup', onKeyUp)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('keyup', onKeyUp)
    }
  }, [market, setMarket, setPage])
}

function isEditable(target: EventTarget | null) {
  if (!(target instanceof HTMLElement)) return false
  return target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)
}
