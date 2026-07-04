import type { ReactNode } from 'react'
import type { DomainStatus } from '../types/status'

type StatusBoundaryProps = {
  children: ReactNode
  emptyLabel?: string
  loading?: ReactNode
  message?: string
  onRetry?: () => void
  status: DomainStatus
}

export function StatusBoundary({
  children,
  emptyLabel = '当前没有需要处理的数据',
  loading,
  message,
  onRetry,
  status,
}: StatusBoundaryProps) {
  if (status === 'loading') return <>{loading ?? <div className="state-empty">正在准备数据</div>}</>

  if (status === 'empty') {
    return (
      <div className="state-empty">
        <strong>{emptyLabel}</strong>
        <span>{message ?? '后台会继续观察，有结果时自动出现。'}</span>
      </div>
    )
  }

  if (status === 'error') {
    return (
      <div className="state-error">
        <strong>这块数据暂时不可用</strong>
        <span>{message ?? '不影响其它面板，稍后会自动重试。'}</span>
        {onRetry && <button onClick={onRetry} type="button">重新检查</button>}
      </div>
    )
  }

  if (status === 'live-gated') {
    return (
      <div className="state-empty">
        <strong>实盘还没有接入</strong>
        <span>模拟盘继续运行；实盘接入前不会展示真实资金结果。</span>
      </div>
    )
  }

  return (
    <>
      {status === 'stale' && <div className="stale-notice">数据有延迟，后台正在刷新</div>}
      {children}
    </>
  )
}
