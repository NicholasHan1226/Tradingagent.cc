import { useState } from 'react'
import { marketLabels, markets, pageMeta } from '../data/dashboard'
import type { Market, Page } from '../types/dashboard'

export function MarketHeader({
  activePage,
  activeMarket,
  liveReturn,
  signalCount,
  setActiveMarket,
  targetReturn,
  tradeSignalCount,
}: {
  activePage: Page
  activeMarket: Market
  liveReturn: number
  signalCount: number
  setActiveMarket: (market: Market) => void
  targetReturn: number
  tradeSignalCount: number
}) {
  const meta = pageMeta[activePage]
  const [showMarkets, setShowMarkets] = useState(false)

  return (
    <section className="market-header">
      <div className="market-symbol">
        <div>
          <strong>{meta.title}</strong>
          <span>模拟盘 · {marketLabels[activeMarket]} · {meta.copy}</span>
        </div>
      </div>
      <div className="market-stats">
        <Stat label="目标差" value={`${liveReturn - targetReturn >= 0 ? '+' : ''}${(liveReturn - targetReturn).toFixed(2)}%`} />
        <Stat detail="已收录" label="机会" value={`${signalCount}`} />
        <Stat detail="可处理" label="交易信号" value={`${tradeSignalCount}`} cyan />
        <Stat label="最大回撤" value="-6.12%" red />
      </div>
      <div className="market-tools">
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
