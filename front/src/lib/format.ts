import type { HoldingRow } from '../types/dashboard'

export function riskClass(risk: HoldingRow['risk']) {
  if (risk === '偏高') return 'blocked'
  if (risk === '观察') return 'missed'
  return 'executed'
}

export function formatTime(date: Date) {
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date)
}

export function formatCurrency(value: number) {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits: 2,
  }).format(value)
}
