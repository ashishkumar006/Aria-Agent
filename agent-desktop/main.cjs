/* Aria desktop shell — Electron window over the local consoles.
 *
 * Strategy (phase 1: thin desktop wrapper):
 * the FastAPI agent (:8500) and gateway (:8109) stay the backends.
 * The shell health-gates, auto-starts missing backends, enforces a
 * single instance, handles aria:// deep links, and kills its children
 * on quit. Static console pages keep working in any browser in parallel.
 */
const { app, BrowserWindow, Menu, Tray, globalShortcut, shell, Notification } = require('electron');
const { spawn } = require('child_process');
const http = require('http');
const path = require('path');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '..');
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

async function ensureBackends() {
  const gwUp = await healthy('http://localhost:8109/v1/control/presence');
  if (!gwUp && AUTO_START && fs.existsSync(path.join(GATEWAY_DIR, 'main.py'))) {
    spawnBackend('gateway', GATEWAY_DIR, ['main.py']);
  }
  for (let i = 0; i < 15; i++) {
    if (await healthy('http://localhost:8109/v1/control/presence')) break;
    await new Promise((r) => setTimeout(r, 2000));
  }
  const agentUp = await healthy(AGENT_BASE + '/api/health');
  if (!agentUp && AUTO_START && fs.existsSync(path.join(AGENT_DIR, 'agent_server.py'))) {
    spawnBackend('agent', AGENT_DIR, ['agent_server.py']);
  }
}

function errorPage() {
  // Auto-retries: polls the agent health endpoint and reloads into the
  // console the moment the backend answers — no manual Retry needed.
  return 'data:text/html;charset=utf-8,' + encodeURIComponent(
    '<body style="background:#090909;color:#ededed;font:14px system-ui;display:flex;' +
    'align-items:center;justify-content:center;height:100vh;margin:0">' +
    '<div style="text-align:center"><div style="font-size:28px;margin-bottom:8px">⚡</div>' +
    '<h3 style="margin:0 0 6px">Starting Aria backends…</h3>' +
    '<p id="st" style="color:#7b7b86">waiting for the agent on :8500</p>' +
    '<p style="color:#4c4c56;font-size:12px">Start manually:<br><code>cd S9SharedCode/code && uv run agent_server.py</code></p>' +
    '<button onclick="location.reload()" style="background:#1b1b24;color:#ededed;' +
    'border:1px solid #33333b;border-radius:5px;padding:6px 14px;cursor:pointer">Retry now</button></div>' +
    '<script>let n=0;const t=setInterval(async()=>{n++;try{const r=await fetch("http://localhost:8500/api/health");' +
    'if(r.ok){clearInterval(t);location.href="http://localhost:8500/console";}}catch(e){}' +
    'const el=document.getElementById("st");if(el)el.textContent="waiting for the agent on :8500 ("+n*2+"s)";},2000);</script></body>');
}

// aria://chat | aria://runs | aria://memory | aria://scheduler |
// aria://skills | aria://ledger | aria://settings
function handleDeepLink(url) {
  if (!url || !url.startsWith('aria://')) return;
  const page = url.replace('aria://', '').split('/')[0] || 'chat';
  const known = { chat: 'console', runs: 'runs', memory: 'memory', research: 'research',
    scheduler: 'scheduler', apps: 'apps',
    skills: 'skills', ledger: 'ledger', console: 'mission', settings: 'settings' };
  const target = 'http://localhost:8500/' + (known[page] || 'console');
  if (win) {
    win.loadURL(target);
    if (win.isMinimized()) win.restore();
    win.focus();
  }
}

async function createWindow() {
  win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1100,
    minHeight: 700,
    backgroundColor: '#090909',
    title: 'Aria',
    webPreferences: {
      preload: __dirname + '/preload.cjs',
      contextIsolation: true,
    },
  });

  const ok = await healthy(AGENT_BASE + '/api/health');
  await win.loadURL(ok ? AGENT_URL : errorPage());

  win.webContents.setWindowOpenHandler(({ url }) => {
    if (!url.startsWith('http://localhost:') && !url.startsWith('http://127.0.0.1:')) {
      shell.openExternal(url);
      return { action: 'deny' };
    }
    return { action: 'allow' };
  });

  const submenu = [
    { label: 'Reload console', accelerator: 'CmdOrCtrl+R', click: () => win && win.reload() },
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
}

function setupTray() {
  // No icon asset yet — tray activates once an icon file ships.
  // Keeping the hook (no-op) so the wiring point is explicit.
  tray = null;
}

// Keep-warm: a tiny HEAD ping every 20s stops the servers' event loops
// from going cold during idle (first request after ~45s idle costs ~2s
// on Windows while the process wakes). Cheap, loopback-only.
function keepWarm() {
  const pings = [
    'http://localhost:8500/api/health',
    'http://localhost:8109/v1/control/presence',
  ];
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
    if (process.platform === 'win32' || process.platform === 'darwin') {
      try { app.setAsDefaultProtocolClient('aria'); } catch (e) { log('protocol failed: ' + e.message); }
    }
    // Non-blocking startup: the window opens immediately (showing the
    // auto-retrying splash when backends aren't up yet) while servers boot
    // in the background. Previously the 30s gateway wait blocked the UI.
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
    globalShortcut.unregisterAll();
    for (const { name, proc } of children) {
      killTree(proc);
      log(`${name} stopped`);
    }
  });
}
