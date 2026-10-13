/* Minimal preload: versions plus one readiness action.
 *
 * No backend access is exposed here yet — the console talks to the agent over
 * HTTP exactly as the browser build does, which is deliberate: the shell owns
 * no console state, so a console fix shows up in the app with no Electron
 * change at all.
 *
 * `checkNow` exists for the boot splash. That page is a `data:` URL, so it
 * cannot poll /api/health itself (its origin is opaque and the agent's CORS
 * policy allows only the two real origins). It asks the main process, which
 * uses Node's http and is not subject to CORS. */
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('ariaDesktop', {
  versions: {
    node: process.versions.node,
    electron: process.versions.electron,
  },
  shell: 'electron',
  checkNow: () => ipcRenderer.send('aria:check-now'),

  /* Window controls, because `titleBarStyle: 'hidden'` removed the OS title
     bar along with the only way to move or close the window. These are
     deliberately methods that send a message rather than direct main-process
     handles: nothing here gives the renderer any authority of its own. */
  minimise: () => ipcRenderer.send('aria:win', 'minimise'),
  toggleMaximise: () => ipcRenderer.send('aria:win', 'toggle-maximise'),
  close: () => ipcRenderer.send('aria:win', 'close'),
  /* Subscribe to maximise/restore so the button can show the right icon and
     aria-label. Returns an unsubscribe function. */
  onMaxChange: (cb) => {
    const h = (_e, m) => cb(!!m);
    ipcRenderer.on('aria:maximised', h);
    return () => ipcRenderer.removeListener('aria:maximised', h);
  },
});
