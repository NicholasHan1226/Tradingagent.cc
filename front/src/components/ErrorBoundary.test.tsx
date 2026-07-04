import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ErrorBoundary } from './ErrorBoundary'

function BrokenPanel(): never {
  throw new Error('panel failed')
}

describe('ErrorBoundary', () => {
  it('keeps the dashboard recoverable when a child crashes', () => {
    render(
      <ErrorBoundary>
        <BrokenPanel />
      </ErrorBoundary>,
    )

    expect(screen.getByText('看版暂时无法显示')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重新加载' })).toBeInTheDocument()
  })
})
