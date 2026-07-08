import type { MarketSummary } from '../../types/dashboard'
import { PanelTitle } from '../PanelTitle'

const MARKET_LABELS: Record<string, string> = {
  'A-share': 'A股',
  US: '美股',
  Crypto: '加密',
  PM: '预测',
  CNFutures: '期货',
}

export function ClosedLoopProofPanel({ summaries }: { summaries: MarketSummary[] }) {
  const rows = summaries.filter((summary) => summary.market !== 'HK')

  return (
    <section className="panel rail-panel closure-proof-panel" aria-label="市场运行状态">
      <PanelTitle kicker="运行证据" title="市场状态" />
      <div className="closure-proof-list">
        {rows.length ? rows.map((summary) => {
          const counts = formatCounts(summary)

          return (
            <div className={`closure-proof-row ${summary.runtimeState ?? 'empty'}`} key={summary.market}>
              <div>
                <span>{MARKET_LABELS[summary.market] ?? summary.market}</span>
                <strong>{formatState(summary)}</strong>
                <em>{formatEvidence(summary)}</em>
              </div>
              {counts && <b>{counts}</b>}
            </div>
          )
        }) : (
          <div className="empty-panel-copy compact-copy">
            <strong>等待运行证据</strong>
            <span>模拟盘写入后会显示信号、成交和归因。</span>
          </div>
        )}
      </div>
    </section>
  )
}

function formatState(summary: MarketSummary) {
  if (summary.executionFault || summary.runtimeState === 'needs_attention') return '需要处理'
  if (summary.runtimeState === 'normal') return '闭环中'
  if (summary.runtimeState === 'strategy_wait') return '等待机会'
  return '等待数据'
}

function formatEvidence(summary: MarketSummary) {
  if (summary.noTradeEvidence) {
    const evidence = summary.noTradeEvidence
    const status = evidence.evidenceStatus === 'ready' ? '证据完整' : evidence.evidenceStatus === 'incomplete' ? '证据缺口' : '无记录'
    const candidates = evidence.candidateCount === undefined ? '-' : String(evidence.candidateCount)
    const orders = evidence.orderCount === undefined ? '-' : String(evidence.orderCount)
    return `${status} · 候选 ${candidates} · 订单 ${orders}`
  }
  if (summary.tradeCount > 0) return `成交 ${summary.tradeCount} · 可复盘`
  if (summary.holdingCount > 0) return `持仓 ${summary.holdingCount} · 持续盯市`
  if (summary.signalCount > 0) return `信号 ${summary.signalCount} · 等待结果`
  return summary.runtimeReason ? normalizeReason(summary.runtimeReason) : '暂无新机会'
}

function formatCounts(summary: MarketSummary) {
  if (!summary.signalCount && !summary.tradeCount && !summary.holdingCount) return ''
  return `信号 ${summary.signalCount} · 成交 ${summary.tradeCount} · 持仓 ${summary.holdingCount}`
}

function normalizeReason(reason: string) {
  const cleanReason = reason.toLowerCase().replaceAll('_', ' ').trim()
  const marketPrefix = cleanReason.startsWith('crypto ')
    ? '加密市场'
    : cleanReason.startsWith('pm ')
      ? '预测市场'
      : cleanReason.startsWith('us ')
        ? '美股'
        : ''
  const body = cleanReason
    .replace(/^crypto\s+/, '')
    .replace(/^pm\s+/, '')
    .replace(/^us\s+/, '')

  if (body.includes('waiting momentum signal')) return `${marketPrefix || '当前市场'}等待动量信号`
  if (body.includes('waiting model edge')) return `${marketPrefix || '当前市场'}等待模型优势`
  if (body.includes('waiting for market data')) return `${marketPrefix || '当前市场'}等待行情`
  if (body.includes('waiting')) return `${marketPrefix || '当前市场'}等待机会`

  return reason
    .replaceAll('_', ' ')
    .replace('waiting for', '等待')
    .replace('market data', '行情')
}
