import { marketLabels } from '../data/dashboard'
import type { HoldingRow } from '../types/dashboard'

export type HoldingExposureSummary = {
  label: string
  value: string
  detail: string
  mode: 'percent' | 'amount' | 'mixed' | 'empty'
}

export function summarizeHoldingExposure(holdings: HoldingRow[]): HoldingExposureSummary {
  const exposures = holdings.map((holding) => ({
    ...parseHoldingExposure(holding.weight),
    currency: holding.currency ?? (holding.market === 'Crypto' ? 'USDT' : 'CNY'),
    market: holding.market,
  }))
  if (new Set(exposures.map((exposure) => exposure.market)).size > 1) {
    return {
      label: '持仓账户',
      value: '多账户',
      detail: '不同市场账户不可汇总',
      mode: 'mixed',
    }
  }
  const scopeState = holdingScopeState(holdings)
  if (scopeState === 'missing') {
    return {
      label: '持仓账户',
      value: '范围不可用',
      detail: '缺少明确账户范围，禁止汇总',
      mode: 'mixed',
    }
  }
  if (scopeState === 'multiple') {
    return {
      label: '持仓账户',
      value: '多账户',
      detail: '同一市场不同账户不可汇总',
      mode: 'mixed',
    }
  }
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
    const currencies = new Set(amountValues.map((exposure) => exposure.currency))
    if (currencies.size !== 1) {
      return {
        label: '持仓金额',
        value: '多币种',
        detail: '不同币种金额不可汇总',
        mode: 'mixed',
      }
    }
    const [currency] = [...currencies]
    if (currency !== 'CNY' && currency !== 'USDT') {
      return {
        label: '持仓金额',
        value: '币种不可用',
        detail: '当前市场仅支持 CNY 或 USDT',
        mode: 'mixed',
      }
    }
    const total = amountValues.reduce((sum, exposure) => sum + exposure.value, 0)
    return {
      label: '持仓金额',
      value: formatCompactCurrency(total, currency),
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
      currency: holding.currency ?? (holding.market === 'Crypto' ? 'USDT' : 'CNY'),
    }))
    .filter((row) => row.exposure.value > 0)

  if (!exposures.length) return [{ name: '等待持仓', value: 100 }]
  if (new Set(holdings.map((holding) => holding.market)).size > 1) {
    return [{ name: '多账户不可汇总', value: 100 }]
  }
  const scopeState = holdingScopeState(holdings)
  if (scopeState === 'missing') return [{ name: '账户范围不可用', value: 100 }]
  if (scopeState === 'multiple') return [{ name: '多账户不可汇总', value: 100 }]

  const amountRows = exposures.filter((row) => row.exposure.kind === 'amount')
  const percentRows = exposures.filter((row) => row.exposure.kind === 'percent')
  if (amountRows.length && percentRows.length) return [{ name: '记录待统一', value: 100 }]
  if (amountRows.length && new Set(amountRows.map((row) => row.currency)).size > 1) {
    return [{ name: '多币种不可汇总', value: 100 }]
  }
  if (amountRows.length && !amountRows.every((row) => row.currency === 'CNY' || row.currency === 'USDT')) {
    return [{ name: '币种不可用', value: 100 }]
  }

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

function holdingScopeState(holdings: HoldingRow[]): 'single' | 'multiple' | 'missing' {
  const scopes = holdings.map((holding) => holding.accountScope?.trim()).filter(Boolean) as string[]
  if (scopes.length !== holdings.length) return 'missing'
  return new Set(scopes).size > 1 ? 'multiple' : 'single'
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

function formatCompactCurrency(value: number, currency: NonNullable<HoldingRow['currency']>) {
  if (currency === 'USDT') {
    return `${value.toLocaleString('en-US', {
      maximumFractionDigits: value >= 1000 ? 0 : 2,
      notation: value >= 1_000_000 ? 'compact' : 'standard',
    })} USDT`
  }
  if (currency !== 'CNY') return '币种不可用'
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency,
    maximumFractionDigits: value >= 1000 ? 0 : 2,
    notation: value >= 1_000_000 ? 'compact' : 'standard',
  }).format(value)
}
