/* Aria desktop shell — Electron window over the local consoles.
 *
 * Strategy (phase 1: thin desktop wrapper):
 * the FastAPI agent (:8500) and gateway (:8109) stay the backends.
 * The shell health-gates, auto-starts missing backends, enforces a
 * single instance, handles aria:// deep links, and kills its children
 * on quit. Static console pages keep working in any browser in parallel.
 */
const { app, BrowserWindow, Menu, Tray, nativeImage, globalShortcut, shell, Notification, ipcMain, nativeTheme } = require('electron');
const { spawn } = require('child_process');
const http = require('http');
const path = require('path');
const fs = require('fs');

// In the repo the app sits at agent-desktop/ and the backends are its
// siblings. In a packaged portable build it runs from
// dist\Aria\resources\app, so a pointer file next to Aria.exe names the
// repository it should drive. A packaged app without that pointer logs the
// fallback and shows the boot splash's manual commands rather than pretending
// it can find the services.
const ROOT = (() => {
  if (app.isPackaged) {
    try {
      const pointer = path.join(path.dirname(process.execPath), 'aria-root.txt');
      const target = fs.readFileSync(pointer, 'utf8').trim();
      if (target && fs.existsSync(path.join(target, 'S9SharedCode'))) {
        return target;
      }
    } catch (e) { /* fall through */ }
  }
  return path.resolve(__dirname, '..');
})();
const GATEWAY_DIR = path.join(ROOT, 'llm_gatewayV9');
const AGENT_DIR = path.join(ROOT, 'S9SharedCode', 'code');
const AGENT_URL = process.env.ARIA_AGENT_URL || 'http://localhost:8500/console';
const AGENT_BASE = process.env.ARIA_AGENT_URL || 'http://localhost:8500';
const AUTO_START = (process.env.ARIA_AUTO_START || 'true').toLowerCase() !== 'false';

let win = null;
let tray = null;
const children = [];

const SHELL_LOG = path.join(__dirname, 'shell.log');
const SHELL_LOG_MAX = 1024 * 1024; // 1 MB, then keep the newest 200 KB

function log(line) {
  try {
    try {
      if (fs.existsSync(SHELL_LOG) && fs.statSync(SHELL_LOG).size > SHELL_LOG_MAX) {
        const tail = fs.readFileSync(SHELL_LOG, 'utf8').slice(-200 * 1024);
        fs.writeFileSync(SHELL_LOG, tail);
      }
    } catch (e) { /* rotation is best-effort */ }
    fs.appendFileSync(SHELL_LOG, `[${new Date().toISOString()}] ${line}\n`);
  } catch (e) { /* log file is best-effort */ }
}

// Resolve `uv` to an absolute path (env override wins) so a hijacked PATH
// can't redirect backend launches. Falls back to bare 'uv' with a warning.
function resolveUv() {
  if (process.env.ARIA_UV_PATH && fs.existsSync(process.env.ARIA_UV_PATH)) {
    return process.env.ARIA_UV_PATH;
  }
  const home = process.env.USERPROFILE || process.env.HOME || '';
  const candidates = [
    path.join(home, '.local', 'bin', process.platform === 'win32' ? 'uv.exe' : 'uv'),
    path.join(home, '.cargo', 'bin', process.platform === 'win32' ? 'uv.exe' : 'uv'),
  ];
  for (const c of candidates) {
    try { if (c && fs.existsSync(c)) return c; } catch (e) { /* next */ }
  }
  log('uv not found at known paths; falling back to PATH lookup');
  return 'uv';
}

function healthy(url) {
  return new Promise((resolve) => {
    const req = http.get(url, { timeout: 4000 }, (res) => resolve(res.statusCode === 200));
    req.on('error', () => resolve(false));
    req.on('timeout', () => { req.destroy(); resolve(false); });
  });
}

