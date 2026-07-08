import type { CSSProperties } from 'react'
import { getSignalFunnel } from '../../lib/dashboard'
import type { FunnelEvent, HoldingRow, SignalRow } from '../../types/dashboard'

const OUTCOME_LABELS = ['成交', '观察中', '复盘', '放弃']
const MAX_ANIMATED_SIGNALS = 64
const MAX_VISIBLE_LABELS = 3
const FLOW_STAGES = [
  { label: '发现', fallbackIndex: 0 },
  { label: '初筛', fallbackIndex: 1 },
  { label: '研究', fallbackIndex: 1 },
  { label: '风控', fallbackIndex: 2 },
  { label: '信号', fallbackIndex: 3 },
] as const

export function SignalFunnelFlow({
  events,
  hasSignalData,
  holdings,
  signals,
}: {
  events: FunnelEvent[]
  hasSignalData: boolean
  holdings: HoldingRow[]
  signals: SignalRow[]
}) {
  const funnel = getSignalFunnel(signals)
  const eventFlow = getEventFlow(events)
  const hasHoldingContext = !events.length && !signals.length && holdings.length > 0
  const holdingSummary = hasHoldingContext ? getHoldingSummary(holdings) : null
  const visualStages = hasHoldingContext ? getHoldingVisualStages(holdings) : getVisualStages(funnel, eventFlow)
  const maxStageCount = Math.max(1, ...visualStages.map((stage) => stage.count))
  const stageWidths = visualStages.map((stage) => Math.max(12, stage.width ?? Math.round((stage.count / maxStageCount) * 100)))
  const executedSignals = funnel.executed
  const pendingSignals = funnel.pending
  const missedSignals = funnel.missed
  const blockedSignals = [...funnel.blocked, ...funnel.cancelled]
  const outcomeRows = hasHoldingContext ? getHoldingOutcomeRows(holdings) : getOutcomeRows(eventFlow, {
    executed: executedSignals.length,
    pending: pendingSignals.length,
    missed: missedSignals.length,
    abandoned: blockedSignals.length,
  })
  const outcomeLabels = hasHoldingContext ? ['正贡献', '平稳', '观察', '离场'] : OUTCOME_LABELS
  const passRate = Math.round((funnel.executed.length / Math.max(1, signals.length)) * 100)
  const visualStageDrops = visualStages.map((stage) => stage.dropped)
  const hasStageDrop = visualStageDrops.some((drop) => drop > 0)
  const hasTimingEvidence = signals.some((signal) => signal.stageLatencyMinutes && signal.stageLatencyMinutes > 0)
  const bottleneck = hasHoldingContext && holdingSummary
    ? getHoldingBottleneck(holdings, holdingSummary)
    : getBottleneck(visualStages.map((stage) => ({ label: stage.label, count: stage.count })))
  const hasEventSource = events.length > 0
  const eventCaption = eventFlow
    ? `${eventFlow.total} 个机会进入 · ${eventFlow.outcomes.executed} 个成交 · ${eventFlow.outcomes.abandoned} 个放弃`
    : '等待新机会'
  const caption = hasEventSource
    ? eventCaption
    : hasSignalData
    ? funnel.mode === 'screening' || hasStageDrop || hasTimingEvidence || funnel.executed.length !== signals.length
      ? `${signals.length} 个进入 · ${funnel.tradeSignals.length} 个留下 · ${funnel.executed.length} 个成交`
      : `${signals.length} 条成交回放 · 转化 ${passRate}%`
    : holdings.length > 0
      ? `0 个新机会 · ${holdings.length} 个持仓在跟踪`
      : '等待新机会'
  const modeLabel = hasEventSource
    ? '实时'
    : funnel.mode === 'screening'
    ? '筛选中'
    : funnel.mode === 'partial'
      ? '进行中'
      : funnel.mode === 'replay'
        ? '已完成'
        : hasHoldingContext
          ? '空闲'
          : '等待'
  const particles = hasEventSource
    ? buildEventParticles(events)
      : hasSignalData
        ? buildParticles(signals, funnel.mode)
        : hasHoldingContext
          ? buildHoldingParticles(holdings)
        : []
  const latestTapeItems = hasHoldingContext ? getHoldingTape(holdings) : []
  const firstStageCount = Math.max(1, visualStages[0]?.count ?? signals.length)
  const finalStageCount = visualStages.at(-1)?.count ?? 0
  const conversionRate = Math.round((finalStageCount / firstStageCount) * 100)
  const hasFlowVolume = hasEventSource || hasSignalData || hasHoldingContext
  const moduleTitle = '机会管道'
  const captionText = hasHoldingContext ? caption : hasFlowVolume ? `${caption} · 转化 ${conversionRate}%` : caption
  const flowSummary = getFlowSummary({
    bottleneck,
    conversionRate,
    eventFlow,
    finalStageCount,
    firstStageCount,
    hasEventSource,
    hasHoldingContext,
  })
  const lossRows = hasHoldingContext ? getHoldingLossRows(holdings) : getLossRows(visualStages)
  const railRows = getRailRows(visualStages, outcomeRows)

  return (
    <section className="signal-flow-module" aria-label={moduleTitle}>
      <div className={`signal-flow-board real-funnel-board mode-${funnel.mode} ${hasEventSource ? 'mode-real-flow' : ''} ${hasHoldingContext ? 'mode-holding-context mode-holding-flow' : ''} ${hasFlowVolume ? '' : 'is-empty-flow'}`}>
        <div className="flow-caption">
          <span>{moduleTitle} <b>{modeLabel}</b></span>
          <strong>{captionText}</strong>
        </div>
        {hasFlowVolume ? (
          <>
            <div className="real-funnel-body" role="img" aria-label="机会从发现到交易结果的动态筛选漏斗">
              <div className="real-funnel-rulers" aria-hidden="true">
                {visualStages.map((stage, index) => (
                  <i key={stage.label} style={{ left: `${index * 20}%` }} />
                ))}
              </div>
              <div className="real-funnel-rails" aria-hidden="true">
                {railRows.map((rail, index) => (
                  <span
                    className={`real-funnel-rail ${rail.tone} ${rail.dropped > 0 ? 'has-drop' : ''}`}
                    key={rail.label}
                    style={{
                      '--rail-height': `${Math.max(12, stageWidths[index] * 0.58)}px`,
                      '--rail-top': `${rail.offset}px`,
                      '--rail-delay': `${index * 0.16}s`,
                    } as CSSProperties}
                  >
                    <b>{rail.count}</b>
                  </span>
                ))}
              </div>
              <div className="flow-drop-track" aria-hidden="true">
                {visualStages.slice(1).map((stage) => (
                  <span className={stage.dropped > 0 ? 'has-drop' : ''} key={`drop-${stage.label}`}>
                    <i style={{ height: `${getDropHeight(stage.dropped, visualStages[0]?.count ?? 0)}%` }} />
                  </span>
                ))}
              </div>
              {particles.map((particle, index) => (
                <i
                  className={`funnel-particle ${particle.tone} ${particle.showLabel ? 'labeled' : 'quiet'}`}
                  key={`${particle.label}-${index}`}
                  style={{
                    '--delay': particle.begin,
                    '--duration': particle.duration,
                    '--lane': particle.lane,
                    '--to-left': particle.stopLeft,
                  } as CSSProperties}
                  data-symbol={particle.showLabel ? particle.symbol : undefined}
                  title={particle.label}
                >
                  <b />
                </i>
              ))}
              <div className="flow-stage-axis" aria-hidden="true">
                {visualStages.map((stage, index) => (
                  <span className={stage.dropped > 0 ? 'has-loss' : ''} key={`${stage.label}-axis`}>
                    <b>{stage.count}</b>
                    <em>{stage.label}</em>
                    {index > 0 && stage.dropped > 0 && <i>-{stage.dropped}</i>}
                  </span>
                ))}
              </div>
            </div>
            <div className="flow-outcome-strip">
              {outcomeRows.map((count, index) => (
                <span key={outcomeLabels[index]}>
                  <b>{count}</b> {outcomeLabels[index]}
                </span>
              ))}
            </div>
            <div className="flow-loss-ledger" aria-label={hasHoldingContext ? '持仓跟踪变化' : '机会流失位置'}>
              {lossRows.map((row) => (
                <span className={row.count > 0 ? 'has-loss' : ''} key={row.label}>
                  <em>{row.label}</em>
                  <b>{row.count > 0 ? `-${row.count}` : '0'}</b>
                </span>
              ))}
            </div>
            <div className="flow-bottleneck">
              <span>{flowSummary.label}</span>
              <strong>{flowSummary.value} · {flowSummary.detail}</strong>
            </div>
          </>
        ) : (
          <div className="real-funnel-empty" role="status">
            <span>机会管道</span>
            <strong>{hasHoldingContext ? '暂无新机会' : '等待机会进入'}</strong>
            <p>{hasHoldingContext ? `${holdings.length} 个持仓仍在跟踪，未生成新的可处理信号。` : '有机会进入后，这里会显示发现、研究、风控和队列流动。'}</p>
            {hasHoldingContext && holdingSummary && (
              <div className="holding-context-strip" aria-label="持仓跟踪状态">
                <span><b>{holdings.length}</b>持仓</span>
                <span><b>{holdingSummary.positive}</b>正贡献</span>
                <span><b>{holdingSummary.watching}</b>需观察</span>
              </div>
            )}
          </div>
        )}
        {latestTapeItems.length > 0 && (
          <div className="flow-event-tape" aria-label="最近管道事件">
            <b className="event-tape-label">{hasHoldingContext ? '持仓跟踪' : '最新流动'}</b>
            {latestTapeItems.map((item) => (
              <span className={item.className} key={item.id}>
                <b>{item.stage}</b>
                <strong>{item.symbol}</strong>
                <em>{item.label}</em>
              </span>
            ))}
          </div>
        )}
      </div>
    </section>
  )
}

