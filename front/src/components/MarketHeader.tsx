import { useState } from 'react'
import { marketLabels, markets, pageMeta } from '../data/dashboard'
import { formatCurrency } from '../lib/format'
import type { Market, Page } from '../types/dashboard'

export function MarketHeader({
  activePage,
  activeMarket,
  liveProfit,
  liveReturn,
  maxDrawdown,
  hasPerformanceData,
  signalCount,
  setActiveMarket,
  snapshotGeneratedAt,
  targetReturn,
  tradeSignalCount,
}: {
  activePage: Page
  activeMarket: Market
  hasPerformanceData: boolean
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
  const freshness = snapshotGeneratedAt ? '实时更新' : '预览数据'
  const returnValue = hasPerformanceData
    ? liveProfit !== null
      ? formatCurrency(liveProfit)
      : `${liveReturn >= 0 ? '+' : ''}${liveReturn.toFixed(2)}%`
    : '等待'
  const returnDetail = hasPerformanceData && liveProfit !== null
    ? `${liveReturn >= 0 ? '+' : ''}${liveReturn.toFixed(2)}%`
    : undefined

  return (
    <section className="market-header">
      <div className="market-symbol">
        <div>
          <strong>{meta.title}</strong>
        <span>模拟盘 · {marketLabels[activeMarket]} · {meta.copy}</span>
        </div>
      </div>
      <div className="market-stats">
        <Stat detail={returnDetail} label="当前收益" value={returnValue} cyan={hasPerformanceData} />
        <Stat label="目标差" value={hasPerformanceData ? `${liveReturn - targetReturn >= 0 ? '+' : ''}${(liveReturn - targetReturn).toFixed(2)}%` : '等待'} />
        <Stat detail="机会池" label="机会" value={`${signalCount}`} />
        <Stat detail="可处理" label="信号" value={`${tradeSignalCount}`} cyan />
        <Stat label="最大回撤" value={hasPerformanceData ? `-${Math.abs(maxDrawdown ?? 0).toFixed(2)}%` : '等待'} red={hasPerformanceData} />
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
