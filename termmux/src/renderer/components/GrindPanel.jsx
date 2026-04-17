/**
 * GrindPanel.jsx — Right sidebar showing GrindBot task board.
 * Reads .grindbot/tasks.json via file watching. Live updates as grind runs.
 */
import { useState, useEffect } from 'react'

const CAT_COLOR = {
  bug:         '#f85149',
  security:    '#bc8cff',
  feature:     '#58a6ff',
  performance: '#39d0d8',
  style:       '#6e7681',
}

const SEV_COLOR = {
  critical: '#f85149',
  high:     '#d29922',
  medium:   '#58a6ff',
  low:      '#6e7681',
}

export function GrindPanel({ projectPath, onRunGrind }) {
  const [tasks, setTasks]           = useState([])
  const [cost, setCost]             = useState(null)
  const [watchError, setWatchError] = useState(null)
  const [filter, setFilter]         = useState('all') // all | pending | completed | failed

  const tasksFile = projectPath ? `${projectPath}/.grindbot/tasks.json` : null
  const costFile  = projectPath ? `${projectPath}/.grindbot/cost_history.json` : null

  // ── File watching ───────────────────────────────────────────────────────────

  useEffect(() => {
    if (!tasksFile) { setTasks([]); return }
    let watchId = null, removeListener = null, cancelled = false

    async function init() {
      const r = await window.files.read(tasksFile)
      if (cancelled) return
      if (r.contents) parseTasks(r.contents)
      else setWatchError(r.error || 'no file')

      const w = await window.files.watch(tasksFile)
      if (cancelled) return
      if (w.error) { setWatchError(w.error); return }
      watchId = w.watchId
      removeListener = window.files.onChanged(watchId, parseTasks)
    }

    init()
    return () => {
      cancelled = true
      if (removeListener) removeListener()
      if (watchId !== null) window.files.unwatch(watchId)
    }
  }, [tasksFile])

  useEffect(() => {
    if (!costFile) { setCost(null); return }
    let watchId = null, removeListener = null, cancelled = false

    async function init() {
      const r = await window.files.read(costFile)
      if (cancelled) return
      if (r.contents) parseCost(r.contents)

      const w = await window.files.watch(costFile)
      if (cancelled) return
      if (w.error) return
      watchId = w.watchId
      removeListener = window.files.onChanged(watchId, parseCost)
    }

    init()
    return () => {
      cancelled = true
      if (removeListener) removeListener()
      if (watchId !== null) window.files.unwatch(watchId)
    }
  }, [costFile])

  function parseTasks(contents) {
    try {
      const d = JSON.parse(contents)
      setTasks(Array.isArray(d) ? d : [])
      setWatchError(null)
    } catch { setTasks([]) }
  }

  function parseCost(contents) {
    try {
      const entries = JSON.parse(contents)
      if (!Array.isArray(entries)) return
      const total = entries.reduce((s, e) => s + (e.cost_usd || 0), 0)
      setCost({ total, count: entries.length, last: entries[entries.length - 1] || null })
    } catch { setCost(null) }
  }

  // ── Derived ─────────────────────────────────────────────────────────────────

  const pending   = tasks.filter(t => t.status === 'pending')
  const completed = tasks.filter(t => t.status === 'completed')
  const failed    = tasks.filter(t => t.status === 'failed')
  const total     = tasks.length

  const filtered = filter === 'pending'   ? pending
                 : filter === 'completed' ? completed
                 : filter === 'failed'    ? failed
                 : tasks

  const progress = total > 0 ? Math.round((completed.length / total) * 100) : 0

  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <div style={styles.panel}>
      {/* Header */}
      <div style={styles.header}>
        <span style={{ fontWeight: 700, color: 'var(--accent)', fontSize: 13 }}>⚙ GrindBot</span>
        {projectPath && (
          <span style={{ color: 'var(--text-muted)', fontSize: 10, marginTop: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {projectPath.split(/[\\/]/).pop()}
          </span>
        )}
      </div>

      <div style={styles.body}>
        {!projectPath ? (
          <div style={styles.empty}>Select a project to see tasks.</div>
        ) : (
          <>
            {/* Progress bar */}
            {total > 0 && (
              <div style={{ marginBottom: 14 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 5 }}>
                  <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                    {completed.length}/{total} done
                  </span>
                  <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{progress}%</span>
                </div>
                <div style={styles.progressTrack}>
                  <div style={{ ...styles.progressFill, width: `${progress}%` }} />
                </div>
                {/* Status pills */}
                <div style={{ display: 'flex', gap: 6, marginTop: 8 }}>
                  <Pill label="pending"   count={pending.length}   color="var(--warning)" active={filter==='pending'}   onClick={() => setFilter(f => f==='pending'   ? 'all' : 'pending')} />
                  <Pill label="done"      count={completed.length} color="var(--success)" active={filter==='completed'} onClick={() => setFilter(f => f==='completed' ? 'all' : 'completed')} />
                  <Pill label="failed"    count={failed.length}    color="var(--error)"   active={filter==='failed'}    onClick={() => setFilter(f => f==='failed'    ? 'all' : 'failed')} />
                </div>
              </div>
            )}

            {/* Task list */}
            <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 4, overflowY: 'auto', marginBottom: 12 }}>
              {total === 0 ? (
                <div style={styles.empty}>
                  {watchError ? 'No tasks.json found.' : 'No tasks yet — run a scan.'}
                </div>
              ) : filtered.length === 0 ? (
                <div style={styles.empty}>No {filter} tasks.</div>
              ) : (
                filtered.map(t => <TaskCard key={t.id} task={t} />)
              )}
            </div>

            {/* Cost */}
            {cost && (
              <div style={styles.costRow}>
                <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>Brain cost</span>
                <span style={{ color: 'var(--success)', fontSize: 12, fontFamily: 'monospace', fontWeight: 700 }}>
                  ${cost.total.toFixed(4)}
                </span>
              </div>
            )}

            {/* Actions */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 8 }}>
              <button
                style={styles.btnPrimary}
                onClick={() => onRunGrind({ command: 'grindbot', args: ['grind'], label: 'grindbot grind' })}
              >
                ▶ grindbot grind
              </button>
              <button
                style={styles.btnSecondary}
                disabled={!projectPath}
                onClick={() => onRunGrind({ command: 'grindbot', args: ['scan', projectPath], label: 'grindbot scan' })}
              >
                🔍 grindbot scan
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

// ─── TaskCard ─────────────────────────────────────────────────────────────────

function TaskCard({ task }) {
  const statusIcon  = { pending: '○', completed: '●', failed: '✕' }[task.status] || '·'
  const statusColor = { pending: 'var(--warning)', completed: 'var(--success)', failed: 'var(--error)' }[task.status] || 'var(--text-muted)'
  const catColor    = CAT_COLOR[task.category] || '#6e7681'
  const sevColor    = SEV_COLOR[task.severity] || '#6e7681'
  const dimmed      = task.status === 'completed'

  return (
    <div style={{ ...styles.card, opacity: dimmed ? 0.55 : 1 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        {/* Status dot */}
        <span style={{ color: statusColor, fontSize: 10, flexShrink: 0 }}>{statusIcon}</span>

        {/* ID */}
        <span style={{ color: 'var(--text-muted)', fontSize: 10, fontFamily: 'monospace', flexShrink: 0 }}>
          #{task.id}
        </span>

        {/* Category badge */}
        <span style={{
          fontSize: 9,
          fontWeight: 700,
          textTransform: 'uppercase',
          color: catColor,
          background: `${catColor}22`,
          border: `1px solid ${catColor}55`,
          borderRadius: 3,
          padding: '1px 4px',
          flexShrink: 0,
        }}>
          {task.category || 'task'}
        </span>

        {/* Severity dot */}
        <span style={{ width: 6, height: 6, borderRadius: '50%', background: sevColor, flexShrink: 0 }} title={task.severity} />
      </div>

      {/* Title */}
      <div style={{
        fontSize: 11,
        color: 'var(--text)',
        marginTop: 4,
        lineHeight: 1.35,
        display: '-webkit-box',
        WebkitLineClamp: 2,
        WebkitBoxOrient: 'vertical',
        overflow: 'hidden',
      }}
        title={task.title}
      >
        {task.title}
      </div>

      {/* Branch or error */}
      {task.branch && (
        <div style={{ fontSize: 9, color: 'var(--purple)', marginTop: 3, fontFamily: 'monospace', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {task.branch}
        </div>
      )}
      {task.error && (
        <div style={{ fontSize: 9, color: 'var(--error)', marginTop: 3, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={task.error}>
          {task.error}
        </div>
      )}
    </div>
  )
}

// ─── Pill filter button ───────────────────────────────────────────────────────

function Pill({ label, count, color, active, onClick }) {
  return (
    <button
      onClick={onClick}
      style={{
        fontSize: 10,
        padding: '2px 7px',
        borderRadius: 10,
        border: `1px solid ${active ? color : 'var(--border)'}`,
        background: active ? `${color}22` : 'transparent',
        color: active ? color : 'var(--text-muted)',
        cursor: 'pointer',
        transition: 'all 0.1s',
        fontFamily: 'inherit',
      }}
    >
      {label} {count}
    </button>
  )
}

// ─── Styles ───────────────────────────────────────────────────────────────────

const styles = {
  panel: {
    width: 260,
    flexShrink: 0,
    display: 'flex',
    flexDirection: 'column',
    background: 'var(--surface)',
    borderLeft: '1px solid var(--border)',
    overflow: 'hidden',
  },
  header: {
    padding: '8px 12px',
    borderBottom: '1px solid var(--border)',
    flexShrink: 0,
    display: 'flex',
    flexDirection: 'column',
  },
  body: {
    flex: 1,
    display: 'flex',
    flexDirection: 'column',
    padding: 12,
    overflowY: 'auto',
    gap: 0,
  },
  empty: {
    color: 'var(--text-muted)',
    fontSize: 12,
    lineHeight: 1.5,
  },
  card: {
    background: 'var(--surface2)',
    border: '1px solid var(--border)',
    borderRadius: 6,
    padding: '7px 9px',
  },
  progressTrack: {
    height: 4,
    borderRadius: 2,
    background: 'var(--surface2)',
    overflow: 'hidden',
  },
  progressFill: {
    height: '100%',
    background: 'var(--success)',
    borderRadius: 2,
    transition: 'width 0.4s ease',
  },
  costRow: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    padding: '6px 0',
    borderTop: '1px solid var(--border)',
    marginTop: 4,
  },
  btnPrimary: {
    width: '100%',
    padding: '7px 10px',
    borderRadius: 6,
    fontSize: 12,
    background: 'var(--accent)',
    color: '#fff',
    border: 'none',
    textAlign: 'left',
    cursor: 'pointer',
    fontFamily: 'inherit',
  },
  btnSecondary: {
    width: '100%',
    padding: '7px 10px',
    borderRadius: 6,
    fontSize: 12,
    background: 'var(--surface2)',
    color: 'var(--text)',
    border: '1px solid var(--border)',
    textAlign: 'left',
    cursor: 'pointer',
    fontFamily: 'inherit',
  },
}
