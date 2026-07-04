import type { BookTone } from '../types/dashboard'

export function OutcomePill({ label, tone, value }: { label: string; tone: BookTone; value: string }) {
  return (
    <span className={`outcome-pill ${tone}`}>
      <em>{label}</em>
      <strong>{value}</strong>
    </span>
  )
}