function getFlowSummary({
  bottleneck,
  conversionRate,
  eventFlow,
  finalStageCount,
  firstStageCount,
  hasEventSource,
  hasHoldingContext,
}: {
  bottleneck: string
  conversionRate: number
  eventFlow: EventFlow | null
  finalStageCount: number
  firstStageCount: number
  hasEventSource: boolean
  hasHoldingContext: boolean
}) {
  if (hasHoldingContext) {
    return {
      detail: '等待下一条机会进入',
      label: '管道状态',
      value: '空闲',
    }
  }

  if (hasEventSource && eventFlow) {
    return {
      detail: bottleneck,
      label: '本轮机会',
      value: `${eventFlow.outcomes.executed}/${eventFlow.total} 成交`,
    }
  }

  return {
    detail: bottleneck,
    label: '筛选保留',
    value: `${finalStageCount}/${firstStageCount} · ${conversionRate}%`,
  }
}

function getLossRows(stages: Array<{ count: number; dropped: number; label: string }>) {
  return stages.slice(1).map((stage) => ({
    count: stage.dropped,
    label: stage.label,
  }))
}

function getHoldingSummary(holdings: HoldingRow[]) {
  return {
    positive: holdings.filter((holding) => isPositivePnl(holding.pnl)).length,
    watching: holdings.filter((holding) => holding.risk === '观察' || holding.risk === '偏高').length,
  }
}

