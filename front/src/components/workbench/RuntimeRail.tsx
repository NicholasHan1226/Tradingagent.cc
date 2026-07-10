import { marketLabels } from '../../data/dashboard'
import type { AutomationRuntimeItem } from '../../lib/automationObservatoryViewModel'

export function RuntimeRail({
  item,
  runningCount,
}: {
  item: AutomationRuntimeItem
  runningCount: number
}) {
  return (
    <aside aria-label={item.contextLabel} className={`runtime-rail ${item.kind}`}>
      <header>
        <span>{item.contextLabel}</span>
        <b>运行中 {runningCount}</b>
      </header>
      <section>
        <small>{item.market ? marketLabels[item.market] : '全市场'} · {item.symbol ?? 'AUTO'}</small>
        <h2>{item.name}</h2>
        <dl>
          <div><dt>过程</dt><dd>{item.strategy}</dd></div>
          <div><dt>阶段</dt><dd>{item.stage}</dd></div>
          <div><dt>状态</dt><dd>{item.statusLabel}</dd></div>
          <div><dt>证据</dt><dd>{item.evidenceLabel}</dd></div>
          <div><dt>更新时间</dt><dd>{item.updatedAtLabel}</dd></div>
        </dl>
        <p>{item.detail}</p>
      </section>
    </aside>
  )
}
