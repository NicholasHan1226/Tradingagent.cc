import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { tradingAgentSnapshotPlugin } from './src/server/viteTradingAgentSnapshotPlugin.ts'
import { tradingCopilotPlugin } from './src/server/viteTradingCopilotPlugin.ts'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tradingCopilotPlugin(), tradingAgentSnapshotPlugin()],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('/node_modules/recharts/')) return 'charts'
          if (id.includes('/node_modules/react/') || id.includes('/node_modules/react-dom/')) return 'react'
          return undefined
        },
      },
    },
  },
})
