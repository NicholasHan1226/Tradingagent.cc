import '@testing-library/jest-dom/vitest'
import { beforeEach } from 'vitest'

const localValues = new Map<string, string>()
if (typeof window !== 'undefined') {
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    value: {
      clear: () => localValues.clear(),
      getItem: (key: string) => localValues.get(key) ?? null,
      key: (index: number) => [...localValues.keys()][index] ?? null,
      get length() { return localValues.size },
      removeItem: (key: string) => { localValues.delete(key) },
      setItem: (key: string, value: string) => { localValues.set(key, String(value)) },
    } satisfies Storage,
  })
}

beforeEach(() => {
  localValues.clear()
})
