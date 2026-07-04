import type { HoldingRow, Page } from '../../types/dashboard'
import { AssetCell } from '../AssetCell'
import { PanelTitle } from '../PanelTitle'

export function HoldingsCompact({
  hasHoldingData,
  holdings,
  setActivePage,
}: {
  hasHoldingData: boolean
  holdings: HoldingRow[]
  setActivePage: (page: Page) => void
}) {
  return (
    <section className="panel rail-panel">
      <PanelTitle action="查看持仓" kicker="持仓" onAction={() => setActivePage('持仓')} title="仓位贡献" />
      <div className="compact-holdings">
        {hasHoldingData ? (
          holdings.slice(0, 3).map((holding) => (
            <button key={holding.symbol} onClick={() => setActivePage('持仓')} type="button">
              <AssetCell symbol={holding.symbol} name={holding.name} />
              <span>{holding.weight}</span>
              <strong className={holding.pnl.startsWith('-') ? 'red-text' : 'cyan-text'}>{holding.pnl}</strong>
            </button>
          ))
        ) : (
          <div className="empty-panel-copy">
            <strong>暂无持仓记录</strong>
            <span>模拟盘写入持仓后，会显示仓位、收益和风险。</span>
          </div>
        )}
      </div>
    </section>
  )
}
