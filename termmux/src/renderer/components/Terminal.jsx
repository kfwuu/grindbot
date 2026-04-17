/**
 * Terminal.jsx — xterm.js terminal tile.
 * Owns: xterm instance, PTY IPC connection, resize observer.
 * Renders: TileHeader on top, xterm canvas below.
 */
import { useEffect, useRef } from 'react'
import { Terminal as XTerm } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { Unicode11Addon } from '@xterm/addon-unicode11'
import '@xterm/xterm/css/xterm.css'
import { TileHeader } from './TileHeader.jsx'

const XTERM_THEME = {
  background:    '#0d1117',
  foreground:    '#c9d1d9',
  cursor:        '#58a6ff',
  cursorAccent:  '#0d1117',
  selectionBackground: 'rgba(88, 166, 255, 0.25)',
  black:         '#484f58',
  red:           '#ff7b72',
  green:         '#3fb950',
  yellow:        '#d29922',
  blue:          '#58a6ff',
  magenta:       '#bc8cff',
  cyan:          '#39c5cf',
  white:         '#b1bac4',
  brightBlack:   '#6e7681',
  brightRed:     '#ffa198',
  brightGreen:   '#56d364',
  brightYellow:  '#e3b341',
  brightBlue:    '#79c0ff',
  brightMagenta: '#d2a8ff',
  brightCyan:    '#56d4dd',
  brightWhite:   '#f0f6fc'
}

/**
 * @param {{
 *   sessionId: string,
 *   label: string,
 *   exited?: boolean,
 *   onKill: () => void,
 *   onRename: (label: string) => void,
 *   onExit: () => void
 * }} props
 */
export function Terminal({ sessionId, label, exited, onKill, onRename, onExit }) {
  const containerRef = useRef(null)
  const xtermRef    = useRef(null)
  const fitAddonRef = useRef(null)

  useEffect(() => {
    if (!containerRef.current) return

    // ── Create xterm instance ──────────────────────────────────────────────
    const xterm = new XTerm({
      cursorBlink: true,
      theme: XTERM_THEME,
      fontFamily: '"Cascadia Code", "Cascadia Mono", "MesloLGS NF", Menlo, Monaco, Consolas, "DejaVu Sans Mono", monospace',
      fontSize: 13,
      lineHeight: 1.2,
      scrollback: 5000,
      allowProposedApi: true
    })

    const fitAddon = new FitAddon()
    xterm.loadAddon(fitAddon)

    // Unicode 11 addon: corrects width measurement for block-drawing characters
    // (▓ █ ░ ▒ etc.) so they occupy exactly 1 cell instead of being misclassified.
    const unicode11 = new Unicode11Addon()
    xterm.loadAddon(unicode11)
    xterm.unicode.activeVersion = '11'

    xterm.open(containerRef.current)

    // Fit after a tick so the container has final dimensions
    requestAnimationFrame(() => fitAddon.fit())

    xtermRef.current    = xterm
    fitAddonRef.current = fitAddon

    // ── PTY → xterm (output) ──────────────────────────────────────────────
    const removeDataListener = window.pty.onData(sessionId, (data) => {
      xterm.write(data)
    })

    // ── PTY exit ──────────────────────────────────────────────────────────
    const removeExitListener = window.pty.onExit(sessionId, (code) => {
      xterm.writeln(`\r\n\x1b[2m[Process exited with code ${code}]\x1b[0m`)
      onExit()
    })

    // ── xterm → PTY (input) ───────────────────────────────────────────────
    xterm.onData((data) => {
      window.pty.write(sessionId, data)
    })

    // ── Resize observer ───────────────────────────────────────────────────
    const ro = new ResizeObserver(() => {
      try {
        fitAddon.fit()
        window.pty.resize(sessionId, xterm.cols, xterm.rows)
      } catch { /* ignore during teardown */ }
    })
    ro.observe(containerRef.current)

    return () => {
      removeDataListener()
      removeExitListener()
      ro.disconnect()
      xterm.dispose()
    }
  }, [sessionId]) // intentionally only sessionId — other props don't need re-init

  const tileStyle = {
    display: 'flex',
    flexDirection: 'column',
    background: 'var(--bg)',
    overflow: 'hidden',
    minWidth: 0,
    minHeight: 0,
    ...(exited ? { opacity: 0.7 } : {})
  }

  const bodyStyle = {
    flex: 1,
    overflow: 'hidden',
    padding: '2px 4px 4px'
  }

  return (
    <div style={tileStyle}>
      <TileHeader
        label={label}
        exited={exited}
        onKill={onKill}
        onRename={onRename}
      />
      <div ref={containerRef} style={bodyStyle} />
    </div>
  )
}