function getHoldingBottleneck(holdings: HoldingRow[], summary: ReturnType<typeof getHoldingSummary>) {
  const negative = holdings.filter((holding) => !isPositivePnl(holding.pnl)).length
  if (summary.watching > 0) return `${summary.watching} 个持仓需要观察`
  if (summary.positive > 0) return `${summary.positive} 个持仓正贡献`
  if (negative > 0) return '当前持仓承压'
  return '持仓状态平稳'
}

function getHoldingLossRows(holdings: HoldingRow[]) {
  const watching = holdings.filter((holding) => holding.risk === '观察' || holding.risk === '偏高').length
  const negative = holdings.filter((holding) => !isPositivePnl(holding.pnl)).length
  return [
    { count: negative, label: '负贡献' },
    { count: watching, label: '需观察' },
  ]
}

function getHoldingVisualStages(holdings: HoldingRow[]) {
  const total = holdings.length
  const positive = holdings.filter((holding) => isPositivePnl(holding.pnl)).length
  const normal = holdings.filter((holding) => holding.risk === '正常').length
  const watching = holdings.filter((holding) => holding.risk === '观察' || holding.risk === '偏高').length
  const contributing = Math.max(positive, total - watching)
  const queue = Math.max(1, contributing)
  const stages = [
    { count: total, label: '持仓跟踪' },
    { count: normal, label: '状态正常' },
    { count: positive, label: '正贡献' },
    { count: watching, label: '需要观察' },
    { count: queue, label: '继续跟进' },
  ]

  return stages.map((stage, index) => {
    const previousCount = index === 0 ? stage.count : stages[index - 1].count
    const dropped = index === 0 ? 0 : Math.max(0, previousCount - stage.count)
    return {
      count: stage.count,
      dropped,
      hint: index === 0 ? '当前' : stage.count > 0 ? '保留' : '等待',
      label: stage.label,
      width: Math.max(18, Math.round((stage.count / Math.max(1, total)) * 100)),
    }
  })
}

