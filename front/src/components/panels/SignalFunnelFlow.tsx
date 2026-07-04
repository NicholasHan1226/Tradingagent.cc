import type { CSSProperties } from 'react'
import { getSignalFunnel } from '../../lib/dashboard'
import type { SignalRow } from '../../types/dashboard'

export function SignalFunnelFlow({ hasSignalData, signals }: { hasSignalData: boolean; signals: SignalRow[] }) {
  const funnel = getSignalFunnel(signals)
  const stageNames = ['发现', '成形', '条件', '风控', '信号']
  const maxStageCount = Math.max(1, ...funnel.stages.map((stage) => stage.rows.length))
  const executedSignals = funnel.executed
  const pendingSignals = funnel.pending
  const missedSignals = funnel.missed
  const blockedSignals = [...funnel.blocked, ...funnel.cancelled]
  const particles = hasSignalData ? signals.slice(0, 36).map((signal, index) => {
    const stopStage = getStopStage(signal)
    const tone = signal.status === 'blocked' || signal.status === 'cancelled'
      ? 'red'
      : signal.status === 'missed'
        ? 'amber'
        : 'cyan'

    return {
      begin: `${-(index * 0.58)}s`,
      duration: `${6.6 + (index % 4) * 0.55}s`,
      label: signal.symbol,
      lane: index % 6,
      stopStage,
      stopLeft: `${Math.min(93, stopStage * 20 - 7)}%`,
      tone,
    }
  }) : []
  const passRate = Math.round((funnel.tradeSignals.length / Math.max(1, signals.length)) * 100)
  const caption = hasSignalData
    ? `${signals.length} 个机会进入，${funnel.tradeSignals.length} 个形成交易信号 · 留存 ${passRate}%`
    : '机会通道已连接，等待新的市场机会进入'

  return (
    <section className="signal-flow-module" aria-label="机会漏斗">
      <div className="signal-flow-board">
        <div className="flow-caption">
          <span>机会漏斗</span>
          <strong>{caption}</strong>
        </div>
        <div className="funnel-pipeline" role="img" aria-label="机会从发现到交易信号的动态漏斗">
          <div className="funnel-stage-grid">
            {funnel.stages.map((stage, index) => {
              const retained = Math.round((stage.rows.length / maxStageCount) * 100)
              return (
                <div className="funnel-stage-card" key={stage.label}>
                  <span>{stageNames[index] ?? stage.label}</span>
                  <strong>{stage.rows.length}</strong>
                  <div className="stage-meter" aria-hidden="true">
                    <i style={{ width: `${retained}%` }} />
                  </div>
                </div>
              )
            })}
          </div>
          <div className="funnel-flow-canvas" aria-hidden="true">
            {!hasSignalData && <div className="funnel-scanner" />}
            <div className="funnel-flow-band">
              <span style={{ '--retained': `${Math.round((funnel.stages[1]?.rows.length ?? 0) / maxStageCount * 100)}%` } as CSSProperties} />
              <span style={{ '--retained': `${Math.round((funnel.stages[2]?.rows.length ?? 0) / maxStageCount * 100)}%` } as CSSProperties} />
              <span style={{ '--retained': `${Math.round((funnel.stages[3]?.rows.length ?? 0) / maxStageCount * 100)}%` } as CSSProperties} />
              <span style={{ '--retained': `${Math.round((funnel.stages[4]?.rows.length ?? 0) / maxStageCount * 100)}%` } as CSSProperties} />
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
          <span><b>{executedSignals.length}</b> 已兑现</span>
          <span><b>{pendingSignals.length}</b> 推进中</span>
          <span><b>{missedSignals.length}</b> 机会复盘</span>
          <span><b>{blockedSignals.length}</b> 风控拦截</span>
        </div>
      </div>
    </section>
  )
}

function getStopStage(signal: SignalRow) {
  if (signal.status === 'blocked') return 3
  if (signal.status === 'cancelled') return Math.min(4, Math.max(2, signal.steps - 1))
  if (signal.status === 'missed') return 5
  if (signal.steps >= 6) return 5
  return Math.min(5, Math.max(1, signal.steps))
}
