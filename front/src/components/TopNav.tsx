import { Bell, Settings } from 'lucide-react'
import { pages } from '../data/dashboard'
import type { Page } from '../types/dashboard'

export function TopNav({
  activePage,
  setActivePage,
}: {
  activePage: Page
  setActivePage: (page: Page) => void
}) {
  return (
    <header className="top-nav">
      <button aria-label="回到总览" className="brand-lockup" onClick={() => setActivePage('总览')} type="button">
        <span className="brand-wordmark">
          <strong><span>Trading</span><b>Agent</b></strong>
        </span>
      </button>
      <nav aria-label="主导航">
        {pages.map((item) => (
          <button aria-keyshortcuts={`Alt+${pages.indexOf(item) + 1}`} className={activePage === item ? 'selected' : ''} key={item} onClick={() => setActivePage(item)} type="button">
            {item}
          </button>
        ))}
      </nav>
      <div className="top-actions">
        <span className="top-status"><i />自动化运行中</span>
        <button className="icon-button" type="button" aria-label="提醒"><Bell size={16} /></button>
        <button className="icon-button" type="button" aria-label="设置"><Settings size={16} /></button>
      </div>
    </header>
  )
}