function getHoldingOutcomeRows(holdings: HoldingRow[]) {
  const positive = holdings.filter((holding) => isPositivePnl(holding.pnl)).length
  const watching = holdings.filter((holding) => holding.risk === '观察' || holding.risk === '偏高').length
  const neutral = Math.max(0, holdings.length - positive - watching)
  return [positive, neutral, watching, 0]
}

type EventFlow = {
  total: number
  stages: Record<(typeof FLOW_STAGES)[number]['label'], number>
  outcomes: {
    executed: number
    pending: number
    review: number
    abandoned: number
  }
}

type FlowTapeItem = {
  className: string
  id: string
  label: string
  stage: string
  symbol: string
}

function getEventFlow(events: FunnelEvent[]): EventFlow | null {
  if (!events.length) return null

  const bySignal = events.reduce<Record<string, FunnelEvent[]>>((groups, event) => {
    const key = `${event.market}:${event.symbol}`
    groups[key] = [...(groups[key] ?? []), event]
    return groups
  }, {})

  const signals = Object.values(bySignal)
  const hasStage = (rows: FunnelEvent[], stage: FunnelEvent['stage']) => rows.some((event) => event.stage === stage)
  const hasAnyStage = (rows: FunnelEvent[], stages: FunnelEvent['stage'][]) => stages.some((stage) => hasStage(rows, stage))
  const resultRows = signals
    .map((rows) => rows.find((event) => event.stage === '结果'))
    .filter((row): row is FunnelEvent => Boolean(row))

  return {
    total: signals.length,
    stages: {
      发现: signals.filter((rows) => hasStage(rows, '发现')).length,
      初筛: signals.filter((rows) => hasAnyStage(rows, ['研判', '风控', '队列', '结果'])).length,
      研究: signals.filter((rows) => hasStage(rows, '研判')).length,
      风控: signals.filter((rows) => hasStage(rows, '风控')).length,
      信号: signals.filter((rows) => hasStage(rows, '队列')).length,
    },
    outcomes: {
      executed: resultRows.filter((event) => event.status === '成交').length,
      pending: resultRows.filter((event) => event.status === '等待' || event.status === '机会').length,
      review: resultRows.filter((event) => event.status === '复盘').length,
      abandoned: resultRows.filter((event) => event.status === '拦截').length,
    },
  }
}

function getVisualStages(funnel: ReturnType<typeof getSignalFunnel>, eventFlow: EventFlow | null) {
  return FLOW_STAGES.map((stage, index) => {
    const fallbackCount = funnel.stages[stage.fallbackIndex]?.rows.length ?? 0
    const count = eventFlow ? eventFlow.stages[stage.label] : fallbackCount
    const previousCount = index === 0
      ? count
      : eventFlow
        ? eventFlow.stages[FLOW_STAGES[index - 1].label]
        : funnel.stages[FLOW_STAGES[index - 1].fallbackIndex]?.rows.length ?? 0
    const dropped = index === 0 ? 0 : Math.max(0, previousCount - count)

    return {
      count,
      dropped,
      hint: eventFlow ? getEventStageHint(stage.label, count, eventFlow.total, dropped) : getStageHint(index, count, funnel.stages[0]?.rows.length ?? 0, dropped),
      label: stage.label,
      width: eventFlow ? eventStageWidth(count, eventFlow.total) : Math.max(12, Math.round((count / Math.max(1, funnel.stages[0]?.rows.length ?? 1)) * 100)),
    }
  })
}

function getOutcomeRows(eventFlow: EventFlow | null, fallback: { executed: number; pending: number; missed: number; abandoned: number }) {
  if (!eventFlow) return [fallback.executed, fallback.pending, fallback.missed, fallback.abandoned]

  return [
    eventFlow.outcomes.executed,
    eventFlow.outcomes.pending,
    eventFlow.outcomes.review,
    eventFlow.outcomes.abandoned,
  ]
}

