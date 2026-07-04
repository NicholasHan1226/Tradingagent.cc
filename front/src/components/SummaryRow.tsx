import type { BookTone } from '../types/dashboard'

export function SummaryRow({ label, tone, value }: { label: string; tone?: BookTone; value: string }) {
  return (
    <div className={`summary-row ${tone ?? ''}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  )
}
