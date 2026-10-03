/* Aria Ledger — per-skill spend + turn history. */
(function () {
  'use strict';

  let scope = 'all';

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function fmtTs(ts) {
    if (!ts) return '—';
    return new Date(ts * 1000).toLocaleString();
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

  async function loadLedger() {
    const params = (scope === 'one' && document.getElementById('conv-input').value.trim())
      ? '?conversation_id=' + encodeURIComponent(document.getElementById('conv-input').value.trim()) : '';
    const stb = document.querySelector('#skill-table tbody');
    if (!stb.dataset.loaded) {
      stb.innerHTML = '<tr><td colspan="5"><div class="skel"><div class="bar w85"></div><div class="bar w60"></div></div></td></tr>';
    }
    try {
      const d = await fetch('/api/cost/by_skill' + params).then((r) => r.json());
      const rows = d.rows || [];
      const turns = d.turns || [];
      const t = d.totals || {};
      document.getElementById('led-tot').textContent =
        `$${Number(t.dollars || 0).toFixed(4)} · ${t.calls || 0} calls · ${t.skills || 0} skills`;
      document.getElementById('p-totals').textContent =
        `$${Number(t.dollars || 0).toFixed(4)} · ${t.calls || 0} calls`;
      document.getElementById('skill-tot').textContent = `${rows.length} skills`;
      document.getElementById('turn-tot').textContent = `${turns.length} turns`;
      document.querySelector('#skill-table tbody').innerHTML = rows.map((r) =>
        `<tr><td class="prov" style="padding:7px 10px;">${esc(r.skill)}</td><td style="padding:7px 10px;">${r.calls}</td>` +
        `<td style="padding:7px 10px;">${r.in_tokens}</td><td style="padding:7px 10px;">${r.out_tokens}</td>` +
        `<td style="padding:7px 10px;">$${Number(r.dollars || 0).toFixed(4)}</td></tr>`
      ).join('') || '<tr><td colspan="5"><div class="empty"><span class="e-ico">▦</span><div class="e-t">No spend yet</div><div class="e-s">Chat or research something and costs land here.</div></div></td></tr>';
      stb.dataset.loaded = '1';
      document.querySelector('#turn-table tbody').innerHTML = turns.map((x) =>
        `<tr><td style="padding:7px 10px;white-space:nowrap;">${esc(fmtTs(x.ts))}</td>` +
        `<td style="padding:7px 10px;">${esc(x.query || '(no query)')}</td>` +
        `<td style="padding:7px 10px;">${x.calls}</td>` +
        `<td style="padding:7px 10px;">$${Number(x.usd || 0).toFixed(4)}</td></tr>`
      ).join('') || '<tr><td colspan="4" style="padding:8px 10px;" class="hint">no turns recorded</td></tr>';
    } catch (e) {
      document.getElementById('led-tot').textContent = 'unavailable';
    }
  }

  document.querySelectorAll('#scopes .conv').forEach((b) => {
    b.onclick = () => {
      document.querySelectorAll('#scopes .conv').forEach((x) => x.classList.remove('on'));
      b.classList.add('on');
      scope = b.dataset.scope;
      loadLedger();
    };
  });
  document.querySelector('#scopes .conv[data-scope="all"]').classList.add('on');
  document.getElementById('conv-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      scope = 'one';
      document.querySelectorAll('#scopes .conv').forEach((x) =>
        x.classList.toggle('on', x.dataset.scope === 'one'));
      loadLedger();
    }
  });

  window.loadLedger = loadLedger;
  health();
  loadLedger();
  setInterval(health, 15000);
  setInterval(loadLedger, 30000);
})();
