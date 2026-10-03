/* Aria Skills — capability packs (Arcturus-style Bazaar) over the MCP
   tool catalog. A pack is a curated bundle the planner is offered;
   toggling a pack withholds/restores its tools via /api/config/tools. */
(function () {
  'use strict';

  const PACKS = [
    { id: 'research', name: 'Web Research', ico: '◉',
      desc: 'Search the web, fetch pages, query memory and index documents for retrieval.',
      tools: ['web_search', 'fetch_url', 'search_knowledge', 'index_document'] },
    { id: 'comms', name: 'Messaging', ico: '✉',
      desc: 'Email, Telegram, Slack and Discord — read, send and stay in the loop.',
      tools: ['send_email', 'gmail_query', 'gmail_refresh_token', 'send_telegram',
        'slack_message', 'slack_refresh_token', 'discord_message'] },
    { id: 'plan', name: 'Calendar & Scheduling', ico: '◷',
      desc: 'Calendar events plus the built-in job scheduler the Scheduler page drives.',
      tools: ['create_calendar_event', 'calendar_refresh_token', 'schedule_task',
        'list_scheduled', 'cancel_scheduled'] },
    { id: 'files', name: 'Workspace Files', ico: '▤',
      desc: 'List, read, create and edit files in the agent workspace.',
      tools: ['read_file', 'list_dir', 'create_file', 'update_file', 'edit_file'] },
    { id: 'integrations', name: 'Integrations', ico: '❖',
      desc: 'GitHub, Notion, computer use, currency and clock utilities.',
      tools: ['github_query', 'notion_query', 'computer_action', 'currency_convert', 'get_time'] },
  ];

  let catalog = [];
  let disabled = new Set();

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;',
    }[c]));
  }

  function packOf(tool) {
    const p = PACKS.find((k) => k.tools.includes(tool));
    return p ? p.name : 'Other';
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

  async function setTool(name, enabled) {
    const r = await fetch('/api/config/tools', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tool: name, enabled }),
    }).then((x) => x.json());
    if (r.error) throw new Error(r.error);
    disabled = new Set(r.disabled || []);
  }

  async function toggleTool(name) {
    try {
      await setTool(name, disabled.has(name));
      loadTools();
    } catch (e) {
      document.getElementById('pack-grid').insertAdjacentHTML('beforeend',
        '<div class="hint">toggle failed: ' + esc(e.message) + '</div>');
    }
  }

  async function togglePack(id) {
    const p = PACKS.find((k) => k.id === id);
    if (!p) return;
    const anyLive = p.tools.some((t) => !disabled.has(t));
    try {
      for (const t of p.tools) await setTool(t, !anyLive);
      loadTools();
    } catch (e) {
      document.getElementById('pack-grid').insertAdjacentHTML('beforeend',
        '<div class="hint">pack toggle failed: ' + esc(e.message) + '</div>');
    }
  }

  function scrollPack(id) {
    const el = document.getElementById('pack-' + id);
    if (el) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  async function loadTools() {
    const grid = document.getElementById('pack-grid');
    const rail = document.getElementById('packs');
    const tb = document.querySelector('#tool-table tbody');
    const q = (document.getElementById('tool-search').value || '').toLowerCase();
    if (!grid.dataset.loaded) {
      grid.innerHTML = '<div class="skel"><div class="bar w60"></div><div class="bar w85"></div></div>'.repeat(3);
    }
    try {
      const d = await fetch('/api/tools').then((r) => r.json());
      catalog = d.tools || [];
      try {
        const g = await fetch('/api/config/tools').then((r) => r.json());
        disabled = new Set(g.disabled || []);
      } catch (e) { /* assume all live */ }
      grid.dataset.loaded = '1';
      const known = new Set(catalog.map((t) => t.name));
      const nLive = catalog.filter((t) => !disabled.has(t.name)).length;
      document.getElementById('pack-count').textContent = PACKS.length + ' packs';
      document.getElementById('p-live').textContent = nLive + ' / ' + catalog.length;
      document.getElementById('p-disabled').textContent = disabled.size ? [...disabled].join(', ') : 'none';
      document.getElementById('st-packs').textContent = PACKS.length;
      const stL = document.getElementById('st-live');
      stL.textContent = nLive;
      stL.className = 's-v' + (nLive ? ' ok' : ' bad');
      const stO = document.getElementById('st-off');
      stO.textContent = disabled.size;
      stO.className = 's-v' + (disabled.size ? ' bad' : '');

      rail.innerHTML = '';
      grid.innerHTML = '';
      PACKS.forEach((p) => {
        const live = p.tools.filter((t) => known.has(t) && !disabled.has(t));
        const off = p.tools.length - live.length;
        const vis = !q || p.name.toLowerCase().includes(q) ||
          p.tools.some((t) => t.toLowerCase().includes(q));
        const rb = document.createElement('button');
        rb.className = 'icard' + (off ? '' : '');
        rb.innerHTML = `<div class="i-top"><span class="i-t">${esc(p.ico)}&nbsp;&nbsp;${esc(p.name)}</span>` +
          `<span class="pill ${off ? 'warn' : 'ok'}">${off ? off + ' off' : 'live'}</span></div>` +
          `<div class="i-sub"><span>${live.length}/${p.tools.length} tools live</span></div>`;
        rb.onclick = () => scrollPack(p.id);
        rail.appendChild(rb);
        if (!vis) return;
        const card = document.createElement('div');
        card.className = 'pack' + (off === p.tools.length ? ' off' : '');
        card.id = 'pack-' + p.id;
        card.innerHTML =
          `<div class="p-top"><span class="p-ico">${esc(p.ico)}</span>` +
          `<span class="p-name">${esc(p.name)}</span></div>` +
          `<div class="p-desc">${esc(p.desc)}</div>` +
          `<div class="p-tools">` + p.tools.map((t) =>
            `<span class="${disabled.has(t) ? 'off' : ''}" title="${known.has(t) ? 'registered' : 'not registered'}">${esc(t)}</span>`).join('') + `</div>` +
          `<div class="p-foot"><span class="hint">${live.length}/${p.tools.length} live</span>` +
          `<button onclick="togglePack('${p.id}')">${off ? 'Enable pack' : 'Disable pack'}</button></div>`;
        grid.appendChild(card);
      });

      const rows = catalog.filter((t) =>
        !q || t.name.toLowerCase().includes(q) ||
        (t.description || '').toLowerCase().includes(q) ||
        packOf(t.name).toLowerCase().includes(q));
      tb.innerHTML = rows.map((t) => {
        const off = disabled.has(t.name);
        return `<tr><td><span class="model">${esc(t.name)}</span></td>` +
          `<td>${esc(packOf(t.name))}</td>` +
          `<td>${esc((t.description || '').slice(0, 110))}</td>` +
          `<td><span class="pill ${off ? 'muted' : 'ok'}">${off ? 'withheld' : 'live'}</span></td>` +
          `<td class="num"><button class="icon-btn" onclick="toggleTool('${esc(t.name)}')">${off ? 'Enable' : 'Withhold'}</button></td></tr>`;
      }).join('') || '<tr><td colspan="5"><div class="empty"><span class="e-ico">❖</span><div class="e-t">No tools match</div></div></td></tr>';
    } catch (e) {
      grid.innerHTML = '<div class="empty"><span class="e-ico">⚠</span><div class="e-t">Skills unavailable</div>' +
        '<div class="e-s">' + esc(e.message) + '</div></div>';
    }
  }

  document.getElementById('tool-search').addEventListener('input', loadTools);

  window.loadTools = loadTools;
  window.toggleTool = toggleTool;
  window.togglePack = togglePack;
  health();
  loadTools();
  setInterval(health, 15000);
})();
