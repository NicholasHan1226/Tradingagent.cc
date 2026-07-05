import { marketLabels } from '../data/dashboard'
import type { HoldingRow } from '../types/dashboard'

export type HoldingExposureSummary = {
  label: string
  value: string
  detail: string
  mode: 'percent' | 'amount' | 'mixed' | 'empty'
}

export function summarizeHoldingExposure(holdings: HoldingRow[]): HoldingExposureSummary {
  const exposures = holdings.map((holding) => parseHoldingExposure(holding.weight))
  const percentValues = exposures.filter((exposure) => exposure.kind === 'percent')
  const amountValues = exposures.filter((exposure) => exposure.kind === 'amount')

  if (percentValues.length && amountValues.length) {
    return {
      label: '持仓记录',
      value: String(holdings.length),
      detail: '金额/权重待统一',
      mode: 'mixed',
    }
  }

  if (percentValues.length) {
    const total = percentValues.reduce((sum, exposure) => sum + exposure.value, 0)
    return {
      label: '总仓位',
      value: `${total.toFixed(1)}%`,
      detail: '已记录权重',
      mode: 'percent',
    }
  }

  if (amountValues.length) {
    const total = amountValues.reduce((sum, exposure) => sum + exposure.value, 0)
    return {
      label: '持仓金额',
      value: formatCompactCurrency(total),
      detail: `${amountValues.length} 个持仓有金额`,
      mode: 'amount',
    }
  }

  return {
    label: '持仓规模',
    value: '--',
    detail: '等待记录',
    mode: 'empty',
  }
}

export function getMarketAllocation(holdings: HoldingRow[]) {
  const exposures = holdings
    .map((holding) => ({
      market: marketLabels[holding.market],
      exposure: parseHoldingExposure(holding.weight),
    }))
    .filter((row) => row.exposure.value > 0)

  if (!exposures.length) return [{ name: '等待持仓', value: 100 }]

  const amountRows = exposures.filter((row) => row.exposure.kind === 'amount')
  const percentRows = exposures.filter((row) => row.exposure.kind === 'percent')
  if (amountRows.length && percentRows.length) return [{ name: '记录待统一', value: 100 }]

  const rows = amountRows.length ? amountRows : percentRows
  const total = rows.reduce((sum, row) => sum + row.exposure.value, 0)
  if (total <= 0) return [{ name: '等待持仓', value: 100 }]

  const byMarket = rows.reduce<Record<string, number>>((acc, row) => {
    const share = (row.exposure.value / total) * 100
    acc[row.market] = (acc[row.market] ?? 0) + share
    return acc
  }, {})

  return Object.entries(byMarket)
    .map(([name, value]) => ({ name, value: Number(value.toFixed(1)) }))
    .sort((a, b) => b.value - a.value)
}

export function parseHoldingExposure(value: string): { kind: 'amount' | 'percent' | 'empty'; value: number } {
  const text = value.trim()
  if (!text || text === '--') return { kind: 'empty', value: 0 }

  if (text.includes('%')) {
    return { kind: 'percent', value: parseNumeric(text) }
  }

  if (/\$|¥|￥|CNY|RMB|USDC|USDT/i.test(text) || /[kKmM]$/.test(text.replace(/\s/g, ''))) {
    return { kind: 'amount', value: parseNumeric(text) }
  }

  const parsed = parseNumeric(text)
  return { kind: Number.isFinite(parsed) && parsed > 100 ? 'amount' : 'percent', value: parsed }
}

function parseNumeric(value: string) {
  const normalized = value.replace(/[$¥￥,%+\s]/g, '').replace(/CNY|RMB|USDC|USDT/gi, '').replace(/,/g, '')
  const multiplier = /k$/i.test(normalized) ? 1_000 : /m$/i.test(normalized) ? 1_000_000 : 1
  const numeric = Number(normalized.replace(/[kKmM]$/, ''))
  return Number.isFinite(numeric) ? numeric * multiplier : 0
}

function formatCompactCurrency(value: number) {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits: value >= 1000 ? 0 : 2,
    notation: value >= 1_000_000 ? 'compact' : 'standard',
  }).format(value)
}
