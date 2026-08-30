import type { RuntimeObservations } from '../types/runtimeObservations'

/** Synthetic unit-test input only; never imported by application code. */
export function runtimeObservationFixture(): RuntimeObservations {
  return {
    contract: 'tradingagent.runtime_observations.v1', readOnly: true, realTradingEnabled: false,
    generatedAt: '2026-08-30T12:00:00Z',
    entries: [
      { id: 'ashare-minute-scale', market: 'A-share', sourceClass: 'delayed_research', status: 'dated', observedAt: '2026-08-28T01:45:00Z', sourceSha256: 'a'.repeat(64), coverage: { universe: 100, accepted: 30, missing: 70 }, canonicalAccountConnected: false, reason: 'historical_coverage' },
      { id: 'crypto-g5', market: 'Crypto', sourceClass: 'delayed_research', status: 'dated', observedAt: '2026-08-29T00:00:00Z', sourceSha256: 'b'.repeat(64), simulation: { currency: 'USDT', cash: '9234.567890123456789', equity: '9981.23', fees: '18.77', realizedPnl: '-2.34', positions: 2, orders: 7 }, counts: { completed: 12, rejected: 3 }, canonicalAccountConnected: false, reason: 'backlog_pending' },
    ],
  }
}
