/**
 * AgentOffice.jsx — pixel-agents office embedded as a webview inside termmux.
 * The pixel-server child process (started in main/index.js) serves on :8080.
 */
export function AgentOffice({ projectPath: _projectPath }) {
  return (
    <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>
      <webview
        src="pixel://ui/index.html"
        style={{ flex: 1, border: 'none', width: '100%', height: '100%' }}
      />
    </div>
  )
}
