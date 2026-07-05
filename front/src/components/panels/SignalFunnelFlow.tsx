import type { CSSProperties } from 'react'
import { getSignalFunnel } from '../../lib/dashboard'
import type { SignalRow } from '../../types/dashboard'

const STAGE_NAMES = ['发现', '评分', '风控', '待执行', '结果']

export function SignalFunnelFlow({ hasSignalData, signals }: { hasSignalData: boolean; signals: SignalRow[] }) {
  const funnel = getSignalFunnel(signals)
  const maxStageCount = Math.max(1, ...funnel.stages.map((stage) => stage.rows.length))
  const stageWidths = funnel.stages.map((stage) => Math.max(12, Math.round((stage.rows.length / maxStageCount) * 100)))
  const executedSignals = funnel.executed
  const pendingSignals = funnel.pending
  const missedSignals = funnel.missed
  const blockedSignals = [...funnel.blocked, ...funnel.cancelled]
  const passRate = Math.round((funnel.tradeSignals.length / Math.max(1, signals.length)) * 100)
  const caption = hasSignalData
    ? `${signals.length} 个机会进入 · ${funnel.tradeSignals.length} 个形成结果 · 留存 ${passRate}%`
    : '等待机会流入'
  const particles = buildParticles(hasSignalData ? signals : placeholderSignals())

  return (
    <section className="signal-flow-module" aria-label="机会漏斗">
      <div className="signal-flow-board">
        <div className="flow-caption">
          <span>机会漏斗</span>
          <strong>{caption}</strong>
        </div>
        <div className="funnel-pipeline" role="img" aria-label="机会从发现到交易结果的动态筛选漏斗">
          <div className="funnel-stage-grid" aria-hidden="true">
            {funnel.stages.map((stage, index) => (
              <div className="funnel-stage-card" key={stage.label}>
                <span>{STAGE_NAMES[index] ?? stage.label}</span>
                <strong>{stage.rows.length}</strong>
                <em>{index === 0 ? '进入' : `${stageWidths[index]}%`}</em>
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
                className={`funnel-particle ${particle.tone}`}
                key={`${particle.label}-${index}`}
                style={{
                  '--delay': particle.begin,
                  '--duration': particle.duration,
                  '--lane': particle.lane,
                  '--to-left': particle.stopLeft,
                } as CSSProperties}
                title={particle.label}
              >
                <b />
              </i>
            ))}
          </div>
        </div>
        <div className="flow-outcome-strip">
          <span><b>{executedSignals.length}</b> 已成交</span>
          <span><b>{pendingSignals.length}</b> 待执行</span>
          <span><b>{missedSignals.length}</b> 机会复盘</span>
          <span><b>{blockedSignals.length}</b> 已拦截</span>
        </div>
      </div>
    </section>
  )
}

function buildParticles(rows: SignalRow[]) {
  return rows.slice(0, 42).map((signal, index) => {
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
      duration: `${6.2 + (index % 5) * 0.46}s`,
      label: signal.symbol,
      lane: index % 7,
      stopLeft: `${Math.min(93, stopStage * 20 - 8)}%`,
      tone,
    }
  })
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
