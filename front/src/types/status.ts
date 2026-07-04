export type DataDomain = 'performance' | 'signals' | 'holdings' | 'decisions' | 'risk'

export type DomainStatus = 'ready' | 'loading' | 'empty' | 'stale' | 'error' | 'live-gated'

export type DomainHealth = {
  status: DomainStatus
  updatedAt: string
  message?: string
}

export type AccountMode = 'simulated' | 'live-disabled' | 'live-pending' | 'live-active'

export type DashboardState = {
  mode: AccountMode
  status: DomainStatus
  domains: Record<DataDomain, DomainHealth>
}
