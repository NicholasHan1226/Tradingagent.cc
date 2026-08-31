import { execFile } from 'node:child_process'
import { isAbsolute } from 'node:path'
import { parseRuntimeObservations, runtimeObservationState, type RuntimeObservations } from '../types/runtimeObservations.ts'

const CACHE_MS = 5 * 60 * 1000
const MAX_BUFFER = 64 * 1024

/** Request-triggered, process-local singleflight. No polling timer or awaited child. */
export function createRuntimeObservationReader({
  env = process.env,
  now = Date.now,
  execute = execFile,
}: {
  env?: NodeJS.ProcessEnv
  now?: () => number
  execute?: typeof execFile
} = {}) {
  const python = env.TRADING_AGENT_RUNTIME_PYTHON
  const reader = env.TRADING_AGENT_RUNTIME_READER
  // Never inherit HOME, PYTHONPATH, loader options, tokens, proxy or TD settings.
  const childEnv = {
    PATH: env.PATH || '/usr/bin:/bin',
    LANG: env.LANG || 'C.UTF-8',
    REAL_TRADING_ENABLED: 'false',
    PYTHONDONTWRITEBYTECODE: '1',
    PYTHONUTF8: '1',
  }
  let cached: RuntimeObservations | undefined
  let expiresAt = 0
  let inFlight = false

  return function readRuntimeObservations(): RuntimeObservations | undefined {
    if (!python && !reader) return undefined
    if (inFlight || (cached && now() < expiresAt)) return cached
    const generatedAt = new Date(now()).toISOString()
    if (!python || !reader || !isAbsolute(python) || !isAbsolute(reader) || python.includes('\0') || reader.includes('\0')) {
      cached = runtimeObservationState('unavailable', 'runtime_reader_configuration_invalid', generatedAt)
      expiresAt = now() + CACHE_MS
      return cached
    }

    // Evict expired success BEFORE refreshing; a failed refresh cannot serve it forever.
    cached = runtimeObservationState('pending', 'runtime_refresh_pending', generatedAt)
    inFlight = true
    const finish = (value: RuntimeObservations) => {
      cached = value
      expiresAt = now() + CACHE_MS
      inFlight = false
    }
    const unavailable = (reason: string) => runtimeObservationState('unavailable', reason, new Date(now()).toISOString())
    try {
      execute(python, ['-B', reader], {
        shell: false,
        timeout: 30_000,
        killSignal: 'SIGKILL',
        maxBuffer: MAX_BUFFER,
        encoding: 'utf8',
        env: childEnv,
      }, (error, stdout) => {
        // Never expose stderr, command paths or exception text in the snapshot/logs.
        if (error) {
          finish(unavailable(error.code === 'ERR_CHILD_PROCESS_STDIO_MAXBUFFER'
            ? 'runtime_reader_output_invalid'
            : error.killed ? 'runtime_reader_timeout' : 'runtime_reader_failed'))
          return
        }
        try {
          const result = Buffer.byteLength(stdout, 'utf8') <= MAX_BUFFER ? parseRuntimeObservations(JSON.parse(stdout)) : undefined
          finish(result ?? unavailable('runtime_reader_output_invalid'))
        } catch {
          finish(unavailable('runtime_reader_output_invalid'))
        }
      })
    } catch {
      finish(unavailable('runtime_reader_failed'))
    }
    return cached
  }
}

export const readRuntimeObservations = createRuntimeObservationReader()