function spawnBackend(name, cwd, args) {
  const proc = spawn(resolveUv(), ['run', ...args], {
    cwd,
    windowsHide: true,
    // stderr is piped (not ignored) so backend crashes land in shell.log
    // instead of dying silently. stdout stays ignored (uvicorn access logs
    // would flood the file); rotation in log() bounds the rest.
    stdio: ['ignore', 'ignore', 'pipe'],
  });
  if (proc.stderr) {
    proc.stderr.on('data', (d) => {
      const line = String(d).trim();
      if (line) log(`${name} stderr: ${line.slice(0, 500)}`);
    });
  }
  proc.on('error', (e) => log(`${name} spawn failed: ${e.message}`));
  proc.on('exit', (code) => log(`${name} exited (${code})`));
  children.push({ name, proc });
  log(`${name} started (pid ${proc.pid})`);
  return proc;
}

// Kill the whole process tree: `uv run` spawns grandchildren (uvicorn)
// that a plain proc.kill() orphans.
function killTree(proc) {
  try {
    if (!proc || proc.exitCode !== null) return;
    if (process.platform === 'win32') {
      const { spawnSync } = require('child_process');
      spawnSync('taskkill', ['/pid', String(proc.pid), '/T', '/F'], { windowsHide: true });
    } else {
      try { process.kill(-proc.pid); } catch (e) { proc.kill(); }
    }
  } catch (e) { /* already gone */ }
}

// Readiness endpoints. Both must be UNPROTECTED.
//
// The gateway probe used `/v1/control/presence`, which answers 401 without
// `X-Gateway-Token`. `healthy()` reads "not 200" as "down", so a perfectly
// healthy gateway was reported as down on every launch: the shell spawned a
// second one, which could not bind :8109 and exited 1, and the log showed
// `[Errno 10048] ... only one usage of each socket address` — a crash-looking
// message on a completely normal start. `/health` is public and answers 200.
const GATEWAY_HEALTH = 'http://localhost:8109/health';
const AGENT_HEALTH = AGENT_BASE + '/api/health';

async function ensureBackends() {
  const gwUp = await healthy(GATEWAY_HEALTH);
  if (!gwUp && AUTO_START && fs.existsSync(path.join(GATEWAY_DIR, 'main.py'))) {
    spawnBackend('gateway', GATEWAY_DIR, ['main.py']);
  }
  for (let i = 0; i < 15; i++) {
    if (await healthy(GATEWAY_HEALTH)) break;
    await new Promise((r) => setTimeout(r, 2000));
  }
  const agentUp = await healthy(AGENT_HEALTH);
  if (!agentUp && AUTO_START && fs.existsSync(path.join(AGENT_DIR, 'agent_server.py'))) {
    spawnBackend('agent', AGENT_DIR, ['agent_server.py']);
  }
}

function errorPage() {
  // Presentational only. This page deliberately does NOT fetch anything.
  //
  // It used to poll /api/health itself, which never worked: a `data:` URL has
  // an opaque origin, and the agent's CORS policy allows exactly two origins
  // (http://localhost:8500 and http://127.0.0.1:8500). The preflight came back
  // 400, `r.ok` was never true, and the user sat on "Starting Aria
  // backends…" indefinitely - on the one path every cold launch takes. The
  // Retry button reloaded the same broken page.
  //
  // Node's `http` in the main process is not subject to CORS at all, so the
  // polling moved to waitForAgent() below, which navigates the window when the
  // agent answers. "Retry now" asks the main process to check immediately.
  return 'data:text/html;charset=utf-8,' + encodeURIComponent(
    '<body style="background:#090909;color:#ededed;font:14px system-ui;display:flex;' +
    'align-items:center;justify-content:center;height:100vh;margin:0">' +
    '<div style="text-align:center;max-width:34em;padding:0 24px">' +
    '<div style="font-size:28px;margin-bottom:8px">⚡</div>' +
    '<h3 style="margin:0 0 6px">Starting Aria backends…</h3>' +
    '<p id="st" style="color:#7b7b86">waiting for the agent on :8500</p>' +
    '<p style="color:#4c4c56;font-size:12px;line-height:1.7">This page is shown while the ' +
    'Python services boot, which takes 20-40 seconds on a cold start.<br>' +
    'If it never resolves, start them by hand:<br>' +
    '<code>cd S9SharedCode/code &amp;&amp; uv run agent_server.py</code><br>' +
    '<code>cd llm_gatewayV9 &amp;&amp; uv run main.py</code></p>' +
    '<button id="retry" style="background:#1b1b24;color:#ededed;' +
    'border:1px solid #33333b;border-radius:5px;padding:6px 14px;cursor:pointer">' +
    'Check now</button></div>' +
    '<script>const el=document.getElementById("st");let n=0;' +
    'setInterval(()=>{n++;if(el)el.textContent="still waiting for the agent on :8500 ("+' +
    'n*2+"s elapsed)";},2000);' +
    'document.getElementById("retry").addEventListener("click",()=>{' +
    'if(window.ariaDesktop&&window.ariaDesktop.checkNow){window.ariaDesktop.checkNow();}' +
    'if(el)el.textContent="checking…";});</script></body>');
}

