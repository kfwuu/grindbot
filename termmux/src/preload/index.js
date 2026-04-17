/**
 * preload/index.js — contextBridge definitions for TermMux.
 * Exposes ONLY allowlisted IPC channels to the renderer.
 * renderer has zero direct Node/fs/electron access.
 */
import { contextBridge, ipcRenderer } from 'electron'

// ─── PTY ─────────────────────────────────────────────────────────────────────

contextBridge.exposeInMainWorld('pty', {
  /** Spawn a new PTY. Returns Promise<{ id } | { error }> */
  create: (opts) => ipcRenderer.invoke('pty:create', opts),

  /** Send keystrokes/data to PTY. */
  write: (id, data) => ipcRenderer.send('pty:write', { id, data }),

  /** Notify PTY of terminal resize. */
  resize: (id, cols, rows) => ipcRenderer.send('pty:resize', { id, cols, rows }),

  /** Kill a PTY session. */
  kill: (id) => ipcRenderer.send('pty:kill', { id }),

  /**
   * Subscribe to output data from a specific PTY.
   * @returns {() => void} Cleanup function — call on component unmount.
   */
  onData: (id, callback) => {
    const handler = (_, payload) => {
      if (payload.id === id) callback(payload.data)
    }
    ipcRenderer.on('pty:data', handler)
    return () => ipcRenderer.removeListener('pty:data', handler)
  },

  /**
   * Subscribe to exit event from a specific PTY.
   * @returns {() => void} Cleanup function.
   */
  onExit: (id, callback) => {
    const handler = (_, payload) => {
      if (payload.id === id) callback(payload.code)
    }
    ipcRenderer.on('pty:exit', handler)
    return () => ipcRenderer.removeListener('pty:exit', handler)
  }
})

// ─── Files ────────────────────────────────────────────────────────────────────

contextBridge.exposeInMainWorld('files', {
  /** Read a file. Returns Promise<{ contents } | { error }> */
  read: (path) => ipcRenderer.invoke('files:read', { path }),

  /** Start watching a file. Returns Promise<{ watchId } | { error }> */
  watch: (path) => ipcRenderer.invoke('files:watch', { path }),

  /** Stop watching a file. */
  unwatch: (watchId) => ipcRenderer.send('files:unwatch', { watchId }),

  /**
   * Subscribe to file change events for a specific watch.
   * @returns {() => void} Cleanup function.
   */
  onChanged: (watchId, callback) => {
    const handler = (_, payload) => {
      if (payload.watchId === watchId) callback(payload.contents)
    }
    ipcRenderer.on('files:changed', handler)
    return () => ipcRenderer.removeListener('files:changed', handler)
  }
})

// ─── Dialog ───────────────────────────────────────────────────────────────────

contextBridge.exposeInMainWorld('dialog', {
  /** Open a directory picker. Returns Promise<{ path } | { path: null }> */
  openDir: () => ipcRenderer.invoke('dialog:openDir')
})

// ─── Config ───────────────────────────────────────────────────────────────────

contextBridge.exposeInMainWorld('electronConfig', {
  /** Load persisted config. Returns Promise<object> */
  get: () => ipcRenderer.invoke('config:get'),

  /** Persist config object. */
  set: (config) => ipcRenderer.send('config:set', config)
})

// ─── Session Events ───────────────────────────────────────────────────────────

contextBridge.exposeInMainWorld('sessionEvents', {
  onOpenClaude: (callback) => {
    const handler = () => callback()
    ipcRenderer.on('request:openClaude', handler)
    return () => ipcRenderer.removeListener('request:openClaude', handler)
  }
})
