/**
 * TileGrid.jsx — CSS grid container for terminal tiles.
 * Renders one Terminal per session. Grid columns are configurable 1–4.
 */
import { Terminal } from './Terminal.jsx'

/**
 * @param {{
 *   sessions: { id: string, label: string, command: string, exited?: boolean }[],
 *   gridCols: number,
 *   onKill: (id: string) => void,
 *   onRename: (id: string, label: string) => void,
 *   onExit: (id: string) => void
 * }} props
 */
export function TileGrid({ sessions, gridCols, onKill, onRename, onExit }) {
  if (sessions.length === 0) {
    return (
      <div style={emptyStyle}>
        <div style={{ textAlign: 'center' }}>
          <div style={{ fontSize: 48, marginBottom: 16 }}>⬡</div>
          <div style={{ fontSize: 18, color: 'var(--text)', marginBottom: 8 }}>
            No terminals open
          </div>
          <div style={{ color: 'var(--text-muted)' }}>
            Click <strong style={{ color: 'var(--accent)' }}>+ Add Session</strong> to start vibe coding
          </div>
        </div>
      </div>
    )
  }

  const cols = Math.min(Math.max(1, gridCols), 4)

  return (
    <div
      style={{
        flex: 1,
        display: 'grid',
        gridTemplateColumns: `repeat(${cols}, 1fr)`,
        gap: '2px',
        overflow: 'hidden',
        background: 'var(--border)',
        minWidth: 0
      }}
    >
      {sessions.map((session) => (
        <Terminal
          key={session.id}
          sessionId={session.id}
          label={session.label}
          exited={session.exited}
          onKill={() => onKill(session.id)}
          onRename={(label) => onRename(session.id, label)}
          onExit={() => onExit(session.id)}
        />
      ))}
    </div>
  )
}

const emptyStyle = {
  flex: 1,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  color: 'var(--text-muted)',
  userSelect: 'none'
}
