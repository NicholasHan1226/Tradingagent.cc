import type { AccountMode, DashboardState, DataDomain, DomainHealth, DomainStatus } from '../types/status.ts'

export type ApiStatus = Exclude<DomainStatus, 'live-gated'>

export type ApiDomainHealth = Omit<DomainHealth, 'status'> & {
  status: ApiStatus
}

export type DashboardApiResponse = {
  mode: AccountMode
  status: ApiStatus
  domains: Record<DataDomain, ApiDomainHealth>
}

export type DashboardAdapterResult = DashboardState