function eventStageWidth(count = 0, total: number) {
  return Math.max(8, Math.min(100, Math.round((count / Math.max(1, total)) * 140)))
}

function getEventStageHint(label: string, count: number, total: number, dropped: number) {
  if (count <= 0) return '等待'
  if (label === '发现') return '进入'
  if (label === '信号') return '准备'
  if (dropped > 0) return `留下 ${count}`
  return `${Math.round((count / Math.max(1, total)) * 100)}%`
}

function getRailRows(stages: Array<{ count: number; dropped: number; label: string }>, outcomes: number[]) {
  const offsets = [-24, -11, 0, 11, 24]
  return stages.map((stage, index) => ({
    count: stage.count,
    dropped: stage.dropped,
    label: stage.label,
    offset: offsets[index] ?? 0,
    tone: index === stages.length - 1 && outcomes[0] > 0 ? 'cyan' : stage.dropped > 0 ? 'amber' : 'muted',
  }))
}

function getBottleneck(stages: { label: string; count: number }[]) {
  if (!stages.length || stages[0].count === 0) return '等待机会进入'
  const drops = stages.slice(1).map((stage, index) => ({
    from: stages[index].label,
    to: stage.label,
    drop: Math.max(0, stages[index].count - stage.count),
  }))
  const biggest = drops.sort((a, b) => b.drop - a.drop)[0]
  if (!biggest || biggest.drop === 0) return '当前全部通过'
  return `${biggest.from}→${biggest.to} 筛掉 ${biggest.drop} 条`
}

function getDropHeight(drop: number, total: number) {
  if (drop <= 0 || total <= 0) return 7
  return Math.min(100, Math.max(12, Math.round((drop / total) * 100)))
}

function getStageHint(index: number, count: number, total: number, dropped: number) {
  if (index === 0) return '进入'
  if (total <= 0) return '等待'
  if (dropped > 0) return `留下 ${count}`
  if (count === total) return '全量通过'
  return `${Math.round((count / total) * 100)}%`
}

function buildParticles(rows: SignalRow[], mode: ReturnType<typeof getSignalFunnel>['mode']) {
  const visibleRows = rows.slice(0, MAX_ANIMATED_SIGNALS)
  const labelIndexes = pickLabelIndexes(visibleRows)

  return visibleRows.map((signal, index) => {
    const stopStage = mode === 'replay' ? 5 : getStopStage(signal)
    const tone = signal.status === 'blocked' || signal.status === 'cancelled'
      ? 'red'
      : signal.status === 'missed'
        ? 'amber'
        : signal.status === 'pending'
          ? 'muted'
          : 'cyan'

    return {
      begin: `${-(index * 0.52)}s`,
      duration: `${6.4 + (index % 6) * 0.42}s`,
      label: signal.symbol,
      lane: index % 9,
      showLabel: labelIndexes.has(index),
      symbol: compactSymbol(signal.symbol),
      stopLeft: `${Math.min(93, stopStage * 20 - 8)}%`,
      tone,
    }
  })
}

function buildEventParticles(events: FunnelEvent[]) {
  const visibleEvents = events.slice(-MAX_ANIMATED_SIGNALS)
  const labelIndexes = pickEventLabelIndexes(visibleEvents)

  return visibleEvents.map((event, index) => {
    const stopStage = eventStageToStop(event.stage)
    const tone = event.status === '拦截' || event.status === '复盘'
      ? 'red'
      : event.status === '机会'
        ? 'amber'
        : event.status === '等待'
          ? 'muted'
          : 'cyan'

    return {
      begin: `${-(index * 0.36)}s`,
      duration: `${5.6 + (index % 7) * 0.34}s`,
      label: `${event.symbol}-${event.stage}`,
      lane: index % 10,
      showLabel: labelIndexes.has(index),
      symbol: compactSymbol(event.symbol),
      stopLeft: `${Math.min(93, stopStage * 20 - 8)}%`,
      tone,
    }
  })
}

