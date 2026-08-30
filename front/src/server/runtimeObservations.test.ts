// @vitest-environment node
import { execFile, type ExecFileException } from 'node:child_process'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { runtimeObservationFixture } from '../test/runtimeObservationFixture'
import { parseRuntimeObservations } from '../types/runtimeObservations'
import { createRuntimeObservationReader } from './runtimeObservations'

const env = {
  TRADING_AGENT_RUNTIME_PYTHON: '/explicit/python3',
  TRADING_AGENT_RUNTIME_READER: '/explicit/read_runtime_observations.py',
}

function harness(extraEnv: NodeJS.ProcessEnv = {}) {
  let done: (error: ExecFileException | null, stdout: string, stderr: string) => void = () => {}
  const execute = vi.fn((...args: unknown[]) => { done = args[3] as typeof done })
  const read = createRuntimeObservationReader({ env: { ...env, ...extraEnv }, execute: execute as unknown as typeof execFile })
  return { read, execute, finish: (body: unknown = runtimeObservationFixture()) => done(null, JSON.stringify(body), ''), fail: (error: ExecFileException = new Error('private path/token')) => done(error, '', 'private stderr/token') }
}

afterEach(() => vi.useRealTimers())

describe('background runtime observation reader', () => {
  it('returns pending synchronously on the first request and singleflights concurrent reads', async () => {
    const { read, execute, finish } = harness()
    const first = read()
    expect(first).not.toBeInstanceOf(Promise)
    expect(first?.entries.map((e) => e.status)).toEqual(['pending', 'pending'])
    const concurrent = await Promise.all(Array.from({ length: 20 }, async () => read()))
    expect(concurrent.every((value) => value === first)).toBe(true)
    expect(execute).toHaveBeenCalledTimes(1)
    finish()
    expect(read()).toEqual(runtimeObservationFixture())
  })

  it('uses only explicit argv, 30 second hard timeout, 64KiB and a credential-free environment', () => {
    const { read, execute } = harness({ PATH: '/usr/bin:/bin', LANG: 'en_US.UTF-8', HOME: '/private/home', TD_TOKEN: 'secret', TRADINGDATAS_TOKEN: 'secret', PYTHONPATH: '/injection', NODE_OPTIONS: '--inspect', HTTPS_PROXY: 'secret' })
    read()
    expect(execute).toHaveBeenCalledWith('/explicit/python3', ['-B', '/explicit/read_runtime_observations.py'], {
      shell: false, timeout: 30_000, killSignal: 'SIGKILL', maxBuffer: 65_536, encoding: 'utf8',
      env: { PATH: '/usr/bin:/bin', LANG: 'en_US.UTF-8', REAL_TRADING_ENABLED: 'false', PYTHONDONTWRITEBYTECODE: '1', PYTHONUTF8: '1' },
    }, expect.any(Function))
  })

  it.each([{}, { TRADING_AGENT_RUNTIME_PYTHON: '/usr/bin/python3' }, { ...env, TRADING_AGENT_RUNTIME_READER: 'relative.py' }, { ...env, TRADING_AGENT_RUNTIME_PYTHON: 'python3' }])('does not spawn for absent/incomplete/relative config %j', (config) => {
    const execute = vi.fn()
    const read = createRuntimeObservationReader({ env: config, execute: execute as unknown as typeof execFile })
    const result = read()
    expect(execute).not.toHaveBeenCalled()
    if (Object.keys(config).length) expect(result?.entries[0].status).toBe('unavailable')
    else expect(result).toBeUndefined()
  })

  it('caches for five minutes after completion without restamping, then removes success while refreshing', () => {
    vi.useFakeTimers()
    const { read, execute, finish, fail } = harness()
    read()
    vi.advanceTimersByTime(15_000)
    finish()
    const success = read()
    vi.advanceTimersByTime(299_999)
    expect(read()).toBe(success)
    expect(read()?.generatedAt).toBe('2026-08-30T12:00:00Z')
    expect(execute).toHaveBeenCalledTimes(1)
    vi.advanceTimersByTime(1)
    expect(read()?.entries[1].status).toBe('pending')
    expect(read()?.entries[1].simulation).toBeUndefined()
    expect(execute).toHaveBeenCalledTimes(2)
    fail()
    const failure = read()
    expect(failure?.entries[1].status).toBe('unavailable')
    expect(JSON.stringify(failure)).not.toMatch(/private|secret|9234/)
    vi.advanceTimersByTime(299_999)
    expect(read()).toBe(failure)
    vi.advanceTimersByTime(1)
    expect(read()?.entries[0].status).toBe('pending')
    expect(execute).toHaveBeenCalledTimes(3)
    finish()
    expect(read()).toEqual(runtimeObservationFixture())
  })

  it('clears timed-out results locally and accepts later recovery', () => {
    const { read, fail } = harness()
    read()
    fail(Object.assign(new Error('timeout'), { killed: true }))
    expect(read()?.entries[0]).toMatchObject({ status: 'unavailable', reason: 'runtime_reader_timeout', observedAt: null })
  })

  it('classifies output-buffer overflow as invalid output, not a timeout', () => {
    const { read, fail } = harness()
    read()
    fail(Object.assign(new Error('too large'), { code: 'ERR_CHILD_PROCESS_STDIO_MAXBUFFER', killed: true }))
    expect(read()?.entries[0].reason).toBe('runtime_reader_output_invalid')
  })

  it('catches synchronous spawn errors without rejecting the snapshot', () => {
    const read = createRuntimeObservationReader({ env, execute: (() => { throw new Error('private path') }) as unknown as typeof execFile })
    expect(read()?.entries[0].status).toBe('unavailable')
  })

  it.each(['broken JSON', 'x'.repeat(65_537), { ...runtimeObservationFixture(), realTradingEnabled: true }])('contains invalid output within this panel', (body) => {
    const { read, finish } = harness()
    read()
    finish(body)
    expect(read()?.entries.every((e) => e.status === 'unavailable' && !e.simulation)).toBe(true)
  })
})

