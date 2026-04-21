/**
 * App.jsx — Root component for TermMux.
 * Manages: session list, grid config, project path, layout.
 */
import { useState, useEffect, useCallback, useRef } from 'react'
import { Toolbar } from './components/Toolbar.jsx'
import { TileGrid } from './components/TileGrid.jsx'
import { GrindPanel } from './components/GrindPanel.jsx'
import { AgentOffice } from './components/AgentOffice.jsx'

const MAX_SESSIONS = 16

export default function App() {
  const [sessions, setSessions] = useState([])       // { id, label, command }[]
  const [gridCols, setGridCols] = useState(2)
  const [projectPath, setProjectPath] = useState('')
  const [grindPanelOpen, setGrindPanelOpen] = useState(true)
  const [officePanelOpen, setOfficePanelOpen] = useState(false)

  // Always-current sessions ref so handleAddSession never reads a stale closure value.
  const sessionsRef = useRef(sessions)
  useEffect(() => { sessionsRef.current = sessions }, [sessions])

  // Guard: prevent concurrent handleAddSession calls from racing past the
  // MAX_SESSIONS check before any of them have called setSessions.
  const creatingRef = useRef(false)

  // Load persisted config on mount.
  // The cancelled flag makes this safe under React StrictMode's double-effect
  // execution: if the effect is cleaned up before the promise resolves (which
  // StrictMode does on the first mount), the state setters are never called and
  // the second mount's effect takes over cleanly.
  useEffect(() => {
    let cancelled = false
    window.electronConfig.get().then((cfg) => {
      if (cancelled) return
      if (cfg.projectPath) setProjectPath(cfg.projectPath)
      if (cfg.gridCols)    setGridCols(cfg.gridCols)
      if (cfg.grindPanelOpen !== undefined) setGrindPanelOpen(cfg.grindPanelOpen)
      if (cfg.officePanelOpen !== undefined) setOfficePanelOpen(cfg.officePanelOpen)
    })
    return () => { cancelled = true }
  }, [])

  // Persist config when it changes
  useEffect(() => {
    window.electronConfig.set({ projectPath, gridCols, grindPanelOpen, officePanelOpen })
  }, [projectPath, gridCols, grindPanelOpen, officePanelOpen])

  const handleAddSession = useCallback(async ({ command, args = [], label }) => {
    // creatingRef prevents a second call from racing through the MAX_SESSIONS
    // check while the first is still awaiting window.pty.create.
    if (creatingRef.current) return
    // Read from ref so we always see the current session count, not the value
    // that was captured when this callback was last recreated.
    if (sessionsRef.current.length >= MAX_SESSIONS) return

    creatingRef.current = true
    try {
      const result = await window.pty.create({
        command,
        args,
        cwd: projectPath || undefined
      })

      if (result.error) {
        console.error('Failed to create PTY:', result.error)
        return
      }

      const displayLabel = label || command || 'shell'
      setSessions((prev) => [
        ...prev,
        { id: result.id, label: displayLabel, command }
      ])
    } finally {
      creatingRef.current = false
    }
  }, [projectPath])

  const handleKillSession = useCallback((id) => {
    window.pty.kill(id)
    setSessions((prev) => prev.filter((s) => s.id !== id))
  }, [])

  const handleRenameSession = useCallback((id, label) => {
    setSessions((prev) =>
      prev.map((s) => (s.id === id ? { ...s, label } : s))
    )
  }, [])

  const handleSessionExit = useCallback((id) => {
    // Mark as exited — tile stays, user closes manually
    setSessions((prev) =>
      prev.map((s) => (s.id === id ? { ...s, exited: true } : s))
    )
  }, [])

  useEffect(() => {
    return window.sessionEvents.onOpenClaude(() => {
      handleAddSession({ command: 'claude', args: ['--dangerously-skip-permissions', '--model', 'claude-sonnet-4-6'], label: 'claude' })
    })
  }, [handleAddSession])

  const handleSelectProject = useCallback(async () => {
    const result = await window.dialog.openDir()
    if (result.path) setProjectPath(result.path)
  }, [])

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <Toolbar
        sessions={sessions}
        gridCols={gridCols}
        projectPath={projectPath}
        onAddSession={handleAddSession}
        onGridColsChange={setGridCols}
        onSelectProject={handleSelectProject}
        onToggleGrindPanel={() => setGrindPanelOpen((v) => !v)}
        grindPanelOpen={grindPanelOpen}
        onToggleOffice={() => setOfficePanelOpen((v) => !v)}
        officePanelOpen={officePanelOpen}
      />

      <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>
        <TileGrid
          sessions={sessions}
          gridCols={gridCols}
          onKill={handleKillSession}
          onRename={handleRenameSession}
          onExit={handleSessionExit}
        />

        {grindPanelOpen && (
          <GrindPanel
            projectPath={projectPath}
            onRunGrind={handleAddSession}
          />
        )}

        {officePanelOpen && (
          <AgentOffice
            projectPath={projectPath}
          />
        )}
      </div>
    </div>
  )
}
