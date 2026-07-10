import { describe, expect, it } from 'vitest'
import { formatTime } from './format'

describe('formatTime', () => {
  it('formats dashboard timestamps in UTC+8 regardless of browser timezone', () => {
    expect(formatTime(new Date('2026-07-11T00:00:00.000Z'))).toBe('08:00:00')
  })
})
