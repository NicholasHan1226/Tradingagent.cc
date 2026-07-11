import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { TerminalCommandPalette } from './TerminalCommandPalette'

describe('terminal command palette', () => {
  it('filters commands and executes the selected desktop action', () => {
    const onExecute = vi.fn()
    render(<TerminalCommandPalette commands={[
      { id: 'page-risk', label: '打开风险终端', group: '页面', hint: 'Alt+5' },
      { id: 'density', label: '切换舒适密度', group: '视图', hint: '本地' },
    ]} onClose={() => undefined} onExecute={onExecute} />)

    const input = screen.getByRole('combobox', { name: '搜索终端命令' })
    expect(input).toHaveFocus()
    fireEvent.change(input, { target: { value: '密度' } })
    expect(screen.queryByText('打开风险终端')).not.toBeInTheDocument()
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onExecute).toHaveBeenCalledWith('density')
  })

  it('closes with escape', () => {
    const onClose = vi.fn()
    render(<TerminalCommandPalette commands={[]} onClose={onClose} onExecute={() => undefined} />)
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Escape' })
    expect(onClose).toHaveBeenCalled()
  })

  it('keeps keyboard focus inside the modal command surface', () => {
    render(<TerminalCommandPalette commands={[
      { id: 'page-risk', label: '打开风险终端', group: '页面' },
      { id: 'density', label: '切换舒适密度', group: '视图' },
    ]} onClose={() => undefined} onExecute={() => undefined} />)

    const input = screen.getByRole('combobox')
    const options = screen.getAllByRole('option')
    input.focus()
    fireEvent.keyDown(input, { key: 'Tab', shiftKey: true })
    expect(options.at(-1)).toHaveFocus()
    fireEvent.keyDown(options.at(-1)!, { key: 'Tab' })
    expect(input).toHaveFocus()
  })
})
