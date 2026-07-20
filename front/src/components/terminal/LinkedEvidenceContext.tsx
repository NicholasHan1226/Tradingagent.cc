import { Link2, X } from 'lucide-react'
import type { LinkedEvidenceContextModel } from '../../lib/linkedEvidenceContext'

export function LinkedEvidenceContext({ model, onClear, onOpenProcess }: { model: LinkedEvidenceContextModel; onClear: () => void; onOpenProcess: () => void }) {
  return (
    <aside aria-label="已关联机会上下文" className="linked-evidence-context">
      <span><Link2 aria-hidden="true" size={13} />{model.legacyFrozen ? '旧漏斗冻结历史' : '关联机会'}</span>
      <button className="linked-evidence-main" onClick={onOpenProcess} type="button"><strong>{model.symbol}</strong><em>{model.market}</em><small>{model.id}</small></button>
      <dl><div><dt>当前阶段</dt><dd>{model.stage}</dd></div><div><dt>结果</dt><dd>{model.result}</dd></div><div><dt>完整度</dt><dd>{model.evidence}</dd></div><div><dt>事件</dt><dd>{model.eventCount}</dd></div><div><dt>关联信号</dt><dd>{model.signalCount}</dd></div><div><dt>关联持仓</dt><dd>{model.holdingCount}</dd></div><div><dt>可归因盈亏</dt><dd className={model.attributablePnl === undefined ? undefined : model.attributablePnl >= 0 ? 'positive' : 'negative'}>{formatPnl(model.attributablePnl, model.attributablePnlCurrency)}</dd></div></dl>
      <button aria-label="清除关联机会" className="linked-evidence-clear" onClick={onClear} type="button"><X aria-hidden="true" size={14} /></button>
    </aside>
  )
}

function formatPnl(value?: number, currency?: 'CNY' | 'USDT') {
  if (value === undefined || currency === undefined) return '—'
  const formatted = Math.abs(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  return currency === 'USDT'
    ? `${value >= 0 ? '+' : '-'}${formatted} USDT`
    : `${value >= 0 ? '+' : '-'}¥${formatted}`
}
