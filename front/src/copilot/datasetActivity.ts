import type { StockIntelligence } from './stockIntelligence.ts'

/**
 * A dataset clock is evidence supplied by that dataset, not an application-wide
 * market session.  The current projection contract does not yet supply this
 * authority, so consumers must keep the result as a coverage gap.
 */
export type DatasetActivityAuthority = {
  market: string
  timezone: string
  calendar: string
  session: 'open' | 'closed' | 'halted' | 'unknown'
}

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
  if (!hasCompleteAuthority(input.authority)) return { ...base, state: 'coverage_gap', reason: 'dataset_activity_authority_incomplete', clockKey: null }
  if (!isTimestamp(input.dataThrough)) return { ...base, state: 'coverage_gap', reason: 'dataset_activity_data_through_invalid', clockKey: null }

  const clockKey = `${input.authority.market}/${input.authority.timezone}/${input.authority.calendar}`
  if (input.freshness !== 'fresh') return { ...base, state: 'stale', reason: 'dataset_not_fresh', clockKey }
  return {
    ...base,
    state: input.authority.session === 'open' ? 'live' : 'closed',
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
      // The current shared projection contract intentionally has no activity
      // authority. Do not consume an undocumented extension before Controller
      // approves the cross-domain contract change.
      authority: null,
    })
  }
  for (const event of intelligence.events) {
    if (!event.dataCapability) continue
    inputs.push({
      datasetId: event.dataCapability.datasetId,
      dataThrough: event.dataCapability.dataThrough,
      freshness: event.dataCapability.freshness,
      authority: null,
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

function hasCompleteAuthority(authority: DatasetActivityAuthority) {
  return Boolean(authority.market.trim() && authority.timezone.trim() && authority.calendar.trim())
    && ['open', 'closed', 'halted'].includes(authority.session)
}

function isTimestamp(value: string) {
  return Number.isFinite(Date.parse(value))
}