// Poll from the MAIN process and navigate the window when the agent answers.
// CORS does not apply here, which is the whole point: the splash page could
// never do this itself.
let waitTimer = null;

async function waitForAgent(target) {
  if (waitTimer) clearInterval(waitTimer);
  for (let i = 0; i < 150; i++) {           // ~5 minutes, then give up quietly
    if (!win || win.isDestroyed()) return;
    // Only hijack a window that is still showing the splash. If the user
    // navigated somewhere (or the console loaded late), leave it alone.
    let showingSplash = false;
    try { showingSplash = (win.getURL() || '').startsWith('data:'); } catch (e) { /* gone */ }
    if (!showingSplash) return;
    if (await healthy(AGENT_BASE + '/api/health')) {
      log('agent healthy; loading console');
      win.loadURL(target || AGENT_URL);
      return;
    }
    await new Promise((r) => setTimeout(r, 2000));
  }
  log('gave up waiting for the agent after 5 minutes');
}

// aria://chat | aria://runs | aria://memory | aria://scheduler |
// aria://skills | aria://ledger | aria://settings
function handleDeepLink(url) {
  if (!url || !url.startsWith('aria://')) return;
  const page = url.replace('aria://', '').split('/')[0] || 'chat';
  const known = { chat: 'console', runs: 'runs', memory: 'memory', research: 'research',
    scheduler: 'scheduler', apps: 'apps', code: 'code', authoring: 'authoring',
    documents: 'documents', parts: 'parts',
    skills: 'skills', ledger: 'ledger', console: 'mission', settings: 'settings' };
  const target = 'http://localhost:8500/' + (known[page] || 'console');
  if (win) {
    win.loadURL(target);
    if (win.isMinimized()) win.restore();
    win.focus();
  }
}

// Page zoom. Chromium's own Ctrl+plus/minus/0 shortcuts are not wired up in a
// BrowserWindow unless an application menu exposes them, and with the menu
// hidden the window had no way to zoom at all. Handle the keys directly on the
// webContents so they work no matter which element has focus, and expose the
// same three actions through the hidden menu for discoverability.
const MIN_ZOOM_LEVEL = -3;
const MAX_ZOOM_LEVEL = 5;

function applyZoom(wc, delta) {
  if (!wc || wc.isDestroyed()) return;
  if (delta === 0) {
    wc.setZoomLevel(0);
    return;
  }
  const current = wc.getZoomLevel();
  const next = Math.max(MIN_ZOOM_LEVEL, Math.min(MAX_ZOOM_LEVEL, current + delta));
  wc.setZoomLevel(next);
}

function bindZoom(wc) {
  wc.on('before-input-event', (event, input) => {
    if (input.type !== 'keyDown' || input.alt) return;
    if (!input.control && !input.meta) return;
    const key = input.key;
    if (key === '+' || key === '=') {
      event.preventDefault();
      applyZoom(wc, 1);
    } else if (key === '-' || key === '_') {
      event.preventDefault();
      applyZoom(wc, -1);
    } else if (key === '0') {
      event.preventDefault();
      applyZoom(wc, 0);
    }
  });
}

