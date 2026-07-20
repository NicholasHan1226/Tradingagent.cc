import { describe, expect, it } from 'vitest'
import { getMarketAllocation, summarizeHoldingExposure } from './holdings'
import type { HoldingRow } from '../types/dashboard'

const baseHolding: HoldingRow = {
  symbol: '600519.SH',
  name: '贵州茅台',
  market: 'A-share',
  weight: '10%',
  pnl: '+¥10',
  risk: '正常',
  role: '测试持仓',
  accountScope: 'test-account',
}

describe('holding exposure helpers', () => {
  it('keeps percentage holdings as total weight', () => {
    const summary = summarizeHoldingExposure([
      { ...baseHolding, weight: '12.8%' },
      { ...baseHolding, symbol: '000001.SZ', weight: '10.6%' },
    ])

    expect(summary).toMatchObject({
      label: '总仓位',
      value: '23.4%',
      mode: 'percent',
    })
  })

  it('fails closed for the retired USD holding currency', () => {
    const summary = summarizeHoldingExposure([
      { ...baseHolding, weight: '$1,200', currency: 'USD' },
      { ...baseHolding, symbol: '000001.SZ', weight: '$800', currency: 'USD' },
    ])

    expect(summary).toMatchObject({
      label: '持仓金额',
      value: '币种不可用',
      mode: 'mixed',
    })
  })

  it('does not allocate a retired USD holding book', () => {
    const allocation = getMarketAllocation([
      { ...baseHolding, weight: '$1,200', currency: 'USD' },
      { ...baseHolding, symbol: '000001.SZ', weight: '$800', currency: 'USD' },
    ])

    expect(allocation).toEqual([{ name: '币种不可用', value: 100 }])
  })

  it('does not pretend mixed amount and percent holdings share one allocation scale', () => {
    const summary = summarizeHoldingExposure([
      { ...baseHolding, weight: '¥1,200', currency: 'CNY' },
      { ...baseHolding, symbol: '000001.SZ', weight: '10%' },
    ])
    const allocation = getMarketAllocation([
      { ...baseHolding, weight: '¥1,200', currency: 'CNY' },
      { ...baseHolding, symbol: '000001.SZ', weight: '10%' },
    ])

    expect(summary).toMatchObject({
      label: '持仓记录',
      value: '2',
      mode: 'mixed',
    })
    expect(allocation).toEqual([{ name: '记录待统一', value: 100 }])
  })

  it('keeps Crypto holdings in native USDT and refuses CNY plus USDT aggregation', () => {
    const crypto = [
      { ...baseHolding, symbol: 'BTC-USDT', market: 'Crypto' as const, weight: '1,200 USDT', currency: 'USDT' as const },
      { ...baseHolding, symbol: 'ETH-USDT', market: 'Crypto' as const, weight: '800 USDT', currency: 'USDT' as const },
    ]
    expect(summarizeHoldingExposure(crypto)).toMatchObject({
      label: '持仓金额',
      value: '2,000 USDT',
      mode: 'amount',
    })

    const mixed = [{ ...baseHolding, weight: '¥1,000', currency: 'CNY' as const }, crypto[0]]
    expect(summarizeHoldingExposure(mixed)).toMatchObject({
      value: '多账户',
      detail: '不同市场账户不可汇总',
      mode: 'mixed',
    })
    expect(getMarketAllocation(mixed)).toEqual([{ name: '多账户不可汇总', value: 100 }])

    const sameMarketMixedCurrency = [crypto[0], { ...crypto[1], currency: 'USD' as const, weight: '$800' }]
    expect(summarizeHoldingExposure(sameMarketMixedCurrency)).toMatchObject({
      value: '多币种',
      detail: '不同币种金额不可汇总',
      mode: 'mixed',
    })
    expect(getMarketAllocation(sameMarketMixedCurrency)).toEqual([{ name: '多币种不可汇总', value: 100 }])
  })

  it('does not aggregate multiple accounts inside one Crypto market', () => {
    const multiAccount = [
      { ...baseHolding, symbol: 'BTC-USDT', market: 'Crypto' as const, weight: '1,200 USDT', currency: 'USDT' as const, accountScope: 'crypto:grid' },
      { ...baseHolding, symbol: 'ETH-USDT', market: 'Crypto' as const, weight: '800 USDT', currency: 'USDT' as const, accountScope: 'crypto:momentum' },
    ]
    expect(summarizeHoldingExposure(multiAccount)).toMatchObject({
      value: '多账户',
      detail: '同一市场不同账户不可汇总',
      mode: 'mixed',
    })
    expect(getMarketAllocation(multiAccount)).toEqual([{ name: '多账户不可汇总', value: 100 }])

    const missingScope = multiAccount.map(({ accountScope: _accountScope, ...holding }) => holding)
    expect(summarizeHoldingExposure(missingScope)).toMatchObject({ value: '范围不可用', mode: 'mixed' })
    expect(getMarketAllocation(missingScope)).toEqual([{ name: '账户范围不可用', value: 100 }])
  })
})
