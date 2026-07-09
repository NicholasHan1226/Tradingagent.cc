import { useState } from 'react'
import { marketLabels, markets, pageMeta } from '../data/dashboard'
import { formatCurrency, formatSignedCnyCompact } from '../lib/format'
import type { AccountMode, Market, Page } from '../types/dashboard'

export function MarketHeader({
  accountMode,
  activePage,
  activeMarket,
  liveProfit,
  liveReturn,
  maxDrawdown,
  hasPerformanceData,
  isDemoPreview,
  isCnyAccount,
  signalCount,
  setActiveMarket,
  snapshotGeneratedAt,
  targetReturn,
  tradeSignalCount,
}: {
  accountMode: AccountMode
  activePage: Page
  activeMarket: Market
  hasPerformanceData: boolean
  isDemoPreview: boolean
  isCnyAccount: boolean
  liveProfit: number | null
  liveReturn: number
  maxDrawdown: number | null
  signalCount: number
  setActiveMarket: (market: Market) => void
  snapshotGeneratedAt: string | null
  targetReturn: number
  tradeSignalCount: number
}) {
  const meta = pageMeta[activePage]
  const [showMarkets, setShowMarkets] = useState(false)
  const freshness = snapshotGeneratedAt ? '最新快照' : isDemoPreview ? '演示数据' : '等待接口'
  const accountLabel = accountMode === 'live' ? '实盘待接入' : '模拟盘'
  const returnValue = hasPerformanceData
    ? liveProfit !== null
      ? isCnyAccount ? formatSignedCnyCompact(liveProfit) : formatCurrency(liveProfit)
      : `${liveReturn >= 0 ? '+' : ''}${liveReturn.toFixed(2)}%`
    : '等待'
  const returnDetail = hasPerformanceData && liveProfit !== null
    ? formatSignedPct(liveReturn)
    : undefined
  const drawdown = Math.abs(maxDrawdown ?? 0)
  const returnTone = getTone(liveProfit ?? liveReturn, liveReturn)

  return (
    <section className="market-header">
      <div className="market-symbol">
        <div>
          <strong>{meta.title}</strong>
        <span>{accountLabel} · {marketLabels[activeMarket]} · {meta.copy}</span>
        </div>
      </div>
      <div className="market-stats">
        <Stat
          detail={returnDetail}
          label="当前收益"
          value={returnValue}
          cyan={hasPerformanceData && returnTone === 'positive'}
          red={hasPerformanceData && returnTone === 'negative'}
        />
        <Stat
          label="目标差"
          value={hasPerformanceData ? formatSignedPct(liveReturn - targetReturn) : '等待'}
          cyan={hasPerformanceData && liveReturn - targetReturn > 0.005}
          red={hasPerformanceData && liveReturn - targetReturn < -0.005}
        />
        <Stat detail="机会池" label="机会" value={`${signalCount}`} />
        <Stat detail="通过筛选" label="可跟进" value={`${tradeSignalCount}`} cyan />
        <Stat label="最大回撤" value={hasPerformanceData ? formatDrawdown(drawdown) : '等待'} red={hasPerformanceData && drawdown > 0} />
      </div>
      <div className="market-tools">
        <span className="market-freshness"><i />{freshness}</span>
        <div className="market-filter">
          <button
            aria-expanded={showMarkets}
            aria-haspopup="menu"
            className="market-filter-trigger"
            onClick={() => setShowMarkets((shown) => !shown)}
            type="button"
          >
            {marketLabels[activeMarket]}
          </button>
          {showMarkets && (
            <div className="market-menu" role="menu">
              {markets.map((market) => (
                <button
                  aria-current={activeMarket === market ? 'true' : undefined}
                  className={activeMarket === market ? 'selected' : ''}
                  key={market}
                  onClick={() => {
                    setActiveMarket(market)
                    setShowMarkets(false)
                  }}
                  role="menuitem"
                  type="button"
                >
                  <span>{marketLabels[market]}</span>
                  {activeMarket === market && <em>当前</em>}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </section>
  )
}

function formatSignedPct(value: number) {
  const cleanValue = Math.abs(value) < 0.005 ? 0 : value
  return `${cleanValue > 0 ? '+' : ''}${cleanValue.toFixed(2)}%`
}

function formatDrawdown(value: number) {
  const cleanValue = Math.abs(value) < 0.005 ? 0 : value
  return cleanValue === 0 ? '0.00%' : `-${cleanValue.toFixed(2)}%`
}

function getTone(amount: number, pct: number) {
  if (amount < -0.005 || pct < -0.005) {
    return 'negative'
  }
  if (amount > 0.005 || pct > 0.005) {
    return 'positive'
  }
  return 'flat'
}

function Stat({
  label,
  value,
  detail,
  cyan = false,
  red = false,
}: {
  detail?: string
  label: string
  value: string
  cyan?: boolean
  red?: boolean
}) {
  return (
    <span className={`${cyan ? 'cyan' : ''} ${red ? 'red' : ''}`}>
      <em>{label}</em>
      <strong>{value}</strong>
      {detail && <b>{detail}</b>}
    </span>
  )
}
