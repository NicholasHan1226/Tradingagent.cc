import type { CSSProperties } from 'react'
import { getSignalFunnel } from '../../lib/dashboard'
import type { FunnelEvent, HoldingRow, SignalRow } from '../../types/dashboard'

const OUTCOME_LABELS = ['成交', '观察中', '复盘', '放弃']
const HOLDING_OUTCOME_LABELS = ['持仓', '观察', '复盘', '放弃']
const MAX_ANIMATED_SIGNALS = 64
const MAX_VISIBLE_LABELS = 6
const FLOW_STAGES = [
  { label: '机会进入', fallbackIndex: 0 },
  { label: '初筛', fallbackIndex: 1 },
  { label: '研究', fallbackIndex: 1 },
  { label: '风控', fallbackIndex: 2 },
  { label: '进入队列', fallbackIndex: 3 },
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
  const hasHoldingReplay = !events.length && !signals.length && holdings.length > 0
  const visualStages = hasHoldingReplay ? getHoldingStages(holdings) : getVisualStages(funnel, eventFlow)
  const maxStageCount = Math.max(1, ...visualStages.map((stage) => stage.count))
  const stageWidths = visualStages.map((stage) => Math.max(12, Math.round((stage.count / maxStageCount) * 100)))
  const executedSignals = funnel.executed
  const pendingSignals = funnel.pending
  const missedSignals = funnel.missed
  const blockedSignals = [...funnel.blocked, ...funnel.cancelled]
  const outcomeRows = hasHoldingReplay ? getHoldingOutcomeRows(holdings) : getOutcomeRows(eventFlow, {
    executed: executedSignals.length,
    pending: pendingSignals.length,
    missed: missedSignals.length,
    abandoned: blockedSignals.length,
  })
  const outcomeLabels = hasHoldingReplay ? HOLDING_OUTCOME_LABELS : OUTCOME_LABELS
  const passRate = Math.round((funnel.executed.length / Math.max(1, signals.length)) * 100)
  const visualStageDrops = visualStages.map((stage) => stage.dropped)
  const hasStageDrop = visualStageDrops.some((drop) => drop > 0)
  const hasTimingEvidence = signals.some((signal) => signal.stageLatencyMinutes && signal.stageLatencyMinutes > 0)
  const bottleneck = getBottleneck(visualStages.map((stage) => ({ label: stage.label, count: stage.count })))
  const hasEventSource = events.length > 0
  const eventCaption = eventFlow
    ? `${eventFlow.total} 个机会进入 · ${eventFlow.outcomes.executed} 个成交 · ${eventFlow.outcomes.abandoned} 个放弃`
    : '等待新机会'
  const caption = hasEventSource
    ? eventCaption
    : hasHoldingReplay
      ? `${holdings.length} 个持仓在跟踪 · 暂无新信号进入`
    : hasSignalData
    ? funnel.mode === 'screening' || hasStageDrop || hasTimingEvidence || funnel.executed.length !== signals.length
      ? `${signals.length} 个进入 · ${funnel.tradeSignals.length} 个留下 · ${funnel.executed.length} 个成交`
      : `${signals.length} 条成交回放 · 转化 ${passRate}%`
    : '等待新机会'
  const modeLabel = hasEventSource
    ? '实时'
    : hasHoldingReplay
      ? '持仓中'
    : funnel.mode === 'screening'
    ? '筛选中'
    : funnel.mode === 'partial'
      ? '进行中'
      : funnel.mode === 'replay'
        ? '已完成'
        : '等待数据'
  const particles = hasEventSource
    ? buildEventParticles(events)
    : hasHoldingReplay
      ? buildHoldingParticles(holdings)
    : buildParticles(hasSignalData ? signals : placeholderSignals(), funnel.mode)
  const latestEvents = events.slice(-4).reverse()
  const firstStageCount = Math.max(1, visualStages[0]?.count ?? signals.length)
  const finalStageCount = visualStages.at(-1)?.count ?? 0
  const conversionRate = Math.round((finalStageCount / firstStageCount) * 100)

  return (
    <section className="signal-flow-module" aria-label="机会管道">
      <div className={`signal-flow-board real-funnel-board mode-${funnel.mode} ${hasEventSource ? 'mode-real-flow' : ''}`}>
        <div className="flow-caption">
          <span>机会管道 <b>{modeLabel}</b></span>
          <strong>{caption} · 转化 {conversionRate}%</strong>
        </div>
        <div className="real-funnel-stage-grid" aria-hidden="true">
          {visualStages.map((stage, index) => (
            <div className="real-funnel-stage-card" key={stage.label}>
              <span>{stage.label}</span>
              <strong>{stage.count}</strong>
              <em>{index === 0 ? '进入' : stage.dropped > 0 ? `减少 ${stage.dropped}` : stage.hint}</em>
            </div>
          ))}
        </div>
        <div className="real-funnel-body" role="img" aria-label="机会从发现到交易结果的动态筛选漏斗">
          <div className="real-funnel-rulers" aria-hidden="true">
            {visualStages.map((stage, index) => (
              <i key={stage.label} style={{ left: `${index * 20}%` }} />
            ))}
          </div>
          <div className="real-funnel-channel" aria-hidden="true">
            {visualStages.map((stage, index) => (
              <span
                className="real-funnel-segment"
                key={stage.label}
                style={{
                  '--segment-left': `${index * 20}%`,
                  '--segment-width': `${Math.max(10, stageWidths[index] * 0.56)}%`,
                } as CSSProperties}
              />
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
        </div>
        <div className="flow-outcome-strip">
          {outcomeRows.map((count, index) => (
            <span key={outcomeLabels[index]}>
              <b>{count}</b> {outcomeLabels[index]}
            </span>
          ))}
        </div>
        <div className="flow-bottleneck">
          <span>筛选结果</span>
          <strong>{bottleneck}</strong>
        </div>
        {latestEvents.length > 0 && (
          <div className="flow-event-tape" aria-label="最近管道事件">
            <b className="event-tape-label">最新流动</b>
            {latestEvents.map((event) => (
              <span className={eventStatusClass(event.status)} key={event.id}>
                <b>{event.stage}</b>
                <strong>{event.symbol}</strong>
                <em>{event.label}</em>
              </span>
            ))}
          </div>
        )}
      </div>
    </section>
  )
}

function getHoldingStages(holdings: HoldingRow[]) {
  const observing = holdings.filter((holding) => holding.risk === '观察' || holding.risk === '偏高').length
  const normal = holdings.length - observing

  return [
    { count: holdings.length, dropped: 0, hint: '账户内', label: '已有仓位', width: 100 },
    { count: holdings.length, dropped: 0, hint: '已建仓', label: '入仓结果', width: 100 },
    { count: holdings.length, dropped: 0, hint: '跟踪中', label: '持仓跟踪', width: 100 },
    { count: observing, dropped: Math.max(0, normal), hint: observing ? '需观察' : '正常', label: '风险观察', width: observing ? 56 : 16 },
    { count: holdings.length, dropped: 0, hint: '待复盘', label: '复盘归因', width: 100 },
  ]
}

function getHoldingOutcomeRows(holdings: HoldingRow[]) {
  const watching = holdings.filter((holding) => holding.risk === '观察' || holding.risk === '偏高').length
  return [holdings.length, watching, holdings.length, 0]
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
      机会进入: signals.filter((rows) => hasStage(rows, '发现')).length,
      初筛: signals.filter((rows) => hasAnyStage(rows, ['研判', '风控', '队列', '结果'])).length,
      研究: signals.filter((rows) => hasStage(rows, '研判')).length,
      风控: signals.filter((rows) => hasStage(rows, '风控')).length,
      进入队列: signals.filter((rows) => hasStage(rows, '队列')).length,
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
  if (label === '机会进入') return '进入'
  if (label === '进入队列') return '准备'
  if (dropped > 0) return `留下 ${count}`
  return `${Math.round((count / Math.max(1, total)) * 100)}%`
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
    const tone = holding.risk === '偏高' ? 'red' : holding.risk === '观察' ? 'amber' : 'cyan'

    return {
      begin: `${-(index * 0.58)}s`,
      duration: `${6.2 + (index % 5) * 0.4}s`,
      label: `${holding.symbol}-holding`,
      lane: index % 8,
      showLabel: true,
      symbol: compactSymbol(holding.symbol),
      stopLeft: holding.risk === '正常' ? '88%' : '68%',
      tone,
    }
  })
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

function eventStatusClass(status: FunnelEvent['status']) {
  if (status === '成交') return 'event-filled'
  if (status === '机会') return 'event-opportunity'
  if (status === '拦截') return 'event-block'
  if (status === '复盘') return 'event-review'
  return 'event-watch'
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

function placeholderSignals(): SignalRow[] {
  return Array.from({ length: 12 }, (_, index) => ({
    symbol: '',
    name: '',
    market: 'All Markets',
    method: '',
    status: 'pending',
    impact: '',
    confidence: '',
    age: '',
    reason: '',
    next: '',
    steps: 1 + (index % 4),
  }))
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
