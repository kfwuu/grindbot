/**
 * index.js — Electron main process for TermMux.
 * Owns: window creation, IPC handlers, PTY lifecycle, file watching, config.
 */
import { app, BrowserWindow, ipcMain, dialog, protocol, net } from 'electron'
import { join, resolve } from 'path'
import { pathToFileURL } from 'url'
import { readFileSync, writeFileSync, watch as fsWatch } from 'fs'
import { spawn } from 'child_process'
import * as ptyManager from './ptyManager.js'

// ─── Custom protocol (pixel://) ───────────────────────────────────────────────

protocol.registerSchemesAsPrivileged([
  { scheme: 'pixel', privileges: { standard: true, supportFetchAPI: true, stream: true } }
])

// ─── pixel-server child process ───────────────────────────────────────────────

let serverProcess = null

function startPixelServer() {
  const serverPath = resolve(app.getAppPath(), '..', 'pixel-agents', 'termux-server', 'dist', 'server.js')
  serverProcess = spawn('node', [serverPath], {
    env: { ...process.env, PORT: '8080' },
    stdio: ['ignore', 'pipe', 'inherit']
  })
  serverProcess.stdout.on('data', (data) => {
    for (const line of data.toString().split('\n')) {
      if (!line.trim()) continue
      try {
        const msg = JSON.parse(line.trim())
        if (msg.event === 'openClaude' && mainWindow) {
          mainWindow.webContents.send('request:openClaude')
        }
      } catch { /* not JSON (e.g. normal log lines), ignore */ }
    }
  })
  serverProcess.on('error', (err) => console.error('[TermMux] pixel-server error:', err.message))
}

// ─── Config persistence (simple JSON, consistent with GrindBot philosophy) ───

function getConfigPath() {
  return join(app.getPath('userData'), 'termmux-config.json')
}

function loadConfig() {
  try {
    return JSON.parse(readFileSync(getConfigPath(), 'utf8'))
  } catch {
    return { projectPath: '', gridCols: 2, grindPanelOpen: true }
  }
}

function saveConfig(data) {
  try {
    writeFileSync(getConfigPath(), JSON.stringify(data, null, 2))
  } catch { /* userData dir may not exist yet on first launch */ }
}

// ─── File watchers ────────────────────────────────────────────────────────────

const fileWatchers = new Map()
let watcherIdCounter = 1

// ─── Window ───────────────────────────────────────────────────────────────────

let mainWindow = null

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 800,
    minHeight: 600,
    backgroundColor: '#0d1117',
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'default',
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      webSecurity: true,
      sandbox: false, // preload needs require('electron')
      webviewTag: true
    }
  })

  if (!app.isPackaged && process.env['ELECTRON_RENDERER_URL']) {
    mainWindow.loadURL(process.env['ELECTRON_RENDERER_URL'])
  } else {
    mainWindow.loadFile(join(__dirname, '../renderer/index.html'))
  }

  if (!app.isPackaged) {
    mainWindow.webContents.openDevTools({ mode: 'detach' })
  }

  mainWindow.on('closed', () => { mainWindow = null })
}

// ─── IPC: PTY ─────────────────────────────────────────────────────────────────

ipcMain.handle('pty:create', (event, opts) => {
  const { command, args = [], cwd, cols = 80, rows = 24 } = opts || {}

  // Strict input validation — PTY spawns with array, no shell:true, no injection
  if (command !== undefined && typeof command !== 'string') {
    return { error: 'command must be a string' }
  }
  if (!Array.isArray(args) || args.some(a => typeof a !== 'string')) {
    return { error: 'args must be an array of strings' }
  }
  if (cwd !== undefined && typeof cwd !== 'string') {
    return { error: 'cwd must be a string' }
  }

  let sessionId
  try {
    const result = ptyManager.create({
      command, args, cwd,
      cols: cols | 0,
      rows: rows | 0,
      onData: (data) => {
        if (!event.sender.isDestroyed()) {
          event.sender.send('pty:data', { id: sessionId, data })
        }
      },
      onExit: (code) => {
        if (!event.sender.isDestroyed()) {
          event.sender.send('pty:exit', { id: sessionId, code })
        }
      }
    })
    sessionId = result.id
    return result
  } catch (err) {
    return { error: err.message }
  }
})

