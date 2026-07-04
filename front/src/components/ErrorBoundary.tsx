import { Component, type ErrorInfo, type ReactNode } from 'react'

type ErrorBoundaryState = {
  hasError: boolean
}

export class ErrorBoundary extends Component<{ children: ReactNode }, ErrorBoundaryState> {
  state: ErrorBoundaryState = {
    hasError: false,
  }

  static getDerivedStateFromError() {
    return { hasError: true }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Dashboard render failed', error, info.componentStack)
  }

  render() {
    if (this.state.hasError) {
      return (
        <main className="app-error">
          <section>
            <span>TradingAgent</span>
            <h1>看版暂时无法显示</h1>
            <p>页面没有继续展示可能不完整的数据。重新加载后会再次检查当前模拟盘状态。</p>
            <button onClick={() => window.location.reload()} type="button">重新加载</button>
          </section>
        </main>
      )
    }

    return this.props.children
  }
}
