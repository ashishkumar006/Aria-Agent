/* Aria Runs — run list + DAG detail + compare. Read-only over
   /api/runs/summary and the session graph endpoints. */
(function () {
  'use strict';

  const dagPanel = document.getElementById('dag-panel');
  const dagSvg = document.getElementById('dag');
  const dagCount = document.getElementById('dag-count');
  const dagState = document.getElementById('dag-state');
  const nodeDetail = document.getElementById('node-detail');

  let runs = [];
  let currentSid = '';
  let currentQuery = '';
  let cmp = [];
  let lastRunsSig = '', lastGraphSig = '', lastGraphPending = null, rafQueued = false;

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function ago(ts) {
    if (!ts) return '—';
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return Math.round(s) + 's ago';
    if (s < 3600) return Math.round(s / 60) + 'm ago';
    if (s < 86400) return Math.round(s / 3600) + 'h ago';
    return Math.round(s / 86400) + 'd ago';
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

  function runStatus(r) {
    const keys = Object.keys(r.status_counts || {}).map((k) => k.toLowerCase());
    if (keys.some((k) => /fail|error/.test(k))) return 'r';
    if (keys.some((k) => /run|active|working/.test(k))) return 'v';
    if (keys.some((k) => /pend|queue|wait/.test(k))) return 'o';
    return 'g';
  }

  function pillFor(st) {
    if (st === 'g') return '<span class="pill ok">done</span>';
    if (st === 'r') return '<span class="pill err">error</span>';
    if (st === 'v') return '<span class="pill info">live</span>';
    return '<span class="pill muted">queued</span>';
  }

  async function refreshRuns() {
    const box = document.getElementById('runs');
    const first = !runs.length && !box.dataset.loaded;
    if (first) {
      box.innerHTML = '<div class="skel"><div class="bar w85"></div><div class="bar w60"></div></div>'.repeat(4);
    }
    const q = (document.getElementById('run-search').value || '').toLowerCase();
    const f = document.getElementById('run-filter').value;
    try {
      const d = await fetch('/api/runs/summary?limit=200').then((r) => r.json());
      runs = d.runs || [];
      const t = d.totals || {};
      // signature guard: totals update cheaply, list re-renders only on change
      document.getElementById('run-count').textContent = t.runs || 0;
      document.getElementById('tot-badge').textContent =
        `${t.runs || 0} runs · ${t.nodes || 0} nodes · $${Number(t.dollars || 0).toFixed(4)}`;
      document.getElementById('p-totals').textContent =
        `${t.runs || 0} runs · ${t.nodes || 0} nodes · $${Number(t.dollars || 0).toFixed(4)}`;
      document.getElementById('st-runs').textContent = t.runs || 0;
      document.getElementById('st-nodes').textContent = t.nodes || 0;
      document.getElementById('st-spend').textContent = '$' + Number(t.dollars || 0).toFixed(4);
      const nErr = runs.filter((s) => runStatus(s) === 'r').length;
      const stErr = document.getElementById('st-err');
      stErr.textContent = nErr;
      stErr.className = 's-v' + (nErr ? ' bad' : ' ok');
      const vis = runs.filter((s) => {
        if (q && !(s.query || '').toLowerCase().includes(q)) return false;
        const st = runStatus(s);
        if (f === 'ok' && st !== 'g') return false;
        if (f === 'err' && st !== 'r') return false;
        return true;
      });
      const sig = vis.length + ':' + vis.map((s) => s.session_id + (s.updated || '')).join(',') +
        ':' + Array.from(document.querySelectorAll('input[data-cmp]:checked')).map((el) => el.getAttribute('data-cmp')).join(',');
      if (sig === lastRunsSig && box.dataset.loaded) return; // nothing changed — leave DOM alone
      lastRunsSig = sig;
      if (!vis.length) {
        box.innerHTML = '<div class="empty"><span class="e-ico">▤</span>' +
          '<div class="e-t">No runs yet</div>' +
          '<div class="e-s">Ask anything in Chat and every execution lands here with its graph, cost and replay.</div></div>';
        box.dataset.loaded = '1';
        return;
      }
      box.innerHTML = '';
      box.dataset.loaded = '1';
      vis.forEach((s) => {
        const b = document.createElement('div');
        b.className = 'icard' + (s.session_id === currentSid ? ' on' : '');
        const st = runStatus(s);
        const skills = (s.skills || []).slice(0, 3).join(' · ');
        b.innerHTML =
          `<div class="i-top"><input type="checkbox" data-cmp="${esc(s.session_id)}"` +
          `${cmp.includes(s.session_id) ? ' checked' : ''} title="compare" style="accent-color:var(--accent)">` +
          `<span class="i-t"></span>${pillFor(st)}</div>` +
          `<div class="i-sub"><span>${s.nodes || 0} nodes</span><span>$${Number(s.usd || 0).toFixed(4)}</span>` +
          `<span>${ago(s.updated)}</span>${skills ? `<span>${esc(skills)}</span>` : ''}</div>`;
        b.querySelector('.i-t').textContent = (s.query || '(untitled)').slice(0, 90);
        b.querySelector('.i-t').style.cursor = 'pointer';
        b.querySelector('.i-t').onclick = () => selectRun(s.session_id);
        box.appendChild(b);
      });
    } catch (e) {
      box.innerHTML = '<div class="hint" style="padding:4px 10px;">runs unavailable</div>';
    }
  }

  document.getElementById('runs').addEventListener('change', (e) => {
    const cb = e.target.closest ? e.target.closest('input[data-cmp]') : null;
    if (!cb) return;
    const sid = cb.getAttribute('data-cmp');
    if (cb.checked) {
      cmp.push(sid);
      if (cmp.length > 2) {
        const drop = cmp.shift();
        const old = document.querySelector(`input[data-cmp="${drop}"]`);
        if (old) old.checked = false;
      }
    } else {
      cmp = cmp.filter((x) => x !== sid);
    }
    renderCompare();
  });

  function findRun(sid) {
    return runs.find((s) => s.session_id === sid);
  }

  async function selectRun(sid) {
    const s = findRun(sid);
    if (!s) return;
    rpExit();
    document.getElementById('insp').hidden = true;
    currentSid = sid;
    document.querySelectorAll('#runs .conv').forEach((el) => el.classList.remove('on'));
    document.getElementById('crumb-name').textContent = (s.query || 'Run').slice(0, 40);
    const emp = document.getElementById('empty');
    if (emp) emp.remove();
    dagPanel.hidden = false;
    dagState.textContent = 'final';
    dagState.className = 'pill ok';
    await loadGraph(sid, false);
  }

  function renderCompare() {
    const panel = document.getElementById('cmp-panel');
    const badge = document.getElementById('p-compare');
    if (cmp.length !== 2) {
      panel.hidden = true;
      badge.textContent = cmp.length === 1 ? 'tick one more run' : 'tick two runs';
      return;
    }
    const [a, b] = cmp.map(findRun);
    if (!a || !b) return;
    panel.hidden = false;
    badge.textContent = 'comparing 2 runs';
    document.getElementById('cmp-a').textContent = (a.query || a.session_id).slice(0, 40);
    document.getElementById('cmp-b').textContent = (b.query || b.session_id).slice(0, 40);
    const rows = [
      ['Nodes', a.nodes, b.nodes],
      ['Status', JSON.stringify(a.status_counts), JSON.stringify(b.status_counts)],
      ['Skills', (a.skills || []).join(', ') || '—', (b.skills || []).join(', ') || '—'],
      ['Spend', '$' + Number(a.usd || 0).toFixed(4), '$' + Number(b.usd || 0).toFixed(4)],
      ['Updated', ago(a.updated), ago(b.updated)],
    ];
    document.querySelector('#cmp-table tbody').innerHTML = rows.map(([k, x, y]) =>
      `<tr><td style="color:var(--faint)">${esc(k)}</td><td>${esc(String(x))}</td><td>${esc(String(y))}</td></tr>`
    ).join('');
  }

  function clearCompare() {
    cmp = [];
    document.querySelectorAll('input[data-cmp]').forEach((el) => { el.checked = false; });
    renderCompare();
  }

  /* ---------- DAG (same renderer as chat) ---------- */
  const GLYPH = {planner: '◈', researcher: '⌕', retriever: '⌘', browser: '▤', computer: '▣',
    coder: '⌨', distiller: '⬔', critic: '✓', formatter: '✎', summariser: '☰', action: '➤', sandbox_executor: '⚙'};
  const NW = 168, NH = 44, GX = 64, GY = 14;

  let replayOn = false, replayIdx = -1, replayOrder = [], replayTimer = null, replaySpeedMs = 1000;
  let lastGraph = null, inspTab = 'overview', inspNid = null, inspDump = null, lastQuery = '';

  function layout(nodes, edges) {
    const children = {}, indeg = {}, byId = {};
    nodes.forEach((n) => { byId[n.id] = n; indeg[n.id] = 0; });
    edges.forEach((e) => {
      if (!byId[e.from] || !byId[e.to]) return;
      (children[e.from] = children[e.from] || []).push(e.to);
      indeg[e.to]++;
    });
    const depth = {}, queue = [];
    nodes.forEach((n) => { if (!indeg[n.id]) { depth[n.id] = 0; queue.push(n.id); } });
    nodes.forEach((n) => { if (!(n.id in depth)) { depth[n.id] = 0; queue.push(n.id); } });
    while (queue.length) {
      const id = queue.shift();
      (children[id] || []).forEach((ch) => {
        if ((depth[ch] || 0) < depth[id] + 1) depth[ch] = depth[id] + 1;
        if (--indeg[ch] === 0) queue.push(ch);
      });
    }
    const cols = {};
    nodes.forEach((n) => { (cols[depth[n.id]] = cols[depth[n.id]] || []).push(n); });
    return {cols, children};
  }

  function statusClass(st) {
    const s = String(st || '').toLowerCase();
    if (/fail|error/.test(s)) return 'st-err';
    if (/run|active|working/.test(s)) return 'st-run';
    if (/ok|complete|done|success|pass/.test(s)) return 'st-ok';
    return 'st-idle';
  }

  function statusWord(st) {
    const s = String(st || '').toLowerCase();
    if (/fail|error/.test(s)) return 'Failed';
    if (/run|active|working/.test(s)) return 'Running';
    if (/ok|complete|done|success|pass/.test(s)) return 'Completed';
    return String(st || 'pending');
  }

  function renderDAG(data) {
    lastGraph = data;
    const nodes = data.nodes || [], edges = data.edges || [];
    if (data.query) lastQuery = data.query;
    dagCount.textContent = nodes.length ? `${nodes.length} nodes` : '';
    if (!nodes.length) {
      dagSvg.setAttribute('viewBox', '0 0 300 40');
      dagSvg.setAttribute('width', 300);
      dagSvg.innerHTML = '<text x="12" y="24" fill="#4c4c56" font-size="11">no graph recorded</text>';
      return;
    }
    const {cols} = layout(nodes, edges);
    const depths = Object.keys(cols).map(Number).sort((a, b) => a - b);
    const pos = {};
    depths.forEach((d, ci) => {
      cols[d].forEach((n, ri) => { pos[n.id] = {x: 12 + ci * (NW + GX), y: 12 + ri * (NH + GY)}; });
    });
    const W = 12 + depths.length * (NW + GX);
    const H = 12 + Math.max(...depths.map((d) => cols[d].length)) * (NH + GY);
    dagSvg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    dagSvg.setAttribute('width', W);
    let s = '<defs><marker id="ah" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto">' +
      '<path d="M0,0 L7,3.5 L0,7" fill="none" stroke="#3a3a46" stroke-width="1.2"/></marker></defs>';
    edges.forEach((e) => {
      const a = pos[e.from], b = pos[e.to];
      if (!a || !b) return;
      const x1 = a.x + NW, y1 = a.y + NH / 2, x2 = b.x, y2 = b.y + NH / 2;
      const mx = Math.round((x1 + x2) / 2);
      s += `<path class="edge" d="M${x1} ${y1} H${mx} V${y2} H${x2}" marker-end="url(#ah)"/>`;
    });
    nodes.forEach((n) => {
      const p = pos[n.id];
      const sk = String(n.skill || n.id);
      const glyph = GLYPH[sk.toLowerCase()] || '●';
      const cap = esc(sk.toUpperCase().slice(0, 22));
      const word = esc(statusWord(n.status));
      const cls = statusClass(n.status);
      s += `<g class="node" data-nid="${esc(n.id)}">` +
        `<rect x="${p.x}" y="${p.y}" width="${NW}" height="${NH}" rx="6"></rect>` +
        `<circle class="port" cx="${p.x}" cy="${p.y + NH / 2}" r="3.2"/>` +
        `<circle class="port" cx="${p.x + NW}" cy="${p.y + NH / 2}" r="3.2"/>` +
        `<rect class="ibox" x="${p.x + 10}" y="${p.y + 10}" width="24" height="24" rx="5"></rect>` +
        `<text class="glyph" x="${p.x + 22}" y="${p.y + 26}" text-anchor="middle">${glyph}</text>` +
        `<text class="t-cap" x="${p.x + 40}" y="${p.y + 18}">${cap}</text>` +
        `<text class="t-sub ${cls}" x="${p.x + 40}" y="${p.y + 32}">${word}</text></g>`;
    });
    dagSvg.innerHTML = s;
    if (replayOn) paintReplay();
  }

  dagSvg.addEventListener('click', (e) => {
    const g = e.target.closest ? e.target.closest('.node') : null;
    if (!g) return;
    showNode(g.getAttribute('data-nid'));
  });

  function kv(k, v, cls) {
    return `<div class="kv"><span class="kk">${esc(k)}</span><span class="vv${cls ? ' ' + cls : ''}">${esc(v)}</span></div>`;
  }

  function fmtDur(d) {
    const a = d.started_at, b = d.completed_at;
    if (typeof a === 'number' && typeof b === 'number' && b >= a) return (b - a).toFixed(2) + 's';
    if (typeof d.elapsed_s === 'number') return d.elapsed_s.toFixed(2) + 's';
    const r = d.result || {};
    if (typeof r.elapsed_s === 'number') return r.elapsed_s.toFixed(2) + 's';
    return '—';
  }

  async function showNode(nid) {
    if (!nid || !currentSid) return;
    document.querySelectorAll('#dag .node').forEach((el) => el.classList.remove('sel'));
    const g = document.querySelector(`#dag .node[data-nid="${CSS.escape(nid)}"]`);
    if (g) g.classList.add('sel');
    dagPanel.classList.add('sel');
    inspNid = nid;
    inspDump = null;
    document.getElementById('insp').hidden = false;
    document.getElementById('insp-name').textContent = nid;
    document.getElementById('insp-id').textContent = nid;
    document.getElementById('insp-status').innerHTML = '<span class="pill cap">loading…</span>';
    document.getElementById('insp-query').textContent = lastQuery || '—';
    document.getElementById('insp-body').innerHTML = '<span class="hint">loading node…</span>';
    try {
      const d = await fetch(`/api/sessions/${encodeURIComponent(currentSid)}/nodes/${encodeURIComponent(nid)}`).then((r) => r.json());
      if (d.error) throw new Error(d.error);
      inspDump = d;
      paintInspector();
    } catch (err) {
      document.getElementById('insp-body').innerHTML = `<span class="hint">node unavailable: ${esc(err.message)}</span>`;
    }
  }

  function paintInspector() {
    const d = inspDump;
    if (!d) return;
    const r = d.result || {};
    const st = statusWord(d.status);
    const cls = statusClass(d.status);
    document.getElementById('insp-name').textContent =
      (String(d.skill || inspNid).toUpperCase() + 'AGENT').slice(0, 30);
    document.getElementById('insp-id').textContent = d.node_id || inspNid;
    document.getElementById('insp-status').innerHTML =
      `<span class="pill ${cls === 'st-ok' ? 'ok' : (cls === 'st-err' ? 'err' : 'cap')}">${esc(st)}</span>`;
    document.querySelectorAll('#insp-tabs button').forEach((b) =>
      b.classList.toggle('on', b.dataset.tab === inspTab));
    const body = document.getElementById('insp-body');
    const cost = (typeof r.cost !== 'undefined' && r.cost !== null) ? String(r.cost) : '—';
    if (inspTab === 'output') {
      const o = r.output;
      const txt = (typeof o === 'string') ? o : JSON.stringify(o, null, 1);
      body.innerHTML = '<pre></pre>';
      body.querySelector('pre').textContent = String(txt == null ? '(no output)' : txt).slice(0, 4000);
    } else if (inspTab === 'stats') {
      body.innerHTML =
        kv('provider', r.provider || '—') +
        kv('duration', fmtDur(d)) +
        kv('cost', cost, cost === '—' ? '' : 'g') +
        kv('retries', String(d.retries != null ? d.retries : 0)) +
        kv('started', d.started_at ? new Date(d.started_at * 1000).toLocaleTimeString() : '—') +
        kv('completed', d.completed_at ? new Date(d.completed_at * 1000).toLocaleTimeString() : '—');
    } else {
      const inputs = Array.isArray(d.inputs) ? d.inputs : [];
      const succ = Array.isArray(r.successors) ? r.successors : [];
      body.innerHTML =
        kv('skill', d.skill || '—') +
        kv('status', st, cls === 'st-ok' ? 'g' : (cls === 'st-err' ? 'r' : '')) +
        kv('type', r.agent_name || d.skill || '—') +
        `<div class="k">INPUTS (READS)</div><div class="chips">${inputs.map((x) => `<span class="chip">${esc(x)}</span>`).join('') || '<span class="hint">—</span>'}</div>` +
        `<div class="k">OUTPUTS (WRITES)</div><div class="chips">${succ.map((x) => `<span class="chip">${esc(x)}</span>`).join('') || '<span class="hint">—</span>'}</div>` +
        kv('duration', fmtDur(d)) +
        kv('cost', cost, cost === '—' ? '' : 'g');
    }
  }

  document.getElementById('insp-tabs').addEventListener('click', (e) => {
    const b = e.target.closest ? e.target.closest('button[data-tab]') : null;
    if (!b) return;
    inspTab = b.dataset.tab;
    paintInspector();
  });

  async function loadGraph(sid, light) {
    try {
      const d = await fetch(`/api/sessions/${encodeURIComponent(sid)}/graph${light ? '?light=true' : ''}`).then((r) => r.json());
      if (sid !== currentSid) return d;
      const nodes = d.nodes || [];
      const sig = nodes.length + ':' + nodes.map((n) => n.id + (n.status || '')).join(',');
      if (sig === lastGraphSig) return d; // unchanged — skip repaint for smoothness
      lastGraphSig = sig;
      if (rafQueued) { lastGraphPending = d; return d; }
      rafQueued = true;
      requestAnimationFrame(() => { rafQueued = false; renderDAG(lastGraphPending || d); lastGraphPending = null; });
      return d;
    } catch (e) { return null; }
  }

  function rpOrder() {
    if (!lastGraph) return [];
    const {cols} = layout(lastGraph.nodes || [], lastGraph.edges || []);
    return Object.keys(cols).map(Number).sort((a, b) => a - b)
      .flatMap((d) => cols[d].map((n) => n.id));
  }

  function rpEnter() {
    if (!lastGraph || !(lastGraph.nodes || []).length) return;
    replayOrder = rpOrder();
    if (!replayOrder.length) return;
    replayOn = true;
    replayIdx = -1;
    document.getElementById('replay').hidden = false;
    rpStep(1);
  }

  function paintReplay() {
    document.querySelectorAll('#dag .node').forEach((el) => {
      const i = replayOrder.indexOf(el.getAttribute('data-nid'));
      el.classList.toggle('dim', !(i >= 0 && i <= replayIdx));
      el.classList.toggle('cur', i === replayIdx);
    });
    document.getElementById('rp-label').textContent = `${Math.max(0, replayIdx + 1)} / ${replayOrder.length}`;
    document.getElementById('rp-fill').style.width =
      (replayOrder.length ? ((replayIdx + 1) / replayOrder.length * 100) : 0) + '%';
    document.getElementById('rp-play').textContent = replayTimer ? '⏸' : '▶';
  }

  function rpStep(d) {
    if (!replayOn) return;
    replayIdx = Math.min(replayOrder.length - 1, Math.max(0, replayIdx + d));
    paintReplay();
    showNode(replayOrder[replayIdx]);
  }

  function rpToggle() {
    if (!replayOn) return;
    if (replayTimer) {
      clearInterval(replayTimer);
      replayTimer = null;
    } else {
      replayTimer = setInterval(() => {
        if (replayIdx >= replayOrder.length - 1) {
          clearInterval(replayTimer);
          replayTimer = null;
          paintReplay();
        } else {
          rpStep(1);
        }
      }, replaySpeedMs);
    }
    paintReplay();
  }

  function rpSpeed() {
    replaySpeedMs = replaySpeedMs === 1000 ? 500 : 1000;
    document.getElementById('rp-speed').textContent = replaySpeedMs === 1000 ? '1s' : '0.5s';
    if (replayTimer) { rpToggle(); rpToggle(); }
  }

  function rpExit() {
    replayOn = false;
    replayIdx = -1;
    if (replayTimer) { clearInterval(replayTimer); replayTimer = null; }
    document.getElementById('replay').hidden = true;
    document.querySelectorAll('#dag .node').forEach((el) => el.classList.remove('dim', 'cur'));
  }

  function toggleDag() {
    const hide = dagSvg.style.display !== 'none';
    dagSvg.style.display = hide ? 'none' : '';
    document.getElementById('insp').hidden = true;
    const btn = document.querySelector('#dag-panel .dag-head button:last-child');
    if (btn) btn.textContent = hide ? 'show' : 'hide';
  }

  document.getElementById('run-search').addEventListener('input', refreshRuns);
  document.getElementById('run-filter').addEventListener('change', refreshRuns);

  window.refreshRuns = refreshRuns;
  window.selectRun = selectRun;
  window.clearCompare = clearCompare;
  window.toggleDag = toggleDag;
  window.rpEnter = rpEnter;
  window.rpStep = rpStep;
  window.rpToggle = rpToggle;
  window.rpSpeed = rpSpeed;
  window.rpExit = rpExit;
  health();
  refreshRuns().then(() => {
    const h = (location.hash || '').slice(1);
    if (h && runs.some((s) => s.session_id === h)) selectRun(h);
  });
  setInterval(health, 15000);
  setInterval(refreshRuns, 30000);
})();