function buildHoldingParticles(holdings: HoldingRow[]) {
  return holdings.slice(0, MAX_ANIMATED_SIGNALS).map((holding, index) => {
    const tone = holding.risk === '偏高'
      ? 'red'
      : holding.risk === '观察'
        ? 'amber'
        : isPositivePnl(holding.pnl)
          ? 'cyan'
          : 'muted'

    return {
      begin: `${-(index * 0.74)}s`,
      duration: `${6.6 + (index % 5) * 0.48}s`,
      label: holding.symbol,
      lane: index % 8,
      showLabel: true,
      symbol: compactSymbol(holding.symbol),
      stopLeft: holding.risk === '偏高' ? '68%' : '92%',
      tone,
    }
  })
}

function getHoldingTape(holdings: HoldingRow[]): FlowTapeItem[] {
  return holdings.slice(0, 4).map((holding, index) => {
    const label = holding.risk === '正常' ? `${holding.pnl} · 正常` : `${holding.pnl} · ${holding.risk}`
    return {
      className: holdingTapeClass(holding),
      id: `holding-${holding.symbol}-${index}`,
      label,
      stage: '持仓',
      symbol: holding.symbol,
    }
  })
}

function holdingTapeClass(holding: HoldingRow) {
  if (holding.risk === '偏高') return 'event-block'
  if (holding.risk === '观察') return 'event-watch'
  return isPositivePnl(holding.pnl) ? 'event-filled' : 'event-review'
}

function isPositivePnl(value: string) {
  return /^\s*\+/.test(value)
}

function pickEventLabelIndexes(events: FunnelEvent[]) {
  const seen = new Set<string>()
  const selected = events
    .map((event, index) => ({ index, label: compactSymbol(event.symbol), priority: eventPriority(event) }))
    .filter((row) => isReadableLabel(row.label))
    .sort((a, b) => b.priority - a.priority || b.index - a.index)
    .filter((row) => {
      if (seen.has(row.label)) return false
      seen.add(row.label)
      return true
    })
    .slice(0, MAX_VISIBLE_LABELS)
    .map((row) => row.index)

  return new Set(selected)
}

function eventPriority(event: FunnelEvent) {
  if (event.status === '机会') return 90
  if (event.status === '拦截') return 80
  if (event.status === '等待') return 60
  if (event.status === '成交') return 40
  return 20
}

function eventStageToStop(stage: FunnelEvent['stage']) {
  if (stage === '结果') return 5
  if (stage === '队列') return 4
  if (stage === '风控') return 3
  if (stage === '研判') return 2
  return 1
}

function pickLabelIndexes(rows: SignalRow[]) {
  const seen = new Set<string>()
  const selected = rows
    .map((signal, index) => ({ index, label: compactSymbol(signal.symbol), priority: labelPriority(signal) }))
    .filter((row) => isReadableLabel(row.label))
    .sort((a, b) => b.priority - a.priority || a.index - b.index)
    .filter((row) => {
      if (seen.has(row.label)) return false
      seen.add(row.label)
      return true
    })
    .slice(0, MAX_VISIBLE_LABELS)
    .map((row) => row.index)

  return new Set(selected)
}

function labelPriority(signal: SignalRow) {
  if (signal.status === 'missed') return 80
  if (signal.status === 'blocked' || signal.status === 'cancelled') return 70
  if (signal.status === 'pending') return 60
  if (signal.stageLatencyMinutes && signal.stageLatencyMinutes > 0) return 40
  return signal.status === 'executed' ? 20 : 10
}

function isReadableLabel(label: string) {
  return label.length >= 3 && !/^0+$/.test(label)
}

function compactSymbol(symbol: string) {
  if (symbol.length <= 8) return symbol
  return symbol.replace(/(\.US|\.SH|\.SZ|\.CFFEX|\.SHFE|\.DCE|\.CZCE|\.INE|\.GFEX|-USD|-PERP)$/i, '').slice(0, 8)
}

function getStopStage(signal: SignalRow) {
  if (signal.status === 'blocked') return 3
  if (signal.status === 'cancelled') return Math.min(4, Math.max(2, signal.steps - 1))
  if (signal.status === 'missed') return 5
  if (signal.status === 'executed') return 5
  if (signal.stage === '待执行') return 4
  if (signal.stage === '风控' || signal.stage === '拒绝') return 3
  if (signal.stage === '评分') return 2
  return Math.min(5, Math.max(1, signal.steps))
}
