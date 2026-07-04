import type { DashboardAdapterResult, DashboardApiResponse } from '../api/types'
import type { DomainHealth } from '../types/status'

export function toDashboardState(response: DashboardApiResponse): DashboardAdapterResult {
  if (response.mode === 'live-disabled' || response.mode === 'live-pending') {
    return {
      mode: response.mode,
      status: 'live-gated',
      domains: Object.fromEntries(
        Object.entries(response.domains).map(([domain, health]) => [
          domain,
          {
            ...health,
            status: 'live-gated',
            message: health.message ?? '实盘完成授权、风控确认和回执写回后才会显示。',
          } satisfies DomainHealth,
        ]),
      ) as DashboardAdapterResult['domains'],
    }
  }

  return {
    mode: response.mode,
    status: response.status,
    domains: response.domains,
  }
}
