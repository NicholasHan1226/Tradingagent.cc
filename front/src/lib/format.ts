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
    timeZone: 'Asia/Shanghai',
  }).format(date)
}

export function formatCurrency(value: number) {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits: 2,
  }).format(value)
}

export function formatUsdt(value: number) {
  return `${value.toLocaleString('en-US', { maximumFractionDigits: 2 })} USDT`
}

export function formatSignedUsdt(value: number) {
  const sign = value > 0 ? '+' : ''
  return `${sign}${formatUsdt(value)}`
}

export function formatCny(value: number) {
  return new Intl.NumberFormat('zh-CN', {
    style: 'currency',
    currency: 'CNY',
    maximumFractionDigits: 0,
  }).format(value)
}

export function formatCnyCompact(value: number) {
  const abs = Math.abs(value)
  if (abs >= 10000) return `${value < 0 ? '-' : ''}¥${(abs / 10000).toFixed(2)}万`
  return formatCny(value)
}

export function formatSignedCnyCompact(value: number) {
  const sign = value > 0 ? '+' : value < 0 ? '-' : ''
  return `${sign}${formatCnyCompact(Math.abs(value))}`
}
