import type { Page } from '../../types/dashboard'
import { PanelTitle } from '../PanelTitle'
import { SummaryRow } from '../SummaryRow'

export function HomeResultBrief({ setActivePage }: { setActivePage: (page: Page) => void }) {
  return (
    <section className="panel rail-panel home-result-brief">
      <PanelTitle action="查看来源" kicker="今日重点" onAction={() => setActivePage('决策')} title="今日操作" />
      <div className="home-brief-section brief-result-section">
        <span className="section-label">现在该看什么</span>
        <div className="summary-list">
          <SummaryRow label="主要贡献" value="A股、美股趋势" tone="cyan" />
          <SummaryRow label="需要复盘" value="入场条件偏严" tone="red" />
          <SummaryRow label="风险节省" value="$1.24M" tone="cyan" />
          <SummaryRow label="实盘状态" value="等待接入" />
        </div>
      </div>
      <div className="home-brief-section brief-action-section">
        <span className="section-label">优先级</span>
        <div className="decision-list compact">
          <button onClick={() => setActivePage('机会')} type="button">
            <span>优先跟进</span>
            <strong>腾讯 0700.HK</strong>
            <em>价格和成交量接近走强</em>
          </button>
          <button onClick={() => setActivePage('风险')} type="button">
            <span>先别追</span>
            <strong>BTC-USD</strong>
            <em>波动太大，等风险降下来</em>
          </button>
          <button onClick={() => setActivePage('复盘')} type="button">
            <span>复盘对象</span>
            <strong>HYPE-PERP</strong>
            <em>入场条件过严，窗口已过</em>
          </button>
        </div>
      </div>
      <div className="home-brief-section brief-risk-section">
        <span className="section-label">风险边界</span>
        <div className="risk-cards compact">
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
      </div>
      <button className="primary-action" onClick={() => setActivePage('收益')} type="button">
        查看收益原因
      </button>
    </section>
  )
}
