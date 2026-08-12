import type { DatasetActivityAuthority, StockIntelligence } from './stockIntelligence.ts'

export const ACTIVITY_AUTHORITY_MISSING_FIELDS = [
  'calendar.id',
  'calendar.version',
  'calendar.receiptId',
  'calendar.receiptSha256',
  'calendar.lineageSha256',
  'calendar.calendarSha256',
  'session.state',
  'session.asOf',
] as const

export type ActivityAuthorityMissingField = typeof ACTIVITY_AUTHORITY_MISSING_FIELDS[number]
export type DatasetEvidenceQuality = 'usable' | 'usable_degraded' | 'unavailable'

export type ActivityAuthorityBinding = {
  datasetId: string
  dataThrough: string
  receiptId: string
  receiptSha256: string
  lineageSha256: string
}

export type ActivityAuthorityInspection = {
  valid: boolean
  complete: boolean
  quality: DatasetEvidenceQuality
  missingFields: ActivityAuthorityMissingField[]
}

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
  quality: DatasetEvidenceQuality
  missingFields: ActivityAuthorityMissingField[]
}

export type DatasetActivityInput = {
  datasetId: string
  dataThrough: string
  freshness: 'fresh' | 'stale' | 'degraded' | 'demo'
  authority: DatasetActivityAuthority | null
  receiptBound?: boolean
}

export function resolveDatasetActivity(input: DatasetActivityInput): DatasetActivity {
  const base = { datasetId: input.datasetId, dataThrough: input.dataThrough }
  const inspection = inspectActivityAuthority(input.authority, {
    datasetId: input.datasetId,
    dataThrough: input.dataThrough,
    receiptId: input.authority?.source?.receiptId ?? '',
    receiptSha256: input.authority?.source?.receiptSha256 ?? '',
    lineageSha256: input.authority?.source?.lineageSha256 ?? '',
  })
  const receiptBound = input.receiptBound ?? Boolean(input.authority?.source)
  if (!isTimestamp(input.dataThrough)) {
    return {
      ...base,
      state: 'coverage_gap',
      reason: 'dataset_activity_data_through_invalid',
      clockKey: null,
      quality: 'unavailable',
      missingFields: inspection.missingFields,
    }
  }
  if (!inspection.valid) {
    return {
      ...base,
      state: 'coverage_gap',
      reason: 'dataset_activity_authority_incomplete',
      clockKey: null,
      quality: 'unavailable',
      missingFields: inspection.missingFields,
    }
  }
  if (!input.authority) {
    return {
      ...base,
      state: 'coverage_gap',
      reason: 'dataset_activity_authority_missing',
      clockKey: null,
      quality: receiptBound ? 'usable_degraded' : 'unavailable',
      missingFields: [...ACTIVITY_AUTHORITY_MISSING_FIELDS],
    }
  }
  if (!inspection.complete) {
    return {
      ...base,
      state: 'coverage_gap',
      reason: 'dataset_activity_authority_incomplete',
      clockKey: null,
      quality: receiptBound ? 'usable_degraded' : 'unavailable',
      missingFields: inspection.missingFields,
    }
  }

  const clockKey = `${input.authority.market}/${input.authority.timezone}/${input.authority.calendar.id}`
  if (input.freshness !== 'fresh') return { ...base, state: 'stale', reason: 'dataset_not_fresh', clockKey, quality: 'usable_degraded', missingFields: [] }
  return {
    ...base,
    state: input.authority.session.state === 'open' ? 'live' : 'closed',
    reason: null,
    clockKey,
    quality: 'usable',
    missingFields: [],
  }
}

export function inspectActivityAuthority(
  authority: unknown,
  binding: ActivityAuthorityBinding,
): ActivityAuthorityInspection {
  if (authority == null) {
    return {
      valid: true,
      complete: false,
      quality: 'usable_degraded',
      missingFields: [...ACTIVITY_AUTHORITY_MISSING_FIELDS],
    }
  }
  if (!isRecord(authority)) return { valid: false, complete: false, quality: 'unavailable', missingFields: [] }
  if (
    authority.datasetId !== binding.datasetId
    || authority.market !== 'ashare'
    || authority.timezone !== 'Asia/Shanghai'
    || authority.dataThrough !== binding.dataThrough
  ) return { valid: false, complete: false, quality: 'unavailable', missingFields: [] }

  const source = authority.source
  if (source !== undefined && (!isRecord(source)
    || source.receiptId !== binding.receiptId
    || source.receiptSha256 !== binding.receiptSha256
    || source.lineageSha256 !== binding.lineageSha256)) {
    return { valid: false, complete: false, quality: 'unavailable', missingFields: [] }
  }

  const missingFields: ActivityAuthorityMissingField[] = []
  const calendar = isRecord(authority.calendar) ? authority.calendar : null
  const calendarRequired: Array<[ActivityAuthorityMissingField, string]> = [
    ['calendar.id', 'id'],
    ['calendar.version', 'version'],
    ['calendar.receiptId', 'receiptId'],
    ['calendar.receiptSha256', 'receiptSha256'],
    ['calendar.lineageSha256', 'lineageSha256'],
    ['calendar.calendarSha256', 'calendarSha256'],
  ]
  if (calendar && calendar.sourceDatasetId !== undefined && calendar.sourceDatasetId !== 'cn.market.trade_calendar') {
    return { valid: false, complete: false, quality: 'unavailable', missingFields: [] }
  }
  for (const [field, key] of calendarRequired) {
    const value = calendar?.[key]
    if (value === undefined || value === null || value === '') {
      missingFields.push(field)
    } else if (key.endsWith('Sha256') ? typeof value !== 'string' || !isSha256(value) : typeof value !== 'string' || !value.trim()) {
      return { valid: false, complete: false, quality: 'unavailable', missingFields: [] }
    }
  }

  const session = isRecord(authority.session) ? authority.session : null
  const state = session?.state
  if (state === undefined || state === null || state === '') missingFields.push('session.state')
  else if (!['open', 'closed', 'halted'].includes(String(state))) return { valid: false, complete: false, quality: 'unavailable', missingFields: [] }
  const asOf = session?.asOf
  if (asOf === undefined || asOf === null || asOf === '') missingFields.push('session.asOf')
  else if (typeof asOf !== 'string' || !isTimezoneAwareTimestamp(asOf)) return { valid: false, complete: false, quality: 'unavailable', missingFields: [] }

  return {
    valid: true,
    complete: missingFields.length === 0,
    quality: missingFields.length === 0 ? 'usable' : 'usable_degraded',
    missingFields,
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
      receiptBound: Boolean(intelligence.source.receiptId && intelligence.source.receiptSha256 && intelligence.source.lineageSha256),
    })
  }
  for (const event of intelligence.events) {
    if (!event.dataCapability) continue
    inputs.push({
      datasetId: event.dataCapability.datasetId,
      dataThrough: event.dataCapability.dataThrough,
      freshness: event.dataCapability.freshness,
      authority: event.dataCapability.activityAuthority ?? null,
      receiptBound: Boolean(event.dataCapability.receiptId && event.dataCapability.receiptSha256 && event.dataCapability.lineageSha256),
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value))
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
