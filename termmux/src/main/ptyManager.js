/**
 * ptyManager.js — Manages node-pty sessions for TermMux.
 * All PTY operations go through this module. Max 16 concurrent sessions.
 */
import pty from '@homebridge/node-pty-prebuilt-multiarch'

const MAX_SESSIONS = 16
const sessions = new Map()
let nextId = 1

/**
 * Return a copy of env with VS Code-specific variables removed.
 *
 * When TermMux runs inside VS Code's integrated terminal, process.env carries
 * VS Code's internal IPC variables. If these are inherited by spawned processes
 * they activate VS Code shell integration inside the child. On Windows the
 * ConPTY process chain (conhost → shell wrapper → actual command) has three
 * participants that each fire the integration independently, which is why VS
 * Code opens three times from a single spawn.
 *
 * Stripping these keys makes each TermMux terminal behave as if it were
 * launched independently of VS Code, while leaving every other env var intact.
 *
 * @param {NodeJS.ProcessEnv} env
 * @returns {NodeJS.ProcessEnv}
 */
function sanitizeEnv(env) {
  const blocklist = [
    'VSCODE_IPC_HOOK_CLI',           // CLI IPC socket — causes `code` CLI to reuse parent VS Code
    'VSCODE_GIT_IPC_HANDLE',         // Git extension IPC handle
    'VSCODE_GIT_ASKPASS_NODE',       // Git ask-pass helpers
    'VSCODE_GIT_ASKPASS_EXTRA_ARGS',
    'VSCODE_GIT_ASKPASS_MAIN',
    'VSCODE_INJECTION_FLAG',         // Shell-integration injection trigger
    'VSCODE_NONCE',                  // Shell-integration handshake nonce
    'ELECTRON_RUN_AS_NODE',          // Forces Electron binary to run as plain Node
    'ELECTRON_NO_ATTACH_CONSOLE',    // Electron console-attachment flag
  ]
  const clean = { ...env }
  for (const key of blocklist) delete clean[key]
  // Normalise TERM_PROGRAM so tools inside the terminal don't think they are
  // running inside VS Code's own terminal emulator.
  if (clean.TERM_PROGRAM === 'vscode') clean.TERM_PROGRAM = 'TermMux'
  return clean
}

/**
 * Spawn a new PTY session.
 * @param {object} opts
 * @param {string} [opts.command] - Command to run (defaults to platform shell)
 * @param {string[]} [opts.args] - Arguments array
 * @param {string} [opts.cwd] - Working directory
 * @param {number} [opts.cols] - Terminal columns
 * @param {number} [opts.rows] - Terminal rows
 * @param {(data: string) => void} opts.onData - Called with output data
 * @param {(code: number) => void} opts.onExit - Called on process exit
 * @returns {{ id: string }}
 */
export function create({ command, args = [], cwd, cols = 80, rows = 24, onData, onExit }) {
  if (sessions.size >= MAX_SESSIONS) {
    throw new Error(`Maximum sessions reached (${MAX_SESSIONS})`)
  }

  const defaultShell = process.platform === 'win32' ? 'powershell.exe' : 'bash'
  const cmd = command || defaultShell

  const ptyProcess = pty.spawn(cmd, args, {
    name: 'xterm-256color',
    cols: Math.max(1, Math.min(500, cols)),
    rows: Math.max(1, Math.min(200, rows)),
    cwd: cwd || process.cwd(),
    env: sanitizeEnv(process.env)
  })

  const id = String(nextId++)

  ptyProcess.onData((data) => {
    if (onData) onData(data)
  })

  ptyProcess.onExit(({ exitCode }) => {
    sessions.delete(id)
    if (onExit) onExit(exitCode)
  })

  sessions.set(id, { pty: ptyProcess })
  return { id }
}

/**
 * Write data (keystrokes) to a PTY session.
 * @param {string} id
 * @param {string} data
 */
export function write(id, data) {
  const session = sessions.get(id)
  if (session) session.pty.write(data)
}

/**
 * Resize a PTY session.
 * @param {string} id
 * @param {number} cols
 * @param {number} rows
 */
export function resize(id, cols, rows) {
  const session = sessions.get(id)
  if (session && cols > 0 && rows > 0) {
    session.pty.resize(Math.min(cols, 500), Math.min(rows, 200))
  }
}

/**
 * Kill a PTY session.
 * @param {string} id
 */
export function kill(id) {
  const session = sessions.get(id)
  if (session) {
    try { session.pty.kill() } catch { /* already dead */ }
    sessions.delete(id)
  }
}

/**
 * Kill all PTY sessions. Called on app quit for graceful cleanup.
 */
export function killAll() {
  for (const [, session] of sessions) {
    try { session.pty.kill() } catch { /* ignore */ }
  }
  sessions.clear()
}

/** @returns {number} Active session count */
export function count() {
  return sessions.size
}

export { MAX_SESSIONS }
