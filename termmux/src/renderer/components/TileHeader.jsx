/**
 * TileHeader.jsx — Header bar for a terminal tile.
 * Shows label (double-click to rename), status dot, close button.
 */
import { useState, useRef, useEffect } from 'react'

/**
 * @param {{
 *   label: string,
 *   exited?: boolean,
 *   onKill: () => void,
 *   onRename: (label: string) => void
 * }} props
 */
export function TileHeader({ label, exited, onKill, onRename }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft]     = useState(label)
  const inputRef              = useRef(null)

  useEffect(() => {
    if (editing && inputRef.current) {
      inputRef.current.focus()
      inputRef.current.select()
    }
  }, [editing])

  function startEdit() {
    setDraft(label)
    setEditing(true)
  }

  function commitEdit() {
    const trimmed = draft.trim()
    if (trimmed) onRename(trimmed)
    setEditing(false)
  }

  function handleKeyDown(e) {
    if (e.key === 'Enter')  commitEdit()
    if (e.key === 'Escape') setEditing(false)
    e.stopPropagation() // don't send keystrokes to xterm
  }

  const dotColor = exited ? 'var(--text-muted)' : 'var(--success)'

  return (
    <div style={headerStyle}>
      {/* Status dot */}
      <span style={{ ...dotStyle, background: dotColor }} />

      {/* Label (or rename input) */}
      {editing ? (
        <input
          ref={inputRef}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commitEdit}
          onKeyDown={handleKeyDown}
          style={inputStyle}
        />
      ) : (
        <span
          onDoubleClick={startEdit}
          title="Double-click to rename"
          style={labelStyle}
        >
          {label}
          {exited && <span style={{ color: 'var(--text-muted)', marginLeft: 4 }}>[exited]</span>}
        </span>
      )}

      {/* Close button */}
      <button
        onClick={onKill}
        title="Close terminal"
        style={closeStyle}
      >
        ✕
      </button>
    </div>
  )
}

const headerStyle = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  padding: '3px 8px',
  background: 'var(--surface)',
  borderBottom: '1px solid var(--border)',
  userSelect: 'none',
  flexShrink: 0,
  minHeight: 28
}

const dotStyle = {
  width: 8,
  height: 8,
  borderRadius: '50%',
  flexShrink: 0
}

const labelStyle = {
  flex: 1,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
  color: 'var(--text-muted)',
  fontSize: 11,
  fontFamily: 'monospace',
  cursor: 'default'
}

const inputStyle = {
  flex: 1,
  fontSize: 11,
  padding: '1px 4px',
  height: 20
}

const closeStyle = {
  color: 'var(--text-muted)',
  fontSize: 10,
  width: 18,
  height: 18,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  borderRadius: 3,
  flexShrink: 0,
  transition: 'background 0.1s, color 0.1s'
}
