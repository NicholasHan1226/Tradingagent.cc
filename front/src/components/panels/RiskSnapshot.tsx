import type { Page } from '../../types/dashboard'
import { PanelTitle } from '../PanelTitle'

export function RiskSnapshot({ setActivePage }: { setActivePage: (page: Page) => void }) {
  return (
    <section className="panel rail-panel">
      <PanelTitle action="看风险" kicker="风险" onAction={() => setActivePage('风险')} title="边界是否安全" />
      <div className="risk-cards">
        <button className="risk-card red" onClick={() => setActivePage('风险')} type="button">
          <span>最大回撤</span>
          <strong>-6.12%</strong>
          <em>接近 -7% 限制</em>
        </button>
        <button className="risk-card cyan" onClick={() => setActivePage('风险')} type="button">
          <span>风险保护</span>
          <strong>$1.24M</strong>
          <em>避免过度暴露</em>
        </button>
      </div>
    </section>
  )
}
