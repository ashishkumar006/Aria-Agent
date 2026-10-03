/* Aria Memory — chunk inspector over the gateway memory plane. */
(function () {
  'use strict';

  const DRAWERS = ['working', 'episode', 'fact', 'playbook', 'policy', 'audit', 'document'];
  let drawer = '';

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
    } catch (e) {
      document.getElementById('health-dot').className = 'dot r';
      document.getElementById('health-text').textContent = 'unreachable';
    }
  }

  const DRAWER_ICON = {working: '◈', episode: '▤', fact: '✎', playbook: '❖', policy: '⬢', audit: '▦', document: '▭'};
  function renderDrawers() {
    const box = document.getElementById('drawers');
    box.innerHTML = '';
    const all = document.createElement('button');
    all.className = 'icard' + (drawer === '' ? ' on' : '');
    all.innerHTML = '<div class="i-top"><span class="i-t">all drawers</span><span class="pill info">7</span></div>' +
      '<div class="i-sub"><span>search everywhere</span></div>';
    all.onclick = () => { drawer = ''; document.getElementById('p-drawer').textContent = 'all'; renderDrawers(); loadMem(); };
    box.appendChild(all);
    DRAWERS.forEach((d) => {
      const b = document.createElement('button');
      b.className = 'icard' + (drawer === d ? ' on' : '');
      b.innerHTML = `<div class="i-top"><span class="i-t">${DRAWER_ICON[d] || '○'}&nbsp;&nbsp;${esc(d)}</span></div>` +
        `<div class="i-sub"><span>memory drawer</span></div>`;
      b.onclick = () => { drawer = d; document.getElementById('p-drawer').textContent = d; renderDrawers(); loadMem(); };
      box.appendChild(b);
    });
    document.getElementById('drawer-count').textContent = DRAWERS.length + 1;
  }

  async function loadMem() {
    const box = document.getElementById('chunks');
    const q = document.getElementById('mem-search').value.trim();
    const hide = document.getElementById('mem-hide').checked;
    const params = new URLSearchParams({limit: 60});
    if (q) params.set('q', q);
    if (drawer) params.set('drawers', drawer);
    if (hide) params.set('hide_superseded', 'true');
    box.innerHTML = '<div class="skel"><div class="bar w85"></div><div class="bar w60"></div></div>'.repeat(3);
    try {
      const d = await fetch('/api/memory?' + params).then((r) => r.json());
      const items = d.items || [];
      document.getElementById('p-shown').textContent = items.length + ' items';
      if (d.error) {
        box.innerHTML = `<div class="msg err"><div class="who">Error</div><div class="bubble"></div></div>`;
        box.querySelector('.bubble').textContent = d.error;
        return;
      }
      if (!items.length) {
        box.innerHTML = '<div class="empty"><span class="e-ico">⌘</span>' +
          '<div class="e-t">Nothing stored here</div>' +
          '<div class="e-s">Write a memory above, or try another drawer or query.</div></div>';
        return;
      }
      box.innerHTML = '';
      items.forEach((it) => {
        const card = document.createElement('section');
        card.className = 'dag';
        const sup = it.superseded_by ? `<span class="pill err">superseded</span>` : `<span class="pill ok">${esc(it.kind || '?')}</span>`;
        card.innerHTML =
          `<div class="dag-head"><span>${sup}</span>` +
          (it.drawer ? `<span class="pill cap">${esc(it.drawer)}</span>` : '') +
          `<span class="spacer" style="flex:1"></span><span class="hint">${esc(it.id || '').slice(0, 18)}</span></div>` +
          `<div style="padding:9px 12px;font-size:12.5px;"></div>` +
          `<div style="padding:0 12px 9px;display:flex;gap:5px;flex-wrap:wrap;">` +
          `${(it.keywords || []).map((k) => `<span class="chip">${esc(k)}</span>`).join('')}` +
          `<span class="hint">${esc(it.source || '')}</span></div>`;
        card.querySelector('.dag-head + div').textContent = it.descriptor || '(no descriptor)';
        box.appendChild(card);
      });
    } catch (e) {
      box.innerHTML = '<div class="hint">memory unavailable: ' + esc(e.message) + '</div>';
    }
  }

  async function remember() {
    const out = document.getElementById('write-out');
    const descriptor = document.getElementById('w-desc').value.trim();
    if (!descriptor) { out.textContent = 'descriptor required.'; return; }
    const kws = document.getElementById('w-kw').value.split(',').map((s) => s.trim()).filter(Boolean);
    out.textContent = 'saving…';
    try {
      const r = await fetch('/api/memory/remember', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({kind: 'fact', descriptor, keywords: kws, value: {},
          source: 'dashboard', run_id: 'dashboard-' + Date.now()}),
      }).then((x) => x.json());
      out.textContent = (r && r.status === 'ok')
        ? 'remembered' + (r.id ? ' ' + r.id : '')
        : JSON.stringify(r).slice(0, 200);
      if (String(out.textContent).startsWith('remembered')) {
        document.getElementById('w-desc').value = '';
        loadMem();
      }
    } catch (e) { out.textContent = e.message; }
  }

  async function wipeSession() {
    const out = document.getElementById('write-out');
    const sid = document.getElementById('w-sess').value.trim();
    if (!sid) { out.textContent = 'session id required.'; return; }
    if (!confirm(`Wipe gateway memory for session ${sid}?`)) return;
    out.textContent = 'wiping…';
    try {
      const r = await fetch('/api/memory?session_id=' + encodeURIComponent(sid), {method: 'DELETE'}).then((x) => x.json());
      out.textContent = 'wiped: ' + JSON.stringify(r).slice(0, 160);
      loadMem();
    } catch (e) { out.textContent = e.message; }
  }

  function toggleWrite() {
    const b = document.getElementById('write-body');
    const hidden = b.style.display === 'none';
    b.style.display = hidden ? '' : 'none';
    const btn = document.querySelector('#write-panel .dag-head button');
    if (btn) btn.textContent = hidden ? 'hide' : 'show';
  }

  document.getElementById('mem-search').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') loadMem();
  });

  window.loadMem = loadMem;
  window.remember = remember;
  window.wipeSession = wipeSession;
  window.toggleWrite = toggleWrite;
  renderDrawers();
  health();
  loadMem();
  setInterval(health, 15000);
})();
