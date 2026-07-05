import type { CSSProperties } from 'react'
import { getSignalFunnel } from '../../lib/dashboard'
import type { FunnelEvent, SignalRow } from '../../types/dashboard'

const OUTCOME_LABELS = ['成交', '等待', '复盘', '拦截']
const MAX_ANIMATED_SIGNALS = 64
const MAX_VISIBLE_LABELS = 6

export function SignalFunnelFlow({ events, hasSignalData, signals }: { events: FunnelEvent[]; hasSignalData: boolean; signals: SignalRow[] }) {
  const funnel = getSignalFunnel(signals)
  const eventStages = getEventStageCounts(events)
  const maxStageCount = Math.max(1, ...funnel.stages.map((stage) => stage.rows.length))
  const stageWidths = funnel.stages.map((stage) => Math.max(12, Math.round((stage.rows.length / maxStageCount) * 100)))
  const executedSignals = funnel.executed
  const pendingSignals = funnel.pending
  const missedSignals = funnel.missed
  const blockedSignals = [...funnel.blocked, ...funnel.cancelled]
  const passRate = Math.round((funnel.executed.length / Math.max(1, signals.length)) * 100)
  const stageDrops = funnel.stageDrops
  const hasStageDrop = stageDrops.some((drop) => drop > 0)
  const hasTimingEvidence = signals.some((signal) => signal.stageLatencyMinutes && signal.stageLatencyMinutes > 0)
  const bottleneck = getBottleneck(funnel.stages.map((stage) => ({ label: stage.label, count: stage.rows.length })))
  const hasEventSource = events.length > 0
  const caption = hasEventSource
    ? `${events.length} 个管道事件 · ${signals.length} 个机会 · ${funnel.executed.length} 个已兑现`
    : hasSignalData
    ? funnel.mode === 'screening' || hasStageDrop || hasTimingEvidence || funnel.executed.length !== signals.length
      ? `${signals.length} 个进入 · ${funnel.tradeSignals.length} 个留下 · ${funnel.executed.length} 个成交`
      : `${signals.length} 条成交回放 · 转化 ${passRate}%`
    : '等待机会流入'
  const modeLabel = funnel.mode === 'screening'
    ? '实时筛选'
    : funnel.mode === 'partial'
      ? '部分阶段'
      : funnel.mode === 'replay'
        ? '成交回放'
        : '等待数据'
  const particles = hasEventSource
    ? buildEventParticles(events)
    : buildParticles(hasSignalData ? signals : placeholderSignals(), funnel.mode)
  const latestEvents = events.slice(-4).reverse()

  return (
    <section className="signal-flow-module" aria-label="交易漏斗">
      <div className={`signal-flow-board mode-${funnel.mode}`}>
        <div className="flow-caption">
          <span>交易漏斗 <b>{modeLabel}</b></span>
          <strong>{caption}</strong>
        </div>
        <div className="funnel-pipeline" role="img" aria-label="机会从发现到交易结果的动态筛选漏斗">
          <div className="funnel-stage-grid" aria-hidden="true">
            {funnel.stages.map((stage, index) => (
              <div className="funnel-stage-card" key={stage.label} style={{ '--stage-strength': `${stageWidths[index]}%` } as CSSProperties}>
                <span>{stage.label}</span>
                <strong>{eventStages[stage.label] ?? stage.rows.length}</strong>
                <em>{hasEventSource ? '事件进入' : getStageHint(index, stage.rows.length, signals.length, stageDrops[index])}</em>
                <div className="stage-meter" aria-hidden="true">
                  <i style={{ width: `${hasEventSource ? eventStageWidth(eventStages[stage.label], events.length) : stageWidths[index]}%` }} />
                </div>
              </div>
            ))}
          </div>
          <div className="funnel-flow-canvas" aria-hidden="true">
            {!hasSignalData && <div className="funnel-scanner" />}
            <div className="funnel-mouth">
              {stageWidths.map((width, index) => (
                <span
                  key={funnel.stages[index]?.label ?? index}
                  style={{
                    '--stage-offset': `${index * 20}%`,
                    '--stage-width': `${width}%`,
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
        </div>
        <div className="flow-drop-track" aria-hidden="true">
          {stageDrops.slice(1).map((drop, index) => (
            <span className={drop > 0 ? 'has-drop' : ''} key={`${funnel.stages[index].label}-${funnel.stages[index + 1].label}`}>
              <i style={{ height: `${Math.min(100, Math.max(8, drop * 12))}%` }} />
            </span>
          ))}
        </div>
        <div className="flow-outcome-strip">
          {[executedSignals, pendingSignals, missedSignals, blockedSignals].map((rows, index) => (
            <span key={OUTCOME_LABELS[index]}>
              <b>{rows.length}</b> {OUTCOME_LABELS[index]}
            </span>
          ))}
        </div>
        <div className="flow-bottleneck">
          <span>瓶颈</span>
          <strong>{bottleneck}</strong>
        </div>
        {latestEvents.length > 0 && (
          <div className="flow-event-tape" aria-label="最近管道事件">
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

function getEventStageCounts(events: FunnelEvent[]) {
  return events.reduce<Record<string, number>>((counts, event) => {
    counts[event.stage] = (counts[event.stage] ?? 0) + 1
    return counts
  }, {})
}

function eventStageWidth(count = 0, total: number) {
  return Math.max(8, Math.min(100, Math.round((count / Math.max(1, total)) * 140)))
}

function getBottleneck(stages: { label: string; count: number }[]) {
  if (!stages.length || stages[0].count === 0) return '等待机会进入'
  const drops = stages.slice(1).map((stage, index) => ({
    from: stages[index].label,
    to: stage.label,
    drop: Math.max(0, stages[index].count - stage.count),
  }))
  const biggest = drops.sort((a, b) => b.drop - a.drop)[0]
  if (!biggest || biggest.drop === 0) return '当前全量通过'
  return `${biggest.from}到${biggest.to}减少 ${biggest.drop} 条`
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
    symbol: `WAIT-${index}`,
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
