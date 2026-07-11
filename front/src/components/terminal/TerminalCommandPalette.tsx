import { Command, Search } from 'lucide-react'
import { useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react'

export type TerminalCommand = { id: string; label: string; group: string; hint?: string; keywords?: string }

export function TerminalCommandPalette({ commands, onClose, onExecute }: { commands: TerminalCommand[]; onClose: () => void; onExecute: (id: string) => void }) {
  const [query, setQuery] = useState('')
  const [activeIndex, setActiveIndex] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)
  const panelRef = useRef<HTMLElement>(null)
  const visible = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase()
    return normalized ? commands.filter((command) => `${command.label} ${command.group} ${command.keywords ?? ''}`.toLocaleLowerCase().includes(normalized)) : commands
  }, [commands, query])
  const active = visible[Math.min(activeIndex, Math.max(0, visible.length - 1))]

  const execute = (id?: string) => {
    if (!id) return
    onExecute(id)
    onClose()
  }

  const trapFocus = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.key !== 'Tab') return
    const focusable = [...(panelRef.current?.querySelectorAll<HTMLElement>('input, button:not([disabled])') ?? [])]
    if (!focusable.length) return
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    }
  }

  return (
    <div aria-label="终端命令面板" aria-modal="true" className="terminal-command-backdrop" onMouseDown={(event) => { if (event.currentTarget === event.target) onClose() }} role="dialog">
      <section className="terminal-command-palette" onKeyDown={trapFocus} ref={panelRef}>
        <header><Command aria-hidden="true" size={15} /><span>TERMINAL COMMAND</span><kbd>ESC</kbd></header>
        <label><Search aria-hidden="true" size={14} /><input aria-controls="terminal-command-list" aria-label="搜索终端命令" autoFocus onChange={(event) => { setQuery(event.target.value); setActiveIndex(0) }} onKeyDown={(event) => {
          if (event.key === 'Escape') { event.preventDefault(); onClose() }
          if (event.key === 'ArrowDown') { event.preventDefault(); setActiveIndex((index) => Math.min(index + 1, visible.length - 1)) }
          if (event.key === 'ArrowUp') { event.preventDefault(); setActiveIndex((index) => Math.max(0, index - 1)) }
          if (event.key === 'Enter') { event.preventDefault(); execute(active?.id) }
        }} placeholder="页面、市场、密度、关联上下文…" ref={inputRef} role="combobox" value={query} /></label>
        <div className="terminal-command-list" id="terminal-command-list" role="listbox">
          {visible.map((command, index) => <button aria-selected={index === activeIndex} className={index === activeIndex ? 'selected' : ''} key={command.id} onClick={() => execute(command.id)} onMouseEnter={() => setActiveIndex(index)} role="option" type="button"><span><strong>{command.label}</strong><small>{command.group}</small></span>{command.hint && <kbd>{command.hint}</kbd>}</button>)}
          {!visible.length && <p>没有匹配的终端命令</p>}
        </div>
        <footer><span>↑↓ 选择</span><span>↵ 执行</span><span>仅改变本地视图</span></footer>
      </section>
    </div>
  )
}
