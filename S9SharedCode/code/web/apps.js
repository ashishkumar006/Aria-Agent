/* Aria Apps (phase 1) — system overview board. Pure composition of
   existing read APIs, fetched in parallel, signature-guarded. */
(function () {
  'use strict';

  let lastSig = '';

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;',
    }[c]));
  }

  async function health() {
    try {
      const d = await fetch('/api/health').then((r) => r.json());
      const up = d.gateway_up !== false;
      document.getElementById('health-dot').className = 'dot ' + (up ? 'g' : 'r');
      document.getElementById('health-text').textContent = up ? 'ready' : 'agent only';
      return d;
    } catch (e) {
      document.getElementById('health-dot').className = 'dot r';
      document.getElementById('health-text').textContent = 'unreachable';
      return null;
    }
  }

  async function get(url) {
    try { return await fetch(url).then((r) => r.json()); }
    catch (e) { return null; }
  }

  async function loadApps() {
    const [h, sess, sch, tools, guard, spend, ev] = await Promise.all([
      health(),
      get('/api/sessions?limit=200'),
      get('/api/schedule'),
      get('/api/tools'),
      get('/api/config/tools'),
      get('/api/cost/by_skill'),
      get('/api/events?limit=12'),
    ]);
    const gwUp = !!(h && h.gateway_up !== false);
    const nSess = (sess && sess.sessions ? sess.sessions.length : 0);
    const jobs = (sch && sch.schedules) || [];
    const nJobs = jobs.filter((j) => j.enabled).length;
    const catalog = (tools && tools.tools) || [];
    const nOff = (guard && guard.disabled ? guard.disabled.length : 0);
    const rows = (spend && spend.rows) || [];
    const dollars = rows.reduce((a, r) => a + Number(r.dollars || 0), 0);
    const sig = [gwUp, nSess, nJobs, catalog.length, nOff, dollars.toFixed(4),
      ((ev && ev.events) || []).length].join('|');
    const set = (id, v, cls) => {
      const el = document.getElementById(id);
      el.textContent = v;
      if (cls) el.className = 's-v ' + cls;
    };
    set('a-agent', 'up', 'ok');
    set('a-gw', gwUp ? 'up' : 'down', gwUp ? 'ok' : 'bad');
    set('a-sess', nSess);
    set('a-jobs', nJobs, nJobs ? 'ok' : '');
    set('a-tools', (catalog.length - nOff) + '/' + catalog.length, nOff ? '' : 'ok');
    set('a-spend', '$' + dollars.toFixed(4));
    const pill = document.getElementById('sys-pill');
    pill.textContent = gwUp ? '● all systems live' : '● agent only';
    pill.className = 'pill ' + (gwUp ? 'ok' : 'warn');
    if (sig === lastSig) return; // steady state — leave the DOM alone
    lastSig = sig;
    // spend bars
    const max = Math.max(1e-9, ...rows.map((r) => Number(r.dollars || 0)));
    document.getElementById('spend-bars').innerHTML = rows.slice(0, 8).map((r) => {
      const pct = (Number(r.dollars || 0) / max * 100).toFixed(1);
      return `<div style="display:flex;gap:10px;align-items:center;margin:7px 0;font-size:12px;">` +
        `<span style="width:150px;flex:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(r.skill)}</span>` +
        `<span style="flex:1;height:8px;background:#1d1d25;border-radius:4px;overflow:hidden;">` +
        `<span style="display:block;height:100%;width:${pct}%;background:var(--accent);border-radius:4px;"></span></span>` +
        `<span class="num" style="width:90px;flex:none;text-align:right;font-variant-numeric:tabular-nums;">$${Number(r.dollars || 0).toFixed(4)}</span></div>`;
    }).join('') || '<div class="empty"><span class="e-ico">▦</span><div class="e-t">No spend yet</div></div>';
    // mini feed
    const evs = (ev && ev.events) || [];
    document.getElementById('mini-feed').innerHTML = evs.map((e) =>
      `<div class="log-line lv-${esc(e.level)}"><span class="lt">${esc(e.iso)}</span>` +
      `<span class="lsrc">${esc(e.src)}</span><span class="lmsg">${esc(e.msg)}</span></div>`).join('') ||
      '<div class="hint" style="padding:8px 12px;">no recent activity</div>';
  }

  window.loadApps = loadApps;
  loadApps();
  setInterval(loadApps, 15000);
})();
