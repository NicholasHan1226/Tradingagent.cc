import type { ReactNode } from 'react'
import type { TerminalDensity } from '../../lib/terminalDensity'

export function AdaptiveTerminalSurface({ children, density, label }: { children: ReactNode; density: TerminalDensity; label: string }) {
  return <section aria-label={label} className={`adaptive-terminal-surface ${density}`} data-density={density}>{children}</section>
}
