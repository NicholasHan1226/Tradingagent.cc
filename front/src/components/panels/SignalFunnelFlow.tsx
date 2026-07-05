import type { CSSProperties } from 'react'
import { getSignalFunnel } from '../../lib/dashboard'
import type { SignalRow } from '../../types/dashboard'

const OUTCOME_LABELS = ['成交', '等待', '复盘', '拦截']
const MAX_ANIMATED_SIGNALS = 64
const MAX_VISIBLE_LABELS = 10

export function SignalFunnelFlow({ hasSignalData, signals }: { hasSignalData: boolean; signals: SignalRow[] }) {
  const funnel = getSignalFunnel(signals)
  const maxStageCount = Math.max(1, ...funnel.stages.map((stage) => stage.rows.length))
  const stageWidths = funnel.stages.map((stage) => Math.max(12, Math.round((stage.rows.length / maxStageCount) * 100)))
  const executedSignals = funnel.executed
  const pendingSignals = funnel.pending
  const missedSignals = funnel.missed
  const blockedSignals = [...funnel.blocked, ...funnel.cancelled]
  const passRate = Math.round((funnel.executed.length / Math.max(1, signals.length)) * 100)
  const stageDrops = funnel.stages.map((stage, index) => {
    if (index === 0) return 0
    return Math.max(0, funnel.stages[index - 1].rows.length - stage.rows.length)
  })
  const hasStageDrop = stageDrops.some((drop) => drop > 0)
  const hasTimingEvidence = signals.some((signal) => signal.stageLatencyMinutes && signal.stageLatencyMinutes > 0)
  const bottleneck = getBottleneck(funnel.stages.map((stage) => ({ label: stage.label, count: stage.rows.length })))
  const caption = hasSignalData
    ? hasStageDrop || hasTimingEvidence || funnel.executed.length !== signals.length
      ? `${signals.length} 个进入 · ${funnel.tradeSignals.length} 个留下 · ${funnel.executed.length} 个成交`
      : `${signals.length} 条成交回放 · 转化 ${passRate}%`
    : '等待机会流入'
  const particles = buildParticles(hasSignalData ? signals : placeholderSignals())

  return (
    <section className="signal-flow-module" aria-label="交易漏斗">
      <div className="signal-flow-board">
        <div className="flow-caption">
          <span>交易漏斗</span>
          <strong>{caption}</strong>
        </div>
        <div className="funnel-pipeline" role="img" aria-label="机会从发现到交易结果的动态筛选漏斗">
          <div className="funnel-stage-grid" aria-hidden="true">
            {funnel.stages.map((stage, index) => (
              <div className="funnel-stage-card" key={stage.label}>
                <span>{stage.label}</span>
                <strong>{stage.rows.length}</strong>
                <em>{getStageHint(index, stage.rows.length, signals.length, stageDrops[index])}</em>
                <div className="stage-meter" aria-hidden="true">
                  <i style={{ width: `${stageWidths[index]}%` }} />
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
      </div>
    </section>
  )
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
  return `${biggest.from}到${biggest.to}少了 ${biggest.drop} 条`
}

function getStageHint(index: number, count: number, total: number, dropped: number) {
  if (index === 0) return '进入'
  if (total <= 0) return '等待'
  if (dropped > 0) return `留下 ${count}`
  if (count === total) return '全量通过'
  return `${Math.round((count / total) * 100)}%`
}

function buildParticles(rows: SignalRow[]) {
  const visibleRows = rows.slice(0, MAX_ANIMATED_SIGNALS)
  const labelIndexes = new Set(
    visibleRows
      .map((signal, index) => ({ index, priority: labelPriority(signal) }))
      .sort((a, b) => b.priority - a.priority || a.index - b.index)
      .slice(0, MAX_VISIBLE_LABELS)
      .map((row) => row.index),
  )

  return visibleRows.map((signal, index) => {
    const stopStage = getStopStage(signal)
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

function labelPriority(signal: SignalRow) {
  if (signal.status === 'missed') return 80
  if (signal.status === 'blocked' || signal.status === 'cancelled') return 70
  if (signal.status === 'pending') return 60
  if (signal.stageLatencyMinutes && signal.stageLatencyMinutes > 0) return 40
  return signal.status === 'executed' ? 20 : 10
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
