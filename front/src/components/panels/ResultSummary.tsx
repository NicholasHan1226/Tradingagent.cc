import type { Page } from '../../types/dashboard'
import { PanelTitle } from '../PanelTitle'
import { SummaryRow } from '../SummaryRow'

export function ResultSummary({ setActivePage }: { setActivePage: (page: Page) => void }) {
  return (
    <section className="panel rail-panel">
      <PanelTitle action="看来源" kicker="现在该知道" onAction={() => setActivePage('决策')} title="当前结果" />
      <div className="summary-list">
        <SummaryRow label="收益主要来自" value="A股、美股趋势" tone="cyan" />
        <SummaryRow label="错过原因" value="入场条件偏严" tone="red" />
        <SummaryRow label="风险已挡住" value="$1.24M" tone="cyan" />
        <SummaryRow label="实盘状态" value="待接入" />
      </div>
      <button className="primary-action" onClick={() => setActivePage('收益')} type="button">
        收益归因
      </button>
    </section>
  )
}
