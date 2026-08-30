// @vitest-environment node
import type { execFile, ExecFileException } from 'node:child_process'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import type { AddressInfo } from 'node:net'
import type { Server } from 'node:http'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { runtimeObservationFixture } from '../test/runtimeObservationFixture'
import * as runtime from './runtimeObservations'
import { readTradingAgentSnapshot } from './tradingAgentSnapshot'
import { createTradingAgentSnapshotHttpServer } from './tradingAgentSnapshotHttp'

const roots: string[] = []
const servers: Server[] = []
afterEach(async () => {
  vi.restoreAllMocks()
  await Promise.all(servers.splice(0).map((server) => new Promise<void>((resolve) => { server.closeAllConnections(); server.close(() => resolve()) })))
  await Promise.all(roots.splice(0).map((path) => rm(path, { recursive: true, force: true })))
})

describe('runtime observation snapshot isolation', () => {
  it('leaves every preexisting snapshot authority field unchanged', async () => {
    const root = await mkdtemp(join(tmpdir(), 'front-runtime-snapshot-test-'))
    roots.push(root)
    const read = vi.spyOn(runtime, 'readRuntimeObservations').mockReturnValue(undefined)
    const options = { workspaceRoot: root, now: new Date('2026-08-30T12:00:00Z') }
    const before = await readTradingAgentSnapshot(options)
    read.mockReturnValue(runtimeObservationFixture())
    const after = await readTradingAgentSnapshot(options)
    expect(after.runtimeObservations).toEqual(runtimeObservationFixture())
    expect({ ...after, runtimeObservations: undefined }).toEqual(before)
  })

  it('returns HTTP immediately while child is unresolved and ignores command-like query parameters', async () => {
    const root = await mkdtemp(join(tmpdir(), 'front-runtime-http-test-'))
    roots.push(root)
    let done: (error: ExecFileException | null, stdout: string, stderr: string) => void = () => {}
    const execute = vi.fn((...args: unknown[]) => { done = args[3] as typeof done })
    const reader = runtime.createRuntimeObservationReader({
      env: { TRADING_AGENT_RUNTIME_PYTHON: '/approved/python3', TRADING_AGENT_RUNTIME_READER: '/approved/reader.py' },
      execute: execute as unknown as typeof execFile,
    })
    vi.spyOn(runtime, 'readRuntimeObservations').mockImplementation(reader)
    const server = createTradingAgentSnapshotHttpServer({
      workspaceRoot: root,
      readSnapshot: () => readTradingAgentSnapshot({ workspaceRoot: root, now: new Date('2026-08-30T12:00:00Z') }),
    })
    servers.push(server)
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
    const url = `http://127.0.0.1:${(server.address() as AddressInfo).port}/api/trading-agent/snapshot?reader=/untrusted.py&python=/bin/sh&args=-c`
    const responses = await Promise.all(Array.from({ length: 3 }, () => fetch(url, { signal: AbortSignal.timeout(2000) })))
    expect(responses.every((response) => response.status === 200)).toBe(true)
    const first = await responses[0].json()
    expect(first.runtimeObservations.entries[0].status).toBe('pending')
    expect(execute).toHaveBeenCalledTimes(1)
    expect(execute.mock.calls[0].slice(0, 2)).toEqual(['/approved/python3', ['-B', '/approved/reader.py']])
    done(new Error('private failure'), '', 'private stderr')
    const failureResponse = await fetch(url)
    expect(failureResponse.status).toBe(200)
    expect(failureResponse.headers.get('Cache-Control')).toBe('no-store')
    const failed = await failureResponse.json()
    expect(failed.runtimeObservations.entries[0].status).toBe('unavailable')
    expect({ ...failed, runtimeObservations: undefined }).toEqual({ ...first, runtimeObservations: undefined })
    expect(JSON.stringify(failed)).not.toContain('private failure')
  })
})
