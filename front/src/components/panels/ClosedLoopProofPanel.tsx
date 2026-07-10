import type { MarketSummary } from '../../types/dashboard'
import { formatRuntimeReason } from '../../lib/workbenchViewModel'
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
      <PanelTitle kicker="市场概览" title="各市场状态" />
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
            <strong>暂无市场记录</strong>
            <span>有新信号、成交或持仓后会在这里出现。</span>
          </div>
        )}
      </div>
    </section>
  )
}

function formatState(summary: MarketSummary) {
  if (summary.executionFault || summary.runtimeState === 'needs_attention') return '需要处理'
  if (summary.runtimeState === 'normal') return '状态正常'
  if (summary.runtimeState === 'strategy_wait') return '等待机会'
  return '等待数据'
}

function formatEvidence(summary: MarketSummary) {
  if (summary.noTradeEvidence) {
    const evidence = summary.noTradeEvidence
    const status = evidence.evidenceStatus === 'ready' ? '已说明原因' : evidence.evidenceStatus === 'incomplete' ? '原因不完整' : '暂无原因'
    const candidates = evidence.candidateCount === undefined ? '-' : String(evidence.candidateCount)
    const orders = evidence.orderCount === undefined ? '-' : String(evidence.orderCount)
    return `${status} · 机会 ${candidates} · 结果 ${orders}`
  }
  if (summary.tradeCount > 0) return `成交 ${summary.tradeCount} · 可复盘`
  if (summary.holdingCount > 0) return `持仓 ${summary.holdingCount} · 持续盯市`
  if (summary.signalCount > 0) return `信号 ${summary.signalCount} · 等待结果`
  return summary.runtimeReason ? formatRuntimeReason(summary.runtimeReason) : '暂无新机会'
}

function formatCounts(summary: MarketSummary) {
  if (!summary.signalCount && !summary.tradeCount && !summary.holdingCount) return ''
  return `信号 ${summary.signalCount} · 成交 ${summary.tradeCount} · 持仓 ${summary.holdingCount}`
}
