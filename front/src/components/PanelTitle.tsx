import type { Page } from '../types/dashboard'

export function PanelTitle({
  action,
  kicker,
  onAction,
  title,
}: {
  action?: string
  kicker: string
  onAction?: () => void
  title: string
}) {
  return (
    <div className="panel-title">
      <div>
        <span>{kicker}</span>
        <h2>{title}</h2>
      </div>
      {action && onAction && (
        <button onClick={onAction} type="button">
          {action}
        </button>
      )}
    </div>
  )
}

export type PageSetter = (page: Page) => void
