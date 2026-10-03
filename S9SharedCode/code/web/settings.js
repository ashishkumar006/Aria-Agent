/* Aria Settings — environment status + read-only config. */
(function () {
  'use strict';

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  async function health() {
    try {
      const d = await fetch('/api/health').then((r) => r.json());
      const up = d.gateway_up !== false;
      document.getElementById('health-dot').className = 'dot ' + (up ? 'g' : 'r');
      document.getElementById('health-text').textContent = up ? 'ready' : 'agent only';
      return up;
    } catch (e) {
      document.getElementById('health-dot').className = 'dot r';
      document.getElementById('health-text').textContent = 'unreachable';
      return false;
    }
  }

  async function count(url, pick) {
    try {
      const d = await fetch(url).then((r) => r.json());
      return pick(d);
    } catch (e) { return null; }
  }

  async function loadSettings() {
    const box = document.getElementById('statlist');
    if (!box.dataset.loaded) {
      box.innerHTML = '<div class="skel"><div class="bar w60"></div><div class="bar w85"></div></div>'.repeat(3);
    }
    // all probes in parallel — one slow endpoint must not stall the page
    const [gw, tools, sess, sch, tpl] = await Promise.all([
      health(),
      count('/api/tools', (d) => (d.tools || []).length),
      count('/api/sessions?limit=200', (d) => (d.sessions || []).length),
      count('/api/schedule', (d) => (d.schedules || []).length),
      count('/api/templates', (d) => (d.templates || []).length),
    ]);
    const rows = [
      ['agent', true, 'this server :8500'],
      ['gateway', gw, gw ? ':8109 reachable' : ':8109 down'],
      ['mcp tools', tools != null, tools == null ? 'unreadable' : `${tools} registered`],
      ['sessions', sess != null, sess == null ? 'unreadable' : `${sess} in first 200`],
      ['schedules', sch != null, sch == null ? 'unreadable' : `${sch} jobs`],
      ['templates', tpl != null, tpl == null ? 'unreadable' : `${tpl} saved`],
    ];
    box.innerHTML = '';
    box.dataset.loaded = '1';
    rows.forEach(([name, ok, note]) => {
      const b = document.createElement('div');
      b.className = 'icard';
      b.style.cursor = 'default';
      b.innerHTML = `<div class="i-top"><span class="i-t"></span>` +
        `<span class="pill ${ok ? 'ok' : 'err'}">${ok ? 'up' : 'down'}</span></div>` +
        `<div class="i-sub"><span></span></div>`;
      b.querySelector('.i-t').textContent = name;
      b.querySelector('.i-sub span').textContent = note;
      box.appendChild(b);
    });
    try {
      const d = await fetch('/api/config').then((r) => r.json());
      document.getElementById('cfg-raw').textContent = d.raw || d.error || '(empty)';
    } catch (e) {
      document.getElementById('cfg-raw').textContent = 'config unreadable: ' + e.message;
    }
  }

  window.loadSettings = loadSettings;
  loadSettings();
  setInterval(loadSettings, 30000);
})();
