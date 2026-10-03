/* Minimal preload: versions only. No backend access from here yet —
 * the console talks to the agent over HTTP like the browser build. */
const { contextBridge } = require('electron');

contextBridge.exposeInMainWorld('ariaDesktop', {
  versions: {
    node: process.versions.node,
    electron: process.versions.electron,
  },
  shell: 'electron',
});