describe('strict observation contract', () => {
  it('preserves decimal strings, old source timestamps and an explicitly empty result', () => {
    expect(parseRuntimeObservations(runtimeObservationFixture())).toEqual(runtimeObservationFixture())
    expect(parseRuntimeObservations({ ...runtimeObservationFixture(), entries: [] })?.entries).toEqual([])
  })

  it.each([
    ['contract', 'unknown'], ['readOnly', undefined], ['readOnly', 'true'], ['realTradingEnabled', undefined], ['realTradingEnabled', 'false'], ['realTradingEnabled', true], ['generatedAt', '2026-08-30'], ['generatedAt', '2026-02-30T01:00:00Z'], ['token', 'secret'],
  ])('rejects envelope field %s=%s', (key, value) => {
    expect(parseRuntimeObservations({ ...runtimeObservationFixture(), [key]: value })).toBeUndefined()
  })

  it.each([
    ['id', 'unknown'], ['market', 'A-share'], ['sourceClass', 'live'], ['status', 'running'], ['canonicalAccountConnected', undefined], ['canonicalAccountConnected', true], ['canonicalAccountConnected', 'false'], ['observedAt', undefined], ['observedAt', '2026-08-28'], ['sourceSha256', 'abc'], ['sourceSha256', undefined], ['token', 'secret'], ['simulation', { currency: 'USDT' }], ['counts', { completed: -1, rejected: 0 }], ['counts', { completed: 1.5, rejected: 0 }], ['counts', { completed: Number.MAX_SAFE_INTEGER + 1, rejected: 0 }],
  ])('rejects unsafe/unknown entry field %s=%s', (key, value) => {
    const body = runtimeObservationFixture()
    Object.assign(body.entries[1], { [key]: value })
    expect(parseRuntimeObservations(body)).toBeUndefined()
  })

  it.each([['currency', 'CNY'], ['cash', 100], ['cash', 'NaN'], ['cash', '1e3'], ['orders', -1], ['token', 'secret']])('rejects invalid Crypto simulation field %s=%s', (key, value) => {
    const body = runtimeObservationFixture()
    Object.assign(body.entries[1].simulation!, { [key]: value })
    expect(parseRuntimeObservations(body)).toBeUndefined()
  })

  it('rejects duplicate IDs and contradictory coverage', () => {
    const body = runtimeObservationFixture()
    expect(parseRuntimeObservations({ ...body, entries: [body.entries[0], body.entries[0]] })).toBeUndefined()
    body.entries[0].coverage!.accepted = 101
    expect(parseRuntimeObservations(body)).toBeUndefined()
  })

  it.each(['ready', 'dated'] as const)('requires hash and non-future timezone-aware source time for %s', (status) => {
    for (const field of ['observedAt', 'sourceSha256']) {
      const body = runtimeObservationFixture()
      Object.assign(body.entries[1], { status, [field]: null })
      expect(parseRuntimeObservations(body)).toBeUndefined()
    }
    const future = runtimeObservationFixture()
    Object.assign(future.entries[1], { status, observedAt: '2026-08-30T20:00:01+08:00' })
    expect(parseRuntimeObservations(future)).toBeUndefined()
    future.entries[1].observedAt = '2026-08-30T20:00:00+08:00'
    expect(parseRuntimeObservations(future)).toEqual(future)
    future.entries[1].observedAt = '2026-08-30T20:00:00.000001+08:00'
    expect(parseRuntimeObservations(future)).toBeUndefined()
  })

  it.each(['pending', 'unavailable', 'invalid'] as const)('rejects display data carried by %s', (status) => {
    const withMoney = runtimeObservationFixture()
    withMoney.entries[1].status = status
    expect(parseRuntimeObservations(withMoney)).toBeUndefined()
    const withCoverage = runtimeObservationFixture()
    withCoverage.entries[0].status = status
    expect(parseRuntimeObservations(withCoverage)).toBeUndefined()
    const withCounts = runtimeObservationFixture()
    delete withCounts.entries[1].simulation
    withCounts.entries[1].status = status
    expect(parseRuntimeObservations(withCounts)).toBeUndefined()
  })
})
