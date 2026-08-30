/** Independent delayed research only; never an account/performance/heartbeat input. */
export type RuntimeObservationEntry = {
  id: 'ashare-minute-scale' | 'crypto-g5'
  market: 'A-share' | 'Crypto'
  sourceClass: 'delayed_research'
  status: 'ready' | 'dated' | 'pending' | 'unavailable' | 'invalid'
  observedAt: string | null
  sourceSha256: string | null
  coverage?: { universe: number; accepted: number; missing: number }
  simulation?: {
    currency: 'CNY' | 'USDT'
    cash: string
    equity: string
    fees: string
    realizedPnl: string
    positions: number
    orders: number
  }
  counts?: { completed: number; rejected: number }
  canonicalAccountConnected: false
  reason: string
}

export type RuntimeObservations = {
  contract: 'tradingagent.runtime_observations.v1'
  readOnly: true
  realTradingEnabled: false
  generatedAt: string
  entries: RuntimeObservationEntry[]
}

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function keys(value: Record<string, unknown>, allowed: string[]) {
  return Object.keys(value).every((key) => allowed.includes(key))
}

function count(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
}

function decimal(value: unknown): value is string {
  return typeof value === 'string' && value.length <= 128 && /^-?(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value)
}

function iso(value: unknown): value is string {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return false
  const date = new Date(value.slice(0, 10) + 'T00:00:00Z')
  return Number.isFinite(Date.parse(value)) && Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value.slice(0, 10)
}

function sourceTime(value: string): bigint {
  // Python emits microseconds; Date alone would miss a future time within 1 ms.
  const fraction = value.match(/\.(\d+)/)?.[1] ?? ''
  return BigInt(Date.parse(value)) * 1_000_000n + BigInt(fraction.padEnd(9, '0').slice(3))
}

function entry(value: unknown): value is RuntimeObservationEntry {
  if (!record(value) || !keys(value, ['id', 'market', 'sourceClass', 'status', 'observedAt', 'sourceSha256', 'coverage', 'simulation', 'counts', 'canonicalAccountConnected', 'reason'])) return false
  if (!((value.id === 'ashare-minute-scale' && value.market === 'A-share') || (value.id === 'crypto-g5' && value.market === 'Crypto'))) return false
  if (value.sourceClass !== 'delayed_research' || value.canonicalAccountConnected !== false) return false
  if (!['ready', 'dated', 'pending', 'unavailable', 'invalid'].includes(value.status as string)) return false
  if (value.observedAt !== null && !iso(value.observedAt)) return false
  if (value.sourceSha256 !== null && !(typeof value.sourceSha256 === 'string' && /^[a-fA-F0-9]{64}$/.test(value.sourceSha256))) return false
  if (typeof value.reason !== 'string' || value.reason.length > 2048) return false
  if (value.status === 'ready' || value.status === 'dated') {
    if (value.observedAt === null || value.sourceSha256 === null) return false
  } else if ('simulation' in value || 'coverage' in value || 'counts' in value) return false
  if ('coverage' in value) {
    const c = value.coverage
    if (!record(c) || !keys(c, ['universe', 'accepted', 'missing']) || !count(c.universe) || !count(c.accepted) || !count(c.missing)) return false
    if (c.accepted > c.universe || c.missing > c.universe || c.accepted + c.missing !== c.universe) return false
  }
  if ('counts' in value) {
    const c = value.counts
    if (!record(c) || !keys(c, ['completed', 'rejected']) || !count(c.completed) || !count(c.rejected)) return false
  }
  if ('simulation' in value) {
    const s = value.simulation
    if (!record(s) || !keys(s, ['currency', 'cash', 'equity', 'fees', 'realizedPnl', 'positions', 'orders'])) return false
    if (s.currency !== (value.market === 'A-share' ? 'CNY' : 'USDT')) return false
    if (!['cash', 'equity', 'fees', 'realizedPnl'].every((key) => decimal(s[key])) || !count(s.positions) || !count(s.orders)) return false
  }
  return true
}

/** Reject unknown fields as well as implicit/missing safety flags. No coercion. */
export function parseRuntimeObservations(value: unknown): RuntimeObservations | undefined {
  if (!record(value) || !keys(value, ['contract', 'readOnly', 'realTradingEnabled', 'generatedAt', 'entries'])) return undefined
  if (value.contract !== 'tradingagent.runtime_observations.v1' || value.readOnly !== true || value.realTradingEnabled !== false || !iso(value.generatedAt)) return undefined
  if (!Array.isArray(value.entries) || value.entries.length > 2 || !value.entries.every(entry)) return undefined
  if (new Set(value.entries.map((item) => item.id)).size !== value.entries.length) return undefined
  if (value.entries.some((item) => item.observedAt !== null && sourceTime(item.observedAt) > sourceTime(value.generatedAt as string))) return undefined
  return value as RuntimeObservations
}

/** Local adapter status, not a fabricated source observation. */
export function runtimeObservationState(status: 'pending' | 'unavailable', reason: string, generatedAt: string): RuntimeObservations {
  return {
    contract: 'tradingagent.runtime_observations.v1', readOnly: true, realTradingEnabled: false, generatedAt,
    entries: [
      { id: 'ashare-minute-scale', market: 'A-share', sourceClass: 'delayed_research', status, observedAt: null, sourceSha256: null, canonicalAccountConnected: false, reason },
      { id: 'crypto-g5', market: 'Crypto', sourceClass: 'delayed_research', status, observedAt: null, sourceSha256: null, canonicalAccountConnected: false, reason },
    ],
  }
}
