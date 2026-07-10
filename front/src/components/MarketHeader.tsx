import { useState } from 'react'
import { marketLabels, markets, pageMeta } from '../data/dashboard'
import { formatCurrency, formatSignedCnyCompact } from '../lib/format'
import type { AccountMode, Market, Page } from '../types/dashboard'
import type { DomainStatus } from '../types/status'

export function MarketHeader({
  accountMode,
  activeOpportunityCount,
  activePage,
  activeMarket,
  liveProfit,
  liveReturn,
  maxDrawdown,
  positionCount,
  performanceStatus,
  hasPerformanceData,
  isDemoPreview,
  isCnyAccount,
  reviewCount,
  setActiveMarket,
  snapshotGeneratedAt,
  targetReturn,
}: {
  accountMode: AccountMode
  activeOpportunityCount: number
  activePage: Page
  activeMarket: Market
  hasPerformanceData: boolean
  isDemoPreview: boolean
  isCnyAccount: boolean
  liveProfit: number | null
  liveReturn: number
  maxDrawdown: number | null
  positionCount: number
  performanceStatus: DomainStatus
  reviewCount: number
  setActiveMarket: (market: Market) => void
  snapshotGeneratedAt: string | null
  targetReturn: number
}) {
  const meta = pageMeta[activePage]
  const [showMarkets, setShowMarkets] = useState(false)
  const freshness = performanceStatus === 'stale'
    ? '快照滞后'
    : snapshotGeneratedAt
      ? `快照 ${formatSnapshotTime(snapshotGeneratedAt)}`
      : isDemoPreview ? '演示数据' : '等待接口'
  const isLive = accountMode === 'live'
  const showPerformanceData = hasPerformanceData && !isLive
  const accountLabel = isLive ? '实盘待接入' : '模拟盘'
  const returnValue = showPerformanceData
    ? liveProfit !== null
      ? isCnyAccount ? formatSignedCnyCompact(liveProfit) : formatCurrency(liveProfit)
      : `${liveReturn >= 0 ? '+' : ''}${liveReturn.toFixed(2)}%`
    : isLive ? '待接入' : '等待'
  const returnDetail = showPerformanceData && liveProfit !== null
    ? formatSignedPct(liveReturn)
    : undefined
  const drawdown = Math.abs(maxDrawdown ?? 0)
  const returnTone = getTone(liveProfit ?? liveReturn, liveReturn)

  return (
    <section className="market-header" aria-label="市场概览">
      <div className="market-symbol">
        <div>
          <strong>{meta.title}</strong>
          <span>{accountLabel} · {marketLabels[activeMarket]} · {isLive ? <b>模拟盘参考</b> : meta.copy}</span>
        </div>
      </div>
      <div className="market-stats">
        <Stat
          detail={returnDetail}
          label="当前收益"
          value={returnValue}
          cyan={showPerformanceData && returnTone === 'positive'}
          red={showPerformanceData && returnTone === 'negative'}
        />
        <Stat
          label="目标差"
          value={showPerformanceData ? formatSignedPct(liveReturn - targetReturn) : isLive ? '待接入' : '等待'}
          cyan={showPerformanceData && liveReturn - targetReturn > 0.005}
          red={showPerformanceData && liveReturn - targetReturn < -0.005}
        />
        <Stat detail="等待处理" label="当前机会" value={`${activeOpportunityCount}`} cyan={activeOpportunityCount > 0} />
        <Stat detail="风险与错过" label="待复盘" value={`${reviewCount}`} red={reviewCount > 0} />
        <Stat detail="模拟盘" label="持仓" value={`${positionCount}`} />
        <Stat label="最大回撤" value={showPerformanceData ? formatDrawdown(drawdown) : isLive ? '待接入' : '等待'} red={showPerformanceData && drawdown > 0} />
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

function formatSnapshotTime(value: string) {
  const timestamp = new Date(value)
  if (Number.isNaN(timestamp.getTime())) return '时间未知'
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
    timeZone: 'Asia/Shanghai',
  }).format(timestamp)
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
