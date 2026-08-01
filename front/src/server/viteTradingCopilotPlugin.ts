import type { IncomingMessage, ServerResponse } from 'node:http'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import type { Plugin } from 'vite'
import { createTradingCopilotStateHandler } from './tradingCopilotState.ts'
import { createTradingCopilotStockIntelligenceHandler } from './tradingCopilotStockIntelligence.ts'

type Options = { statePath?: string; workspaceRoot?: string }
const currentDir = dirname(fileURLToPath(import.meta.url))
const defaultWorkspaceRoot = resolve(currentDir, '../../..')

export function tradingCopilotPlugin(options: Options = {}): Plugin {
  const handler = createTradingCopilotStateHandler({
    statePath: options.statePath,
    workspaceRoot: options.workspaceRoot ?? process.env.FINANCE_WORKSPACE_ROOT ?? defaultWorkspaceRoot,
  })
  const stockIntelligenceHandler = createTradingCopilotStockIntelligenceHandler({
    workspaceRoot: options.workspaceRoot ?? process.env.FINANCE_WORKSPACE_ROOT ?? defaultWorkspaceRoot,
  })
  const middleware = async (req: IncomingMessage, res: ServerResponse, next: () => void) => {
    if (await stockIntelligenceHandler(req, res)) return
    if (!await handler(req, res)) next()
  }
  return {
    name: 'trading-copilot-state',
    configureServer(server) { server.middlewares.use(middleware) },
    configurePreviewServer(server) { server.middlewares.use(middleware) },
  }
}