async function createWindow() {
  // "Check now" on the boot splash: the page cannot poll itself, so it asks
  // here. Re-running waitForAgent restarts the loop and clears the old timer.
  ipcMain.removeAllListeners('aria:check-now');
  ipcMain.on('aria:check-now', () => {
    if (!win || win.isDestroyed()) return;
    let onSplash = false;
    try { onSplash = (win.getURL() || '').startsWith('data:'); } catch (e) { /* gone */ }
    if (onSplash) waitForAgent(AGENT_URL);
  });

  // Window controls for the custom title bar (titleBarStyle: 'hidden' removed
  // the OS one). The sender is the renderer; acting on `win` rather than on the
  // event is deliberate — a message can only ask for one of three verbs.
  ipcMain.removeAllListeners('aria:win');
  ipcMain.on('aria:win', (_e, verb) => {
    if (!win || win.isDestroyed()) return;
    if (verb === 'minimise') win.minimize();
    else if (verb === 'toggle-maximise') {
      if (win.isMaximized()) win.unmaximize(); else win.maximize();
    } else if (verb === 'close') win.close();
  });

  win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1100,
    minHeight: 700,
    // The menu (Reload console / Open gateway dashboard / devtools / Quit) is
    // kept but hidden until Alt is pressed. It was pinned open as a full row
    // of window height showing a single "Aria" item, which cost vertical space
    // on every page for no gain. The accelerators still work either way.
    autoHideMenuBar: true,
    backgroundColor: '#090909',
    // Created hidden and shown on `ready-to-show` (below): without this the
    // window appears a beat before its first paint, so a cold launch flashed a
    // bare frame over the desktop before the splash or the console drew.
    show: false,
    title: 'Aria',
    icon: fs.existsSync(path.join(__dirname, 'icon.png'))
      ? path.join(__dirname, 'icon.png') : undefined,
    // Drop the Windows title bar and draw our own ~34px strip (see the
    // `aria-dragbar` rule in console-frontend/src/index.css). `hidden` keeps
    // the frame's resize borders; `hiddenInset` is macOS-only. minWidth/
    // minHeight still apply, so the window cannot be shrunk to nothing.
    titleBarStyle: 'hidden',
    webPreferences: {
      preload: __dirname + '/preload.cjs',
      contextIsolation: true,
      // Explicit rather than implicit. This window renders model-generated
      // markdown from arbitrary documents and web pages; Node must never be
      // reachable from that content. It is already the Electron default, but
      // stating it means a future default change cannot quietly widen this.
      nodeIntegration: false,
      sandbox: true,
    },
  });

  bindZoom(win.webContents);

  // The window was created with `show: false`. `ready-to-show` fires once the
  // first frame has painted, so showing here means the user never sees an empty
  // window — only the boot splash or the console itself.
  win.once('ready-to-show', () => { if (win && !win.isDestroyed()) win.show(); });

  const ok = await healthy(AGENT_BASE + '/api/health');
  await win.loadURL(ok ? AGENT_URL : errorPage());
  // Only poll if we actually showed the splash. Main-process `http` is not
  // subject to CORS, so this is the first place the readiness check can work.
  if (!ok) waitForAgent(AGENT_URL);

  // Tell the renderer when the window is maximised by ANY route — its own
  // button, a double-click on the drag bar, or Win+Up — so the button's icon
  // and label stay truthful. Registered after the window exists, which is why
  // it could not live with the other IPC wiring above.
  const pushMax = () => {
    if (!win.isDestroyed()) {
      win.webContents.send('aria:maximised', win.isMaximized());
    }
  };
  for (const ev of ['maximize', 'unmaximize', 'restore']) {
    win.on(ev, pushMax);
  }

  win.webContents.setWindowOpenHandler(({ url }) => {
    if (!url.startsWith('http://localhost:') && !url.startsWith('http://127.0.0.1:')) {
      shell.openExternal(url);
      return { action: 'deny' };
    }
    return { action: 'allow' };
  });

  const submenu = [
    { label: 'Reload console', accelerator: 'CmdOrCtrl+R', click: () => win && win.reload() },
    { type: 'separator' },
    { label: 'Zoom in', accelerator: 'CmdOrCtrl+Plus',
      click: () => win && applyZoom(win.webContents, 1) },
    { label: 'Zoom out', accelerator: 'CmdOrCtrl+-',
      click: () => win && applyZoom(win.webContents, -1) },
    { label: 'Actual size', accelerator: 'CmdOrCtrl+0',
      click: () => win && applyZoom(win.webContents, 0) },
    { type: 'separator' },
    { label: 'Open gateway dashboard', click: () => shell.openExternal('http://localhost:8109/') },
  ];
  if (!app.isPackaged) {
    // DevTools only in dev builds — no reason to ship an inspector
    // with full backend access in production.
    submenu.push({
      label: 'Toggle devtools', accelerator: 'CmdOrCtrl+Shift+I',
      click: () => win && win.webContents.toggleDevTools(),
    });
  }
  submenu.push({ type: 'separator' }, { role: 'quit' });
  Menu.setApplicationMenu(Menu.buildFromTemplate([{ label: 'Aria', submenu }]));
  // Belt and braces alongside autoHideMenuBar: on Windows the menu is shown
  // on Alt, which is the behaviour we want, but setMenuBarVisibility makes it
  // explicit that the bar starts hidden.
  if (win.setMenuBarVisibility) win.setMenuBarVisibility(false);
}

