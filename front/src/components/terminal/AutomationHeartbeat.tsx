import type { RuntimeHeartbeat } from '../../lib/runtimeHeartbeat'

export function AutomationHeartbeat({ heartbeat, compact = false }: { heartbeat: RuntimeHeartbeat; compact?: boolean }) {
  return (
    <section aria-label="自动化心跳" className={`automation-heartbeat ${heartbeat.state}${compact ? ' compact' : ''}`}>
      <i aria-hidden="true" />
      <div>
        <strong>{heartbeat.headline}</strong>
        {!compact && <span>{heartbeat.detail}</span>}
      </div>
      <small>{heartbeat.snapshotLabel}</small>
    </section>
  )
}
