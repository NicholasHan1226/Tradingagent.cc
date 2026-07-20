import { getActionableSignals, getClosedSignals, getPortfolioForView, getVisibleHoldings, getVisibleSignals } from './dashboard'
import type {
  AccountMode,
  FunnelEvent,
  HoldingRow,
  Market,
  MarketSummary,
  PerformancePoint,
  PortfolioSummary,
  SignalRow,
} from '../types/dashboard'

export type WorkbenchViewModel = {
  accountMode: AccountMode
  market: Market
  portfolio: PortfolioSummary | null
  performance: PerformancePoint[]
  headline: {
    pnlAmount: number | null
    returnPct: number
    targetPct: number
    targetGapPct: number
    maxDrawdownPct: number | null
    capitalBase: number | null
    generatedAt: string | null
  }
  opportunities: {
    active: SignalRow[]
    completed: SignalRow[]
  }
  positions: HoldingRow[]
  funnelEvents: FunnelEvent[]
  reviewItems: SignalRow[]
  liveGate: {
    gated: boolean
    title: string
    detail: string
  }
}

export function createWorkbenchViewModel({
  accountMode,
  activeMarket,
  performance,
  portfolio,
  marketSummaries,
  signals,
  holdings,
  funnelEvents,
  generatedAt,
}: {
  accountMode: AccountMode
  activeMarket: Market
  performance: PerformancePoint[]
  portfolio: PortfolioSummary | null
  marketSummaries: MarketSummary[]
  signals: SignalRow[]
  holdings: HoldingRow[]
  funnelEvents: FunnelEvent[]
  generatedAt: string | null
}): WorkbenchViewModel {
  const selectedPortfolio = getPortfolioForView({ activeMarket, marketSummaries, portfolio })
  const visibleSignals = getVisibleSignals(signals, activeMarket)
  const positions = getVisibleHoldings(holdings, activeMarket)
  const visibleFunnelEvents = funnelEvents.filter((event) => activeMarket === 'All Markets' || event.market === activeMarket)
  const selectedPerformance = alignPerformanceWithPortfolio(performance, selectedPortfolio)
  const returnPct = selectedPortfolio?.returnPct ?? selectedPerformance.at(-1)?.simulated ?? 0
  const targetPct = selectedPortfolio?.targetPct ?? selectedPerformance.at(-1)?.target ?? 0

  return {
    accountMode,
    market: activeMarket,
    portfolio: selectedPortfolio,
    performance: selectedPerformance,
    headline: {
      pnlAmount: selectedPortfolio?.pnlAmount ?? null,
      returnPct,
      targetPct,
      targetGapPct: returnPct - targetPct,
      maxDrawdownPct: selectedPortfolio?.maxDrawdownPct ?? null,
      capitalBase: selectedPortfolio?.capitalBase ?? null,
      generatedAt,
    },
    opportunities: {
      active: getActionableSignals(visibleSignals),
      completed: getClosedSignals(visibleSignals),
    },
    positions,
    funnelEvents: visibleFunnelEvents,
    reviewItems: visibleSignals.filter((signal) => signal.status === 'blocked' || signal.status === 'missed' || signal.status === 'cancelled'),
    liveGate: accountMode === 'live'
      ? {
          gated: true,
          title: '实盘待接入',
          detail: '完成账户授权、风险校验和成交回执确认后，再展示真实资金结果。',
        }
      : {
          gated: false,
          title: '模拟盘运行中',
          detail: '当前结果来自只读模拟盘快照。',
        },
  }
}

export function formatRuntimeReason(reason?: string) {
  if (!reason) return '等待更多市场信息'
  const normalized = reason.toLowerCase().trim()
  const exact: Record<string, string> = {
    market_data_missing: '等待行情数据',
    futures_market_data_not_ready: '期货行情尚未就绪',
    crypto_waiting_for_market_data: '加密市场等待行情',
  }
  if (exact[normalized]) return exact[normalized]
  if (normalized.includes('waiting momentum signal')) return '等待动量信号'
  if (normalized.includes('waiting model edge')) return '等待模型优势'
  if (normalized.includes('waiting for market data')) return '等待行情数据'
  if (normalized.includes('waiting')) return '等待更合适的机会'
  if (normalized.includes('_')) return '等待更多市场信息'
  return reason
}

function alignPerformanceWithPortfolio(performance: PerformancePoint[], portfolio: PortfolioSummary | null) {
  if (!performance.length) {
    if (!portfolio) return []
    return [{
      day: '现在',
      simulated: portfolio.returnPct,
      target: portfolio.targetPct,
      benchmark: 0,
      opportunity: -Math.abs(portfolio.maxDrawdownPct),
    }]
  }
  if (!portfolio) return performance
  return performance.map((point, index) => index === performance.length - 1
    ? { ...point, simulated: portfolio.returnPct, target: portfolio.targetPct }
    : point)
}
