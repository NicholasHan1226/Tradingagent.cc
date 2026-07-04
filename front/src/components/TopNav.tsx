import { Bell, Settings } from 'lucide-react'
import { pages } from '../data/dashboard'
import type { AccountMode, Page } from '../types/dashboard'

export function TopNav({
  activePage,
  setActivePage,
}: {
  accountMode: AccountMode
  activePage: Page
  onDismissLiveGate: () => void
  selectAccountMode: (mode: AccountMode) => void
  setActivePage: (page: Page) => void
  showLiveGate: boolean
}) {
  return (
    <header className="top-nav">
      <button aria-label="回到主页" className="brand-lockup" onClick={() => setActivePage('主页')} type="button">
        <span className="brand-wordmark">
          <strong><span>Trading</span><b>Agent</b></strong>
        </span>
      </button>
      <nav aria-label="主导航">
        {pages.map((item) => (
          <button className={activePage === item ? 'selected' : ''} key={item} onClick={() => setActivePage(item)} type="button">
            {item}
          </button>
        ))}
      </nav>
      <div className="top-actions">
        <span className="top-status"><i />模拟盘在线</span>
        <button className="icon-button" type="button" aria-label="提醒"><Bell size={16} /></button>
        <button className="icon-button" type="button" aria-label="设置"><Settings size={16} /></button>
      </div>
    </header>
  )
}
