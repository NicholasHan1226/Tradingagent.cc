import { Bell, Settings } from 'lucide-react'
import { pages } from '../data/dashboard'
import type { Page } from '../types/dashboard'
import type { RuntimeHeartbeat } from '../lib/runtimeHeartbeat'

export function TopNav({
  activePage,
  heartbeat,
  setActivePage,
}: {
  activePage: Page
  heartbeat: RuntimeHeartbeat
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
        <span className={`top-status ${heartbeat.state}`}><i />{topStatusLabel(heartbeat.state)}</span>
        <button className="icon-button" type="button" aria-label="提醒"><Bell size={16} /></button>
        <button className="icon-button" type="button" aria-label="设置"><Settings size={16} /></button>
      </div>
    </header>
  )
}

function topStatusLabel(state: RuntimeHeartbeat['state']) {
  if (state === 'live') return '自动化运行中'
  if (state === 'idle') return '调度正常 · 空闲'
  if (state === 'stale') return '快照滞后'
  return '运行需要关注'
}
