import { getSignalFunnel } from '../../lib/dashboard'
import type { SignalRow } from '../../types/dashboard'

export function SignalFunnelFlow({ signals }: { signals: SignalRow[] }) {
  const funnel = getSignalFunnel(signals)
  const stageX = [80, 222, 364, 506, 648]
  const funnelWidths = [174, 142, 112, 86, 58]
  const mainPath = 'M 76 118 C 196 118, 252 118, 362 118 C 472 118, 520 118, 648 118'
  const observedPath = 'M 648 118 C 690 92, 718 72, 756 62'
  const executedPath = 'M 648 118 C 698 116, 722 116, 760 116'
  const missedPath = 'M 648 118 C 692 148, 720 166, 758 178'
  const blockedPath = 'M 504 118 C 552 152, 582 178, 626 202'
  const executedSignals = funnel.executed
  const pendingSignals = funnel.pending
  const missedSignals = funnel.missed
  const blockedSignals = [...funnel.blocked, ...funnel.cancelled]
  const particles = signals.slice(0, 36).map((signal, index) => {
    const path = signal.status === 'executed'
      ? executedPath
      : signal.status === 'pending'
        ? observedPath
        : signal.status === 'blocked' || signal.status === 'cancelled'
      ? blockedPath
      : signal.status === 'missed'
        ? missedPath
        : mainPath

    return {
      begin: `${-(index * 0.58)}s`,
      label: signal.symbol.replace('.US', '').replace('-USD', ''),
      path,
      tone: signal.status === 'blocked' || signal.status === 'cancelled' ? 'red' : signal.status === 'missed' ? 'amber' : 'cyan',
    }
  })

  return (
    <section className="signal-flow-module" aria-label="机会漏斗">
      <div className="signal-flow-board">
        <div className="flow-caption">
          <span>机会漏斗</span>
          <strong>{signals.length} 个机会进入，{pendingSignals.length} 个进入观察</strong>
        </div>
        <svg className="signal-funnel-svg" viewBox="0 0 780 246" role="img" aria-label="机会从发现到交易信号的动态漏斗">
          <defs>
            <linearGradient id="mainRibbon" x1="0%" x2="100%" y1="0%" y2="0%">
              <stop offset="0%" stopColor="rgba(105, 228, 215, 0.24)" />
              <stop offset="62%" stopColor="rgba(105, 228, 215, 0.11)" />
              <stop offset="100%" stopColor="rgba(105, 228, 215, 0.055)" />
            </linearGradient>
          </defs>
          <g className="funnel-stages">
            {funnel.stages.map((stage, index) => (
              <g className="funnel-stage-node" key={stage.label}>
                <line x1={stageX[index]} x2={stageX[index]} y1="58" y2="184" />
                <path
                  className="funnel-stage-shape"
                  d={`M ${stageX[index] - funnelWidths[index] / 2} 86 L ${stageX[index] + funnelWidths[index] / 2} 86 L ${stageX[index] + funnelWidths[index + 1] / 2 || stageX[index] + 18} 150 L ${stageX[index] - (funnelWidths[index + 1] / 2 || 18)} 150 Z`}
                />
                <text className="funnel-label" x={stageX[index]} y="30">{stage.label}</text>
                <text className="funnel-count" x={stageX[index]} y="54">{stage.rows.length}</text>
              </g>
            ))}
          </g>
          <path className="funnel-main-ribbon" d={mainPath} />
          <path className="funnel-main-line" d={mainPath} />
          <path className="funnel-branch cyan" d={observedPath} />
          <path className="funnel-branch cyan" d={executedPath} />
          <path className="funnel-branch amber" d={missedPath} />
          <path className="funnel-branch red" d={blockedPath} />
          {particles.map((particle, index) => (
            <g className={`signal-particle ${particle.tone}`} key={`${particle.label}-${index}`}>
              <circle r="4" />
              <text x="9" y="3">{particle.label}</text>
              <animateMotion begin={particle.begin} dur="7.2s" path={particle.path} repeatCount="indefinite" />
            </g>
          ))}
          <g className="flow-output main observed">
            <rect x="662" y="38" width="104" height="42" rx="4" />
            <text x="714" y="55">观察中</text>
            <text x="714" y="73">{pendingSignals.length}</text>
          </g>
          <g className="flow-output main">
            <rect x="662" y="94" width="104" height="42" rx="4" />
            <text x="714" y="111">已兑现</text>
            <text x="714" y="129">{executedSignals.length}</text>
          </g>
          <g className="flow-output red">
            <rect x="548" y="190" width="108" height="38" rx="4" />
            <text x="602" y="206">风险保护</text>
            <text x="602" y="223">{blockedSignals.length}</text>
          </g>
          <g className="flow-output amber">
            <rect x="670" y="156" width="92" height="38" rx="4" />
            <text x="716" y="172">未兑现</text>
            <text x="716" y="189">{missedSignals.length}</text>
          </g>
        </svg>
        <div className="flow-outcome-strip">
          <span><b>{executedSignals.length}</b> 已兑现</span>
          <span><b>{pendingSignals.length}</b> 观察中</span>
          <span><b>{missedSignals.length}</b> 未兑现</span>
          <span><b>{blockedSignals.length}</b> 风险保护</span>
        </div>
      </div>
    </section>
  )
}
