import type { DashboardApiResponse } from '../api/types'
import type { TradingAgentReadModelSnapshot } from '../api/tradingAgentReadModel'

export function toDashboardApiResponseFromReadModel(snapshot: TradingAgentReadModelSnapshot): DashboardApiResponse {
  return {
    mode: snapshot.mode,
    status: getAggregateStatus(snapshot),
    domains: snapshot.domains,
  }
}

function getAggregateStatus(snapshot: TradingAgentReadModelSnapshot): DashboardApiResponse['status'] {
  const statuses = Object.values(snapshot.domains).map((domain) => domain.status)
  if (statuses.includes('error')) return 'error'
  if (statuses.includes('loading')) return 'loading'
  if (statuses.includes('stale')) return 'stale'
  if (statuses.every((status) => status === 'empty')) return 'empty'
  return 'ready'
}
