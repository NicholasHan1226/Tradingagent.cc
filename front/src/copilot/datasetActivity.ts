import type { DatasetActivityAuthority, StockIntelligence } from './stockIntelligence.ts'

/**
 * A dataset clock is evidence supplied by that dataset, not an application-wide
 * market session.  Consumers only use the receipt-bound authority that the
 * formal projection validator has accepted; every other case is a coverage gap.
 */
export type { DatasetActivityAuthority } from './stockIntelligence.ts'

export type DatasetActivityState = 'live' | 'closed' | 'stale' | 'coverage_gap'

export type DatasetActivity = {
  datasetId: string
  dataThrough: string
  state: DatasetActivityState
  reason: 'dataset_activity_authority_missing' | 'dataset_activity_authority_incomplete' | 'dataset_activity_data_through_invalid' | 'dataset_not_fresh' | null
  clockKey: string | null
}

export type DatasetActivityInput = {
  datasetId: string
  dataThrough: string
  freshness: 'fresh' | 'stale' | 'degraded' | 'demo'
  authority: DatasetActivityAuthority | null
}

export function resolveDatasetActivity(input: DatasetActivityInput): DatasetActivity {
  const base = { datasetId: input.datasetId, dataThrough: input.dataThrough }
  if (!input.authority) return { ...base, state: 'coverage_gap', reason: 'dataset_activity_authority_missing', clockKey: null }
  if (!hasCompleteAuthority(input.authority, input.datasetId, input.dataThrough)) return { ...base, state: 'coverage_gap', reason: 'dataset_activity_authority_incomplete', clockKey: null }
  if (!isTimestamp(input.dataThrough)) return { ...base, state: 'coverage_gap', reason: 'dataset_activity_data_through_invalid', clockKey: null }

  const clockKey = `${input.authority.market}/${input.authority.timezone}/${input.authority.calendar.id}`
  if (input.freshness !== 'fresh') return { ...base, state: 'stale', reason: 'dataset_not_fresh', clockKey }
  return {
    ...base,
    state: input.authority.session.state === 'open' ? 'live' : 'closed',
    reason: null,
    clockKey,
  }
}

export function collectDatasetActivities(intelligence: StockIntelligence): DatasetActivity[] {
  const inputs: DatasetActivityInput[] = []
  if (intelligence.source) {
    inputs.push({
      datasetId: intelligence.source.datasetId,
      dataThrough: intelligence.source.dataThrough,
      freshness: intelligence.source.freshness,
      authority: intelligence.source.activityAuthority ?? null,
    })
  }
  for (const event of intelligence.events) {
    if (!event.dataCapability) continue
    inputs.push({
      datasetId: event.dataCapability.datasetId,
      dataThrough: event.dataCapability.dataThrough,
      freshness: event.dataCapability.freshness,
      authority: event.dataCapability.activityAuthority ?? null,
    })
  }
  return [...inputs
    .reduce((byDataset, input) => {
      const existing = byDataset.get(input.datasetId)
      if (!existing || Date.parse(input.dataThrough) > Date.parse(existing.dataThrough)) byDataset.set(input.datasetId, input)
      return byDataset
    }, new Map<string, DatasetActivityInput>())
    .values()]
    .map(resolveDatasetActivity)
}

function hasCompleteAuthority(authority: DatasetActivityAuthority, datasetId: string, dataThrough: string) {
  return Boolean(
    authority.datasetId === datasetId
    && authority.market === 'ashare'
    && authority.timezone === 'Asia/Shanghai'
    && authority.calendar.id.trim()
    && authority.calendar.version.trim()
    && authority.calendar.sourceDatasetId === 'cn.market.trade_calendar'
    && authority.calendar.receiptId.trim()
    && isSha256(authority.calendar.receiptSha256)
    && isSha256(authority.calendar.lineageSha256)
    && isSha256(authority.calendar.calendarSha256)
    && authority.source.receiptId.trim()
    && isSha256(authority.source.receiptSha256)
    && isSha256(authority.source.lineageSha256)
    && ['open', 'closed', 'halted'].includes(authority.session.state)
    && isTimezoneAwareTimestamp(authority.session.asOf)
    && authority.dataThrough === dataThrough
    && isTimestamp(authority.dataThrough),
  )
}

function isTimestamp(value: string) {
  return Number.isFinite(Date.parse(value))
}

function isTimezoneAwareTimestamp(value: string) {
  return isTimestamp(value) && /(?:Z|[+-]\d{2}:\d{2})$/i.test(value)
}

function isSha256(value: string) {
  return /^[a-f0-9]{64}$/.test(value)
}
