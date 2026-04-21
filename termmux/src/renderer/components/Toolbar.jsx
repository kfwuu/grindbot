/**
 * Toolbar.jsx — Top bar with Add Session, grid picker, project path, GrindPanel toggle.
 */
import { useState } from 'react'

const PRESET_COMMANDS = [
  { label: 'claude',       command: 'claude',   args: ['--dangerously-skip-permissions', '--model', 'claude-sonnet-4-6'] },
  { label: 'gemini',       command: 'gemini',   args: [] },
  { label: 'bash',         command: 'bash',     args: [] },
  { label: 'grindbot grind', command: 'grindbot', args: ['grind'] },
  { label: 'grindbot scan',  command: 'grindbot', args: ['scan', '.'] },
  { label: 'custom…',     command: null,       args: [] }
]

const GRID_OPTIONS = [
  { label: '1×1', cols: 1 },
  { label: '1×2', cols: 2 },
  { label: '2×2', cols: 2 },
  { label: '2×3', cols: 3 },
  { label: '3×3', cols: 3 },
  { label: '2×4', cols: 4 },
  { label: '4×4', cols: 4 }
]

const MAX_SESSIONS = 16

/**
 * @param {{
 *   sessions: object[],
 *   gridCols: number,
 *   projectPath: string,
 *   grindPanelOpen: boolean,
 *   officePanelOpen: boolean,
 *   onAddSession: (opts: {command:string,args:string[],label:string}) => void,
 *   onGridColsChange: (cols: number) => void,
 *   onSelectProject: () => void,
 *   onToggleGrindPanel: () => void,
 *   onToggleOffice: () => void,
 * }} props
 */
export function Toolbar({
  sessions,
  gridCols,
  projectPath,
  grindPanelOpen,
  officePanelOpen,
  onAddSession,
  onGridColsChange,
  onSelectProject,
  onToggleGrindPanel,
  onToggleOffice,
}) {
  const [showAddForm, setShowAddForm]   = useState(false)
  const [selected, setSelected]         = useState(PRESET_COMMANDS[0])
  const [customCmd, setCustomCmd]       = useState('')
  const atCap = sessions.length >= MAX_SESSIONS

  function handleAdd() {
    const isCustom = selected.command === null
    const command  = isCustom ? customCmd.trim() : selected.command
    const args     = isCustom ? [] : selected.args
    const label    = isCustom ? customCmd.trim() : selected.label

    if (!command) return
    onAddSession({ command, args, label })
    setShowAddForm(false)
    setCustomCmd('')
  }

  function handlePresetChange(e) {
    const found = PRESET_COMMANDS.find((p) => p.label === e.target.value)
    if (found) setSelected(found)
  }

  const projectLabel = projectPath
    ? projectPath.split(/[/\\]/).pop()
    : 'No project'

  return (
    <div style={barStyle}>
      {/* Brand */}
      <span style={brandStyle}>⬡ TermMux</span>

      {/* Separator */}
      <div style={sepStyle} />

      {/* Add Session */}
      <div style={{ position: 'relative' }}>
        <button
          style={btnStyle(false)}
          onClick={() => setShowAddForm((v) => !v)}
          disabled={atCap}
          title={atCap ? 'Maximum 16 sessions reached' : 'Add a new terminal session'}
        >
          + Add Session
          {atCap && <span style={{ marginLeft: 4, color: 'var(--error)' }}>⛔</span>}
        </button>

        {showAddForm && (
          <div style={dropdownStyle}>
            <div style={{ marginBottom: 8, color: 'var(--text-muted)', fontSize: 11 }}>
              Command
            </div>

            <select
              value={selected.label}
              onChange={handlePresetChange}
              style={{ width: '100%', marginBottom: 8 }}
            >
              {PRESET_COMMANDS.map((p) => (
                <option key={p.label} value={p.label}>{p.label}</option>
              ))}
            </select>

            {selected.command === null && (
              <input
                placeholder="command (e.g. node, python3)"
                value={customCmd}
                onChange={(e) => setCustomCmd(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleAdd()}
                style={{ width: '100%', marginBottom: 8 }}
                autoFocus
              />
            )}

            <div style={{ display: 'flex', gap: 6 }}>
              <button
                style={btnStyle(true)}
                onClick={handleAdd}
                disabled={selected.command === null && !customCmd.trim()}
              >
                Launch
              </button>
              <button
                style={{ ...btnStyle(false), color: 'var(--text-muted)' }}
                onClick={() => setShowAddForm(false)}
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Session count */}
      <span style={{ color: 'var(--text-muted)', fontSize: 11 }}>
        {sessions.length}/{MAX_SESSIONS}
      </span>

      <div style={sepStyle} />

      {/* Grid size */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <span style={{ color: 'var(--text-muted)', fontSize: 11 }}>Grid</span>
        <div style={{ display: 'flex', gap: 2 }}>
          {[1, 2, 3, 4].map((c) => (
            <button
              key={c}
              onClick={() => onGridColsChange(c)}
              title={`${c} column${c > 1 ? 's' : ''}`}
              style={{
                ...gridBtnStyle,
                background: gridCols === c ? 'var(--accent)' : 'var(--surface2)',
                color:      gridCols === c ? '#fff' : 'var(--text-muted)'
              }}
            >
              {c}
            </button>
          ))}
        </div>
      </div>

      <div style={sepStyle} />

      {/* Project path */}
      <button
        style={{ ...btnStyle(false), maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
        onClick={onSelectProject}
        title={projectPath || 'Click to select project directory'}
      >
        📁 {projectLabel}
      </button>

      {/* GrindPanel toggle */}
      <button
        style={btnStyle(grindPanelOpen)}
        onClick={onToggleGrindPanel}
        title="Toggle GrindBot panel"
      >
        ⚙ GrindBot
      </button>

      {/* Agent Office toggle */}
      <button
        style={btnStyle(officePanelOpen)}
        onClick={onToggleOffice}
        title="Toggle Agent Office (pixel-art task visualization)"
      >
        🏢 Office
      </button>
    </div>
  )
}

// ─── Styles ───────────────────────────────────────────────────────────────────

const barStyle = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  padding: '0 12px',
  height: 42,
  background: 'var(--surface)',
  borderBottom: '1px solid var(--border)',
  flexShrink: 0,
  userSelect: 'none'
}

const brandStyle = {
  fontWeight: 700,
  fontSize: 14,
  color: 'var(--accent)',
  letterSpacing: '-0.3px',
  flexShrink: 0
}

const sepStyle = {
  width: 1,
  height: 18,
  background: 'var(--border)',
  flexShrink: 0
}

function btnStyle(active) {
  return {
    padding: '4px 10px',
    borderRadius: 5,
    fontSize: 12,
    background: active ? 'var(--accent)' : 'var(--surface2)',
    color: active ? '#fff' : 'var(--text)',
    border: `1px solid ${active ? 'var(--accent)' : 'var(--border)'}`,
    transition: 'background 0.1s, color 0.1s',
    flexShrink: 0
  }
}

const gridBtnStyle = {
  width: 22,
  height: 22,
  borderRadius: 4,
  fontSize: 11,
  border: '1px solid var(--border)',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  transition: 'background 0.1s'
}

const dropdownStyle = {
  position: 'absolute',
  top: 'calc(100% + 6px)',
  left: 0,
  zIndex: 100,
  background: 'var(--surface)',
  border: '1px solid var(--border)',
  borderRadius: 8,
  padding: 12,
  width: 240,
  boxShadow: '0 8px 24px rgba(0,0,0,0.5)'
}

