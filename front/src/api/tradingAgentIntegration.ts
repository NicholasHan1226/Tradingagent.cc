import type { TradingAgentReadModelSnapshot } from './tradingAgentReadModel.ts'

export const TRADING_AGENT_SNAPSHOT_ROUTE = '/api/trading-agent/snapshot'
const TRADING_AGENT_SNAPSHOT_URL_ENV = 'VITE_TRADING_AGENT_SNAPSHOT_URL'

type SnapshotClientOptions = {
  endpoint?: string
  fetcher?: typeof fetch
  timeoutMs?: number
}

export function createTradingAgentSnapshotClient({
  endpoint = resolveTradingAgentSnapshotEndpoint(),
  fetcher = fetch,
  timeoutMs = 8000,
}: SnapshotClientOptions = {}) {
  return {
    async getSnapshot(): Promise<TradingAgentReadModelSnapshot> {
      const controller = new AbortController()
      const timer = setTimeout(() => controller.abort(), timeoutMs)

      try {
        const response = await fetcher(endpoint, {
          method: 'GET',
          headers: { Accept: 'application/json' },
          signal: controller.signal,
        })

        if (!response.ok) {
          throw new Error(`TradingAgent snapshot request failed: ${response.status}`)
        }

        const payload = await response.json()
        assertTradingAgentSnapshot(payload)
        return payload
      } finally {
        clearTimeout(timer)
      }
    },
  }
}

export function resolveTradingAgentSnapshotEndpoint() {
  const meta = import.meta as ImportMeta & { env?: Record<string, string | undefined> }
  const configuredEndpoint = meta.env?.[TRADING_AGENT_SNAPSHOT_URL_ENV]?.trim()
  return configuredEndpoint || TRADING_AGENT_SNAPSHOT_ROUTE
}

export async function getTradingAgentSnapshotResponse(readSnapshot: () => Promise<TradingAgentReadModelSnapshot>) {
  try {
    const snapshot = await readSnapshot()
    return Response.json(snapshot, {
      status: 200,
      headers: {
        'Cache-Control': 'no-store',
      },
    })
  } catch (error) {
    return Response.json(
      {
        error: error instanceof Error ? error.message : 'TradingAgent snapshot unavailable',
      },
      { status: 503 },
    )
  }
}

function assertTradingAgentSnapshot(payload: unknown): asserts payload is TradingAgentReadModelSnapshot {
  if (!payload || typeof payload !== 'object') {
    throw new Error('TradingAgent snapshot contract is invalid')
  }

  const candidate = payload as Partial<TradingAgentReadModelSnapshot>
  const domains = candidate.domains
  const hasDomains =
    Boolean(domains) &&
    ['performance', 'signals', 'holdings', 'decisions', 'risk'].every((domain) => domain in domains!)

  if (candidate.mode !== 'simulated' || !candidate.generatedAt || !hasDomains || !Array.isArray(candidate.funnelEvents) || !candidate.sourceRefs) {
    throw new Error('TradingAgent snapshot contract is invalid')
  }
}
