import { mkdir, writeFile } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { readTradingAgentSnapshot } from './tradingAgentSnapshot'

async function createWorkspace(payload?: Record<string, unknown>) {
  const root = join(tmpdir(), `ta-today-run-${Date.now()}-${Math.random().toString(16).slice(2)}`)
  const project = join(root, 'TradingAgent')
  await mkdir(join(project, 'shared/runtime/run_bundles'), { recursive: true })
  await mkdir(join(project, 'signals'), { recursive: true })
  if (payload) {
    const encoded = `${canonicalJson(payload)}\n`
    const publishRoot = join(project, 'shared/runtime/run_bundles')
    await writeFile(join(publishRoot, 'latest.json'), encoded)
    const runId = typeof payload.run_id === 'string' ? payload.run_id : undefined
    const bundleSha256 = typeof (payload._projection as Record<string, unknown> | undefined)?.bundle_sha256 === 'string'
      ? String((payload._projection as Record<string, unknown>).bundle_sha256)
      : undefined
    if (runId && bundleSha256) {
      const immutableRoot = join(publishRoot, 'runs', runId)
      await mkdir(immutableRoot, { recursive: true })
      await writeFile(join(immutableRoot, `${bundleSha256}.json`), encoded)
    }
  }
  return root
}

const STAGES = [
  'preopen',
  'evidence_ready',
  'universe_ready',
  'decision_ready',
  'risk_checked',
  'orders_simulated',
  'reconciled',
  'learning_recorded',
  'reported',
] as const

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`
  if (value !== null && typeof value === 'object') {
    const rows = Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0)
      .map(([key, child]) => `${JSON.stringify(key)}:${canonicalJson(child)}`)
    return `{${rows.join(',')}}`
  }
  const encoded = JSON.stringify(value)
  if (encoded === undefined) throw new Error('fixture contains a non-JSON value')
  return encoded
}

function sha256(value: string) {
  return createHash('sha256').update(value).digest('hex')
}

function component(stage: typeof STAGES[number], index: number) {
  return {
    stage,
    component_id: `fixture-${stage}`,
    version: '1',
    artifact_sha256: String(index + 1).repeat(64),
  }
}

function bundleStatus(receiptCount: number, stopNewRisk: boolean) {
  if (receiptCount === STAGES.length) return stopNewRisk ? 'completed_with_blocks' : 'completed'
  return stopNewRisk ? 'incomplete_with_blocks' : 'incomplete'
}

type SealRunBundleOptions = {
  delayEvidenceReasonsUntilRisk?: boolean
  forcePreopenPositionAuthorityValid?: boolean
  forceRiskPermittedOrderIds?: string[]
  forceOrdersPositionAuthorityValid?: boolean
}

function sealRunBundle(value: Record<string, unknown>, options: SealRunBundleOptions = {}) {
  const context = structuredClone(value.context as Record<string, unknown>)
  const components = STAGES.map(component)
  const runIdentity = {
    contract_id: 'tradingagent.paper_day_loop.v1',
    trade_date: context.trade_date,
    decision_as_of: context.decision_as_of,
    market: context.market,
    authority_id: context.authority_id,
    authority_generation: context.authority_generation,
    execution_lineage: context.execution_lineage,
    account_type: context.account_type,
    real_trading_enabled: context.real_trading_enabled,
  }
  const runId = `ashare-paper-day-${sha256(canonicalJson(runIdentity)).slice(0, 32)}`
  const inputRows = structuredClone(value.stage_receipts as Array<Record<string, unknown>>)
  const receipts: Array<Record<string, unknown>> = []
  let stopNewRisk = false
  let positionAuthorityValid = false
  let blockReasons: string[] = []
  let permittedOrderIds: string[] = []
  let delayedReasons: string[] = []
  const root = () => ({
    contract_id: 'tradingagent.paper_day_loop.v1',
    run_id: runId,
    context,
    components,
    component_manifest_sha256: sha256(canonicalJson(components)),
    stage_receipts: receipts,
    stop_new_risk: stopNewRisk,
    position_authority_valid: positionAuthorityValid,
    exit_evaluation_allowed: positionAuthorityValid,
    block_reasons: blockReasons,
    permitted_order_ids: permittedOrderIds,
    status: bundleStatus(receipts.length, stopNewRisk),
  })

  for (const [index, row] of inputRows.entries()) {
    const stage = STAGES[index]
    const payload = structuredClone(row.payload as Record<string, unknown>)
    const reasonCodes = Array.isArray(row.reason_codes) ? row.reason_codes.map(String) : []
    const inputBundleSha256 = sha256(canonicalJson(root()))
    const idempotencyKey = sha256(canonicalJson({
      run_id: runId,
      stage,
      input_bundle_sha256: inputBundleSha256,
      component_id: components[index].component_id,
      component_version: components[index].version,
      component_artifact_sha256: components[index].artifact_sha256,
    }))
    const payloadSha256 = sha256(canonicalJson(payload))
    const identity = {
      stage,
      status: reasonCodes.length ? 'completed_with_blocks' : 'completed',
      idempotency_key: idempotencyKey,
      component: components[index],
      input_bundle_sha256: inputBundleSha256,
      payload_sha256: payloadSha256,
      reason_codes: reasonCodes,
    }
    receipts.push({
      ...identity,
      payload,
      receipt_id: sha256(canonicalJson(identity)),
    })
    if (reasonCodes.length && options.delayEvidenceReasonsUntilRisk && stage === 'evidence_ready') {
      delayedReasons = [...reasonCodes]
    } else if (reasonCodes.length) {
      stopNewRisk = true
      blockReasons = [...new Set([...blockReasons, ...reasonCodes])]
    }
    if (stage === 'preopen' && typeof payload.position_authority_valid === 'boolean') {
      positionAuthorityValid = options.forcePreopenPositionAuthorityValid ?? payload.position_authority_valid
    }
    if (stage === 'orders_simulated') {
      const orderReceipts = Array.isArray(payload.order_receipts) ? payload.order_receipts : []
      if (orderReceipts.some((order) => ['filled', 'partial'].includes(String((order as Record<string, unknown>).status)))) {
        positionAuthorityValid = false
      }
      positionAuthorityValid = options.forceOrdersPositionAuthorityValid ?? positionAuthorityValid
    }
    if (stage === 'reconciled' && typeof payload.position_authority_valid === 'boolean') {
      positionAuthorityValid = payload.position_authority_valid
    }
    if (stage === 'risk_checked') {
      const approved = Array.isArray(payload.approved_orders) ? payload.approved_orders : []
      const derivedPermittedOrderIds = approved.flatMap((order) => {
        const orderId = (order as Record<string, unknown>).order_id
        return typeof orderId === 'string' ? [orderId] : []
      })
      permittedOrderIds = options.forceRiskPermittedOrderIds ?? derivedPermittedOrderIds
      if (delayedReasons.length) {
        stopNewRisk = true
        blockReasons = [...new Set([...blockReasons, ...delayedReasons])]
        delayedReasons = []
      }
    }
  }
  const sealed = root()
  return {
    ...sealed,
    _projection: {
      authority: 'non_authority',
      bundle_sha256: sha256(canonicalJson(sealed)),
      environment: 'local_candidate',
      production_verified: false,
      record_type: 'run_bundle_projection',
      schema_version: 1,
    },
  }
}

function fullyResealedStateAttack(
  attack: 'permitted_order_ids' | 'position_authority' | 'delayed_block_reasons',
) {
  const payload = runBundle()
  const receipts = payload.stage_receipts as Array<Record<string, unknown>>
  if (attack === 'permitted_order_ids') {
    const risk = receipts.find((row) => row.stage === 'risk_checked')!
    ;((risk.payload as Record<string, unknown>).approved_orders as Array<Record<string, unknown>>)[0].order_id = 'forged-order'
    return sealRunBundle(payload, { forceRiskPermittedOrderIds: ['o1'] })
  }
  if (attack === 'position_authority') {
    const preopen = receipts.find((row) => row.stage === 'preopen')!
    ;(preopen.payload as Record<string, unknown>).position_authority_valid = false
    return sealRunBundle(payload, { forcePreopenPositionAuthorityValid: true })
  }
  const evidence = receipts.find((row) => row.stage === 'evidence_ready')!
  evidence.reason_codes = ['dataset_stale']
  return sealRunBundle(payload, { delayEvidenceReasonsUntilRisk: true })
}

function runBundle(overrides: Record<string, unknown> = {}) {
  return sealRunBundle({
    contract_id: 'tradingagent.paper_day_loop.v1',
    context: {
      trade_date: '2026-07-16',
      decision_as_of: '2026-07-16T09:05:00+08:00',
      market: 'ashare',
      authority_id: 'ashare-capital-v1',
      authority_generation: 1,
      execution_lineage: 'ashare-sim-fixture-v1',
      account_type: 'simulated',
      real_trading_enabled: false,
      champion_manifest_sha256: 'c'.repeat(64),
    },
    stage_receipts: [
      { stage: 'preopen', status: 'completed', payload: { position_authority_valid: true } },
      {
        stage: 'evidence_ready',
        status: 'completed',
        payload: {
          execution_eligible: true,
          datasets: [
            {
              dataset_id: 'fixture.cn.equity.daily.v1',
              role: 'required_execution',
              state: 'ready',
              evidence_action: 'accept',
              effective_weight: 1,
              receipt_id: 'receipt-1',
            },
          ],
        },
      },
      {
        stage: 'universe_ready',
        status: 'completed',
        payload: { feasible_symbols: ['000001.SZ', '600000.SH'] },
      },
      {
        stage: 'decision_ready',
        status: 'completed',
        payload: {
          champion_manifest_sha256: 'c'.repeat(64),
          decisions: [{ decision_id: 'd1', symbol: '000001.SZ', action: 'open' }],
          llm_evidence: { role: 'evidence_only', status: 'available' },
        },
      },
      {
        stage: 'risk_checked',
        status: 'completed',
        payload: {
          approved_orders: [{ order_id: 'o1', symbol: '000001.SZ', intent: 'open' }],
          rejected_order_ids: [],
        },
      },
      {
        stage: 'orders_simulated',
        status: 'completed',
        payload: { order_receipts: [{ order_id: 'o1', status: 'filled' }] },
      },
      {
        stage: 'reconciled',
        status: 'completed',
        payload: { status: 'reconciled', position_authority_valid: true },
      },
      { stage: 'learning_recorded', status: 'completed', payload: { recorded: true } },
      { stage: 'reported', status: 'completed', payload: { reported: true } },
    ],
    ...overrides,
  })
}

describe('TradingAgent Today RunBundle read model', () => {
  it('summarizes an explicit local simulated RunBundle without claiming production', async () => {
    const workspaceRoot = await createWorkspace(runBundle())

    const snapshot = await readTradingAgentSnapshot({
      workspaceRoot,
      now: new Date('2026-07-16T08:00:00.000Z'),
    })

    expect(snapshot.paperDayRun).toEqual(expect.objectContaining({
      environment: 'local_candidate',
      productionVerified: false,
      runId: 'ashare-paper-day-3a8d9e49f71145ad22d2a522328794f0',
      tradeDate: '2026-07-16',
      status: 'completed',
      currentStage: 'reported',
      completedStageCount: 9,
      totalStageCount: 9,
      dataEvidenceState: 'ready',
      simulationExecutionState: 'eligible',
      candidateCount: 2,
      decisionCount: 1,
      simulatedOrderCount: 1,
      simulatedFillCount: 1,
      llmEvidenceState: 'evidence_only',
    }))
  })

  it('omits missing or unsafe bundles instead of manufacturing status', async () => {
    const missingRoot = await createWorkspace()
    const unsafePayload = runBundle()
    ;(unsafePayload.context as Record<string, unknown>).real_trading_enabled = true
    const unsafeRoot = await createWorkspace(sealRunBundle(unsafePayload))

    const missing = await readTradingAgentSnapshot({ workspaceRoot: missingRoot })
    const unsafe = await readTradingAgentSnapshot({ workspaceRoot: unsafeRoot })

    expect(missing.paperDayRun).toBeUndefined()
    expect(unsafe.paperDayRun).toBeUndefined()
  })

  it('requires the exact non-authority local projection boundary', async () => {
    const { _projection: _ignored, ...missingProjection } = runBundle()
    const forgedProduction = runBundle()
    ;(forgedProduction._projection as Record<string, unknown>).production_verified = true
    const missingRoot = await createWorkspace(missingProjection)
    const forgedRoot = await createWorkspace(forgedProduction)

    const missing = await readTradingAgentSnapshot({ workspaceRoot: missingRoot })
    const forged = await readTradingAgentSnapshot({ workspaceRoot: forgedRoot })

    expect(missing.paperDayRun).toBeUndefined()
    expect(forged.paperDayRun).toBeUndefined()
  })

  it('rejects a completed claim when the ordered receipt chain is incomplete', async () => {
    const payload = runBundle()
    payload.stage_receipts = payload.stage_receipts.slice(0, 4)
    const workspaceRoot = await createWorkspace(payload)

    const snapshot = await readTradingAgentSnapshot({ workspaceRoot })

    expect(snapshot.paperDayRun).toBeUndefined()
  })

  it('keeps degraded data and no-trade reasons visible and fail-closed', async () => {
    const payload = runBundle()
    const receipts = payload.stage_receipts as Array<Record<string, unknown>>
    const evidence = receipts.find((row) => row.stage === 'evidence_ready')!
    evidence.payload = {
      datasets: [{ dataset_id: 'fixture.cn.equity.daily.v1', state: 'stale', evidence_action: 'reject', receipt_id: 'receipt-1' }],
    }
    evidence.reason_codes = ['dataset_stale']
    const risk = receipts.find((row) => row.stage === 'risk_checked')!
    risk.payload = { approved_orders: [], rejected_order_ids: ['o1'], no_trade_reasons: ['no_execution_eligible_candidates'] }
    const orders = receipts.find((row) => row.stage === 'orders_simulated')!
    orders.payload = { order_receipts: [] }
    const workspaceRoot = await createWorkspace(sealRunBundle(payload))

    const snapshot = await readTradingAgentSnapshot({ workspaceRoot })

    expect(snapshot.paperDayRun).toEqual(expect.objectContaining({
      dataEvidenceState: 'degraded',
      simulationExecutionState: 'blocked',
      noTradeReasons: ['no_execution_eligible_candidates'],
      riskBlocks: ['dataset_stale'],
      simulatedFillCount: 0,
    }))
  })

  it('blocks new simulated risk when position authority is invalid', async () => {
    const payload = runBundle()
    const receipts = payload.stage_receipts as Array<Record<string, unknown>>
    const preopen = receipts.find((row) => row.stage === 'preopen')!
    preopen.payload = { position_authority_valid: false }
    preopen.reason_codes = ['position_authority_invalid']
    const risk = receipts.find((row) => row.stage === 'risk_checked')!
    risk.payload = { approved_orders: [], rejected_order_ids: ['o1'] }
    const orders = receipts.find((row) => row.stage === 'orders_simulated')!
    orders.payload = { order_receipts: [] }
    const workspaceRoot = await createWorkspace(sealRunBundle(payload))

    const snapshot = await readTradingAgentSnapshot({ workspaceRoot })

    expect(snapshot.paperDayRun).toEqual(expect.objectContaining({
      simulationExecutionState: 'blocked',
      riskBlocks: expect.arrayContaining(['position_authority_invalid']),
    }))
  })

  it('deweights optional context without hiding an accepted required execution dataset', async () => {
    const payload = runBundle()
    const receipts = payload.stage_receipts as Array<Record<string, unknown>>
    const evidence = receipts.find((row) => row.stage === 'evidence_ready')!
    evidence.payload = {
      execution_eligible: true,
      datasets: [
        {
          dataset_id: 'fixture.cn.equity.daily.v1',
          role: 'required_execution',
          state: 'ready',
          evidence_action: 'accept',
          effective_weight: 1,
          receipt_id: 'receipt-daily',
        },
        {
          dataset_id: 'fixture.cn.index.daily.v1',
          role: 'optional_context',
          state: 'degraded',
          evidence_action: 'deweight',
          effective_weight: 0.5,
          receipt_id: 'receipt-index',
        },
      ],
    }
    const workspaceRoot = await createWorkspace(sealRunBundle(payload))

    const snapshot = await readTradingAgentSnapshot({ workspaceRoot })

    expect(snapshot.paperDayRun).toEqual(expect.objectContaining({
      dataEvidenceState: 'degraded',
      simulationExecutionState: 'eligible',
    }))
  })

  it.each([
    ['payload content', (payload: Record<string, unknown>) => {
      const receipts = payload.stage_receipts as Array<Record<string, unknown>>
      ;(receipts[3].payload as Record<string, unknown>).decisions = []
    }],
    ['receipt identity', (payload: Record<string, unknown>) => {
      const receipts = payload.stage_receipts as Array<Record<string, unknown>>
      receipts[3].receipt_id = '0'.repeat(64)
    }],
    ['component manifest', (payload: Record<string, unknown>) => {
      payload.component_manifest_sha256 = '0'.repeat(64)
    }],
    ['bundle projection', (payload: Record<string, unknown>) => {
      ;(payload._projection as Record<string, unknown>).bundle_sha256 = '0'.repeat(64)
    }],
  ])('rejects a tampered %s even when the file remains valid JSON', async (_label, tamper) => {
    const payload = runBundle()
    tamper(payload)
    const workspaceRoot = await createWorkspace(payload)

    const snapshot = await readTradingAgentSnapshot({ workspaceRoot })

    expect(snapshot.paperDayRun).toBeUndefined()
  })

  it.each([
    ['permitted order chain', 'permitted_order_ids'],
    ['position-authority chain', 'position_authority'],
    ['block-reason accumulation chain', 'delayed_block_reasons'],
  ] as const)('rejects a fully resealed %s attack', async (_label, attack) => {
    const workspaceRoot = await createWorkspace(fullyResealedStateAttack(attack))

    const snapshot = await readTradingAgentSnapshot({ workspaceRoot })

    expect(snapshot.paperDayRun).toBeUndefined()
  })

  it.each([
    ['existing evidence block with a new-risk order', (payload: Record<string, unknown>) => {
      const receipts = payload.stage_receipts as Array<Record<string, unknown>>
      const evidence = receipts.find((row) => row.stage === 'evidence_ready')!
      evidence.reason_codes = ['dataset_stale']
    }, undefined],
    ['duplicate open order id', (payload: Record<string, unknown>) => {
      const receipts = payload.stage_receipts as Array<Record<string, unknown>>
      const risk = receipts.find((row) => row.stage === 'risk_checked')!
      const approved = (risk.payload as Record<string, unknown>).approved_orders as Array<Record<string, unknown>>
      approved.push(structuredClone(approved[0]))
    }, ['o1']],
  ] as const)('rejects fully resealed permitted ids not produced by the day-loop reducer: %s', async (
    _label,
    mutate,
    forcedPermitted,
  ) => {
    const payload = runBundle()
    mutate(payload)
    const forged = sealRunBundle(payload, {
      ...(forcedPermitted ? { forceRiskPermittedOrderIds: [...forcedPermitted] } : {}),
    })
    const workspaceRoot = await createWorkspace(forged)

    const snapshot = await readTradingAgentSnapshot({ workspaceRoot })

    expect(snapshot.paperDayRun).toBeUndefined()
  })

  it('rejects a fully resealed unfilled receipt with invalid proof that preserves position authority', async () => {
    const payload = runBundle()
    const receipts = payload.stage_receipts as Array<Record<string, unknown>>
    const orders = receipts.find((row) => row.stage === 'orders_simulated')!
    orders.payload = { order_receipts: [{ order_id: 'o1', status: 'rejected' }] }
    orders.reason_codes = ['unfilled_receipt_proof_invalid']
    const forged = sealRunBundle(payload, { forceOrdersPositionAuthorityValid: true })
    const workspaceRoot = await createWorkspace(forged)

    const snapshot = await readTradingAgentSnapshot({ workspaceRoot })

    expect(snapshot.paperDayRun).toBeUndefined()
  })

  it('preserves a reduce order under an existing block when position authority is valid', async () => {
    const payload = runBundle()
    const receipts = payload.stage_receipts as Array<Record<string, unknown>>
    const evidence = receipts.find((row) => row.stage === 'evidence_ready')!
    evidence.reason_codes = ['dataset_stale']
    const risk = receipts.find((row) => row.stage === 'risk_checked')!
    const approved = (risk.payload as Record<string, unknown>).approved_orders as Array<Record<string, unknown>>
    approved[0].intent = 'reduce'
    payload.stage_receipts = receipts.slice(0, 5)
    const workspaceRoot = await createWorkspace(sealRunBundle(payload))

    const snapshot = await readTradingAgentSnapshot({ workspaceRoot })

    expect(snapshot.paperDayRun).toEqual(expect.objectContaining({
      completedStageCount: 5,
      currentStage: 'risk_checked',
      riskBlocks: ['dataset_stale'],
      status: 'incomplete_with_blocks',
    }))
  })

  it.each([
    ['microseconds', '2026-07-16T09:05:00.000001+08:00'],
    ['cross-day Shanghai local time', '2026-07-15T23:59:59+08:00'],
    ['noncanonical UTC spelling', '2026-07-16T01:05:00Z'],
    ['noncanonical offset spelling', '2026-07-16T09:05:00+0800'],
  ])('rejects a fully resealed RunContext with %s', async (_label, decisionAsOf) => {
    const payload = runBundle()
    ;(payload.context as Record<string, unknown>).decision_as_of = decisionAsOf
    const workspaceRoot = await createWorkspace(sealRunBundle(payload))

    const snapshot = await readTradingAgentSnapshot({ workspaceRoot })

    expect(snapshot.paperDayRun).toBeUndefined()
  })

  it.each([
    ['empty', 0, undefined, 'incomplete'],
    ['partial', 3, 'universe_ready', 'incomplete'],
  ] as const)('accepts a valid %s receipt prefix', async (
    _label,
    receiptCount,
    currentStage,
    status,
  ) => {
    const payload = runBundle()
    payload.stage_receipts = payload.stage_receipts.slice(0, receiptCount)
    const workspaceRoot = await createWorkspace(sealRunBundle(payload))

    const snapshot = await readTradingAgentSnapshot({ workspaceRoot })

    expect(snapshot.paperDayRun).toEqual(expect.objectContaining({
      completedStageCount: receiptCount,
      currentStage,
      status,
    }))
  })

  it('rejects an unknown top-level RunBundle field even when the projection is resealed', async () => {
    const payload: Record<string, unknown> = runBundle()
    payload.future_unreviewed_field = true
    const root = { ...payload }
    delete root._projection
    ;(payload._projection as Record<string, unknown>).bundle_sha256 = sha256(canonicalJson(root))
    const workspaceRoot = await createWorkspace(payload)

    const snapshot = await readTradingAgentSnapshot({ workspaceRoot })

    expect(snapshot.paperDayRun).toBeUndefined()
  })
})
