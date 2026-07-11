import { Link2, X } from 'lucide-react'
import type { LinkedEvidenceContextModel } from '../../lib/linkedEvidenceContext'

export function LinkedEvidenceContext({ model, onClear, onOpenProcess }: { model: LinkedEvidenceContextModel; onClear: () => void; onOpenProcess: () => void }) {
  return (
    <aside aria-label="已关联机会上下文" className="linked-evidence-context">
      <span><Link2 aria-hidden="true" size={13} />关联机会</span>
      <button className="linked-evidence-main" onClick={onOpenProcess} type="button"><strong>{model.symbol}</strong><em>{model.market}</em><small>{model.id}</small></button>
      <dl><div><dt>当前阶段</dt><dd>{model.stage}</dd></div><div><dt>结果</dt><dd>{model.result}</dd></div><div><dt>完整度</dt><dd>{model.evidence}</dd></div><div><dt>事件</dt><dd>{model.eventCount}</dd></div><div><dt>更新</dt><dd>{model.updatedAt}</dd></div></dl>
      <button aria-label="清除关联机会" className="linked-evidence-clear" onClick={onClear} type="button"><X aria-hidden="true" size={14} /></button>
    </aside>
  )
}