ipcMain.on('pty:write', (_, { id, data }) => {
  if (typeof id === 'string' && typeof data === 'string') {
    ptyManager.write(id, data)
  }
})

ipcMain.on('pty:resize', (_, { id, cols, rows }) => {
  if (typeof id === 'string' && Number.isInteger(cols) && Number.isInteger(rows)) {
    ptyManager.resize(id, cols, rows)
  }
})

ipcMain.on('pty:kill', (_, { id }) => {
  if (typeof id === 'string') ptyManager.kill(id)
})

// ─── IPC: Files ───────────────────────────────────────────────────────────────

ipcMain.handle('files:read', (_, { path: filePath }) => {
  if (typeof filePath !== 'string') return { error: 'path must be a string' }
  try {
    return { contents: readFileSync(filePath, 'utf8') }
  } catch (err) {
    return { error: err.message }
  }
})

ipcMain.handle('files:watch', (event, { path: filePath }) => {
  if (typeof filePath !== 'string') return { error: 'path must be a string' }
  const watchId = String(watcherIdCounter++)
  try {
    const watcher = fsWatch(filePath, { persistent: false }, () => {
      try {
        const contents = readFileSync(filePath, 'utf8')
        if (!event.sender.isDestroyed()) {
          event.sender.send('files:changed', { watchId, path: filePath, contents })
        }
      } catch { /* file temporarily unavailable */ }
    })
    fileWatchers.set(watchId, watcher)
    return { watchId }
  } catch (err) {
    return { error: err.message }
  }
})

ipcMain.on('files:unwatch', (_, { watchId }) => {
  const id = String(watchId)
  const watcher = fileWatchers.get(id)
  if (watcher) {
    watcher.close()
    fileWatchers.delete(id)
  }
})

// ─── IPC: Dialog ──────────────────────────────────────────────────────────────

ipcMain.handle('dialog:openDir', async () => {
  if (!mainWindow) return { path: null }
  const result = await dialog.showOpenDialog(mainWindow, {
    properties: ['openDirectory'],
    title: 'Select Project Directory'
  })
  return result.canceled ? { path: null } : { path: result.filePaths[0] }
})

// ─── IPC: Config ──────────────────────────────────────────────────────────────

ipcMain.handle('config:get', () => loadConfig())
ipcMain.on('config:set', (_, config) => {
  if (config && typeof config === 'object') saveConfig(config)
})

// ─── App lifecycle ────────────────────────────────────────────────────────────

app.whenReady().then(() => {
  const webviewDist = resolve(app.getAppPath(), '..', 'pixel-agents', 'dist', 'webview')
  protocol.handle('pixel', (req) => {
    const url = new URL(req.url)
    let pathname = url.pathname
    if (pathname === '/' || pathname === '') pathname = '/index.html'
    const filePath = join(webviewDist, pathname)
    if (pathname === '/index.html') {
      try {
        let html = readFileSync(filePath, 'utf8')
        html = html.replace('<head>', '<head><script>window.__WS_HOST__="localhost:8080"</script>')
        return new Response(html, { headers: { 'content-type': 'text/html; charset=utf-8' } })
      } catch (err) {
        return new Response('Not found', { status: 404 })
      }
    }
    return net.fetch(pathToFileURL(filePath).toString())
  })
  startPixelServer()
  createWindow()
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('before-quit', () => {
  serverProcess?.kill()
  ptyManager.killAll()
})
app.on('window-all-closed', () => {
  ptyManager.killAll()
  if (process.platform !== 'darwin') app.quit()
})

process.on('SIGTERM', () => {
  ptyManager.killAll()
  app.quit()
})