function setupTray() {
  // A tray icon keeps the app in reach when the window is minimized, which is
  // the difference between "a browser tab in a frame" and a desktop app.
  const iconPath = path.join(__dirname, 'icon.png');
  if (!fs.existsSync(iconPath)) return;
  try {
    tray = new Tray(nativeImage.createFromPath(iconPath).resize({ width: 16, height: 16 }));
    tray.setToolTip('Aria');
    tray.setContextMenu(Menu.buildFromTemplate([
      { label: 'Open Aria', click: () => { if (win) { if (win.isMinimized()) win.restore(); win.show(); win.focus(); } } },
      { label: 'Open Code', click: () => handleDeepLink('aria://code') },
      { type: 'separator' },
      { label: 'Quit', role: 'quit' },
    ]));
    tray.on('click', () => {
      if (!win) return;
      if (win.isMinimized()) win.restore();
      win.show();
      win.focus();
    });
  } catch (e) {
    log('tray failed: ' + e.message);
    tray = null;
  }
}

// Keep-warm: a tiny HEAD ping every 20s stops the servers' event loops
// from going cold during idle (first request after ~45s idle costs ~2s
// on Windows while the process wakes). Cheap, loopback-only.
function keepWarm() {
  const pings = [AGENT_HEALTH, GATEWAY_HEALTH];
  setInterval(() => {
    for (const u of pings) {
      try {
        http.get(u, { timeout: 5000 }, (res) => res.resume()).on('error', () => {});
      } catch (e) { /* server down — autostart already handled it */ }
    }
  }, 20000);
}

function notify(title, body) {
  try {
    if (Notification.isSupported()) new Notification({ title, body }).show();
  } catch (e) { /* notifications are best-effort */ }
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', (event, argv) => {
    const deep = argv.find((a) => typeof a === 'string' && a.startsWith('aria://'));
    if (deep) handleDeepLink(deep);
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });

  app.whenReady().then(async () => {
    // Force dark native chrome. The console draws itself, but the surfaces the
    // shell does NOT own — context menus, native file/print dialogs and the
    // overlay scrollbars of anything Chromium renders natively — follow the OS
    // theme, which on a light Windows desktop means a white flash inside an
    // otherwise black app.
    nativeTheme.themeSource = 'dark';
    if (process.platform === 'win32' || process.platform === 'darwin') {
      try { app.setAsDefaultProtocolClient('aria'); } catch (e) { log('protocol failed: ' + e.message); }
    }
    // Non-blocking startup: the window opens immediately (showing the
    // waiting splash when backends aren't up yet) while servers boot in the
    // background. Previously the 30s gateway wait blocked the UI.
    setupTray();
    keepWarm();
    const backends = ensureBackends().catch((e) => log('backend startup: ' + e.message));
    await createWindow();
    await backends;
    notify('Aria', 'Desktop shell ready.');
    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });

  app.on('open-url', (event, url) => {
    event.preventDefault();
    handleDeepLink(url);
  });

  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit();
  });

  app.on('will-quit', () => {
    if (waitTimer) clearInterval(waitTimer);
    globalShortcut.unregisterAll();
    for (const { name, proc } of children) {
      killTree(proc);
      log(`${name} stopped`);
    }
  });
}
