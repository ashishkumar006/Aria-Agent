/* Aria Research — ask a topic, watch the DAG pipeline work it live,
   inspect any node (query / goal / inputs / outputs / cost), get the
   final report as a rich preview. Topics pane lists past briefs. */
(function () {
  'use strict';

  const dagSvg = document.getElementById('dag');

  let depth = 'standard';
  let sid = '', topic = '', running = false, t0 = 0;
  let finalAnswer = '', pollTimer = null, pollInflight = false, lastSig = '';
  let topics = [];
  let lastGraph = null, inspTab = 'overview', inspNid = null, inspDump = null;
  let replayOn = false, replayIdx = -1, replayOrder = [], replayTimer = null, replaySpeedMs = 1000;
  let rafQueued = false;

  const $ = (id) => document.getElementById(id);

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;',
    }[c]));
  }

  function md(src) {
    const lines = String(src || '').split('\n');
    let html = '', inList = false;
    for (const ln of lines) {
      const t = ln.trimEnd();
      if (/^#{1,3}\s/.test(t)) {
        if (inList) { html += '</ul>'; inList = false; }
        const lvl = t.match(/^#+/)[0].length;
        html += `<h${lvl}>${esc(t.replace(/^#+\s*/, ''))}</h${lvl}>`;
      } else if (/^[-*]\s+/.test(t.trim())) {
        if (!inList) { html += '<ul>'; inList = true; }
        html += `<li>${inline(esc(t.trim().replace(/^[-*]\s+/, '')))}</li>`;
      } else if (!t.trim()) {
        if (inList) { html += '</ul>'; inList = false; }
      } else {
        if (inList) { html += '</ul>'; inList = false; }
        html += `<p>${inline(esc(t.trim()))}</p>`;
      }
    }
    if (inList) html += '</ul>';
    return html;
  }
  function inline(h) {
    return h.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/`(.+?)`/g, '<code>$1</code>');
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
      $('health-dot').className = 'dot ' + (up ? 'g' : 'r');
      $('health-text').textContent = up ? 'ready' : 'agent only';
    } catch (e) {
      $('health-dot').className = 'dot r';
      $('health-text').textContent = 'unreachable';
    }
  }

  /* ---------- topics pane ---------- */
  async function refreshTopics() {
    const box = $('topics');
    const q = ($('topic-search').value || '').toLowerCase();
    if (!box.dataset.loaded) {
      box.innerHTML = '<div class="skel"><div class="bar w85"></div><div class="bar w60"></div></div>'.repeat(3);
    }
    try {
      const d = await fetch('/api/sessions?limit=50').then((r) => r.json());
      topics = (d.sessions || []).filter((s) =>
        /research agent/i.test(s.query || '') || (s.skills || []).length > 1);
      box.dataset.loaded = '1';
      $('topic-count').textContent = topics.length;
      const vis = topics.filter((s) => {
        const t = displayTopic(s).toLowerCase();
        return !q || t.includes(q);
      });
      if (!vis.length) {
        box.innerHTML = '<div class="empty"><span class="e-ico">✦</span>' +
          '<div class="e-t">No research yet</div><div class="e-s">Your briefs land here.</div></div>';
        return;
      }
      box.innerHTML = '';
      vis.forEach((s) => {
        const b = document.createElement('button');
        b.className = 'icard' + (s.session_id === sid ? ' on' : '');
        b.innerHTML = '<div class="i-top"><span class="i-t"></span></div>' +
          '<div class="i-sub"><span></span></div>';
        b.querySelector('.i-t').textContent = displayTopic(s).slice(0, 90);
        b.querySelector('.i-sub span').textContent =
          `${s.nodes || 0} nodes · ${ago(Date.now() / 1000 - (s.updated_ago || 0))}`;
        b.onclick = () => openTopic(s.session_id);
        box.appendChild(b);
      });
    } catch (e) {
      box.innerHTML = '<div class="empty"><span class="e-ico">⚠</span><div class="e-t">Unavailable</div></div>';
    }
  }

  function displayTopic(s) {
    const m = String(s.query || '').match(/Topic:\s*([\s\S]{1,140})/i);
    return (m ? m[1].split('\n')[0] : (s.query || '(untitled)')).trim();
  }

  async function openTopic(id) {
    if (running) return;
    stopPoll();
    sid = id;
    const s = topics.find((t) => t.session_id === id);
    topic = s ? displayTopic(s) : '';
    finalAnswer = '';
    $('p-sid').textContent = sid.slice(0, 12) + '…';
    $('r-topic').value = topic;
    $('brief-card').style.display = '';
    setPipe('snapshot', 'muted');
    const g = await loadGraph(id, false);
    if (g) {
      const nodes = g.nodes || [];
      const fin = nodes.filter((n) => /format|distil|deliver|final|answer/i.test(String(n.skill || n.id || '')));
      const pick = fin.length ? fin[fin.length - 1].id : (nodes.length ? nodes[nodes.length - 1].id : null);
      if (pick) showNode(pick);
      // try to surface the saved final answer
      try {
        const d = await fetch(`/api/sessions/${encodeURIComponent(sid)}/nodes/${encodeURIComponent(pick)}`).then((r) => r.json());
        const out = d && d.result ? d.result.output : null;
        if (typeof out === 'string' && out.length > 40) {
          finalAnswer = out;
          $('dl-btn').disabled = false;
          $('copy-btn').disabled = false;
        }
      } catch (e) { /* read-only nicety */ }
    }
    refreshTopics();
  }

  /* ---------- DAG renderer (ports, orthogonal flow edges) ---------- */
  const NW = 200, NH = 52, GX = 70, GY = 16;

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
    return { cols };
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
    $('dag-empty').hidden = nodes.length > 0;
    $('dag-scroll').hidden = nodes.length === 0;
    $('livebar').hidden = false;
    if (!nodes.length) return;
    const { cols } = layout(nodes, edges);
    const depths = Object.keys(cols).map(Number).sort((a, b) => a - b);
    const pos = {};
    depths.forEach((d, ci) => {
      cols[d].forEach((n, ri) => { pos[n.id] = { x: 16 + ci * (NW + GX), y: 16 + ri * (NH + GY) }; });
    });
    const W = 32 + depths.length * (NW + GX);
    const H = 32 + Math.max(...depths.map((d) => cols[d].length)) * (NH + GY);
    dagSvg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    dagSvg.setAttribute('width', W);
    dagSvg.style.height = Math.max(H, 420) + 'px';
    let s = '<defs><marker id="rah" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto">' +
      '<path d="M0,0 L7,3.5 L0,7" fill="none" stroke="#4a4a56" stroke-width="1.2"/></marker></defs>';
    edges.forEach((e) => {
      const a = pos[e.from], b = pos[e.to];
      if (!a || !b) return;
      const x1 = a.x + NW, y1 = a.y + NH / 2, x2 = b.x, y2 = b.y + NH / 2;
      const mx = Math.round((x1 + x2) / 2);
      const live = running ? ' flow' : '';
      s += `<path class="edge${live}" d="M${x1} ${y1} H${mx} V${y2} H${x2}" marker-end="url(#rah)"/>`;
    });
    nodes.forEach((n) => {
      const p = pos[n.id];
      const sk = String(n.skill || n.id);
      const cap = esc(sk.replace(/agent$/i, '').toUpperCase().slice(0, 20) || 'STEP');
      const sub = esc((sk.match(/agent$/i) ? sk : sk + 'Agent').slice(0, 26));
      const cls = statusClass(n.status);
      const sel = n.id === inspNid ? ' sel' : '';
      s += `<g class="node ${cls}${sel}" data-nid="${esc(n.id)}">` +
        `<rect class="body" x="${p.x}" y="${p.y}" width="${NW}" height="${NH}"></rect>` +
        `<circle class="port" cx="${p.x}" cy="${p.y + NH / 2}" r="3.4"/>` +
        `<circle class="port" cx="${p.x + NW}" cy="${p.y + NH / 2}" r="3.4"/>` +
        `<rect class="ibox" x="${p.x + 11}" y="${p.y + 13}" width="26" height="26" rx="6"></rect>` +
        `<text class="glyph" x="${p.x + 24}" y="${p.y + 30}" text-anchor="middle" font-size="13">⬢</text>` +
        `<text class="t-cap" x="${p.x + 44}" y="${p.y + 21}">${cap}</text>` +
        `<text class="t-sub ${cls}" x="${p.x + 44}" y="${p.y + 37}">${sub}</text></g>`;
    });
    dagSvg.innerHTML = s;
    $('rp-label').textContent = `${nodes.length} nodes`;
    if (replayOn) paintReplay();
  }

  // rAF-throttled paint: polls stay cheap, frames stay smooth
  function queuePaint(data) {
    lastGraph = data;
    if (rafQueued) return;
    rafQueued = true;
    requestAnimationFrame(() => { rafQueued = false; renderDAG(data); });
  }

  dagSvg.addEventListener('click', (e) => {
    const g = e.target.closest ? e.target.closest('.node') : null;
    if (!g) return;
    showNode(g.getAttribute('data-nid'));
  });

  async function loadGraph(id, light) {
    try {
      const d = await fetch(`/api/sessions/${encodeURIComponent(id)}/graph${light ? '?light=true' : ''}`).then((r) => r.json());
      if (id === sid) {
        const nodes = d.nodes || [];
        const sig = nodes.length + ':' + nodes.map((n) => n.id + (n.status || '')).join(',');
        if (sig === lastSig && !light) { renderDAG(d); return d; }
        lastSig = sig;
        queuePaint(d);
      }
      return d;
    } catch (e) { return null; }
  }

  function startPoll() {
    stopPoll();
    const tick = async () => {
      if (!running || !sid) return;
      if (!pollInflight) {
        pollInflight = true;
        try {
          await loadGraph(sid, true);
          $('p-time').textContent = ((Date.now() - t0) / 1000).toFixed(0) + 's';
        } finally { pollInflight = false; }
      }
      pollTimer = setTimeout(tick, 2000);
    };
    pollTimer = setTimeout(tick, 800);
  }

  function stopPoll() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    pollInflight = false;
  }

  /* ---------- inspector ---------- */
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
    if (!nid || !sid) return;
    inspNid = nid;
    document.querySelectorAll('#dag .node').forEach((el) =>
      el.classList.toggle('sel', el.getAttribute('data-nid') === nid));
    $('rinspector').hidden = false;
    $('ri-name').textContent = 'LOADING…';
    $('ri-id').textContent = nid;
    $('ri-body').innerHTML = '<span class="hint">loading node…</span>';
    $('vote-msg').textContent = '';
    $('vote-up').classList.remove('picked');
    $('vote-down').classList.remove('picked');
    try {
      const d = await fetch(`/api/sessions/${encodeURIComponent(sid)}/nodes/${encodeURIComponent(nid)}`).then((r) => r.json());
      if (d.error) throw new Error(d.error);
      inspDump = d;
      try {
        const f = await fetch(`/api/feedback?node_id=${encodeURIComponent(nid)}`).then((r) => r.json());
        if (f.vote === 1) $('vote-up').classList.add('picked');
        if (f.vote === -1) $('vote-down').classList.add('picked');
      } catch (e) { /* no vote yet */ }
      paintInspector();
    } catch (err) {
      $('ri-body').innerHTML = `<span class="hint">node unavailable: ${esc(err.message)}</span>`;
    }
  }

  function paintInspector() {
    const d = inspDump;
    if (!d) return;
    const r = d.result || {};
    const st = statusWord(d.status);
    const skill = String(d.skill || inspNid || '');
    $('ri-name').textContent = (skill.replace(/agent$/i, '') + 'Agent').toUpperCase().slice(0, 30);
    $('ri-id').textContent = (d.node_id || inspNid || '') + ' · ' + st.toUpperCase();
    document.querySelectorAll('#ri-tabs button').forEach((b) =>
      b.classList.toggle('on', b.dataset.tab === inspTab));
    const body = $('ri-body');
    const cost = (typeof r.cost !== 'undefined' && r.cost !== null) ? String(r.cost) : '—';
    const goal = r.goal || r.task || r.instruction || d.prompt || '';
    if (inspTab === 'output') {
      const o = r.output;
      const txt = (typeof o === 'string') ? o : JSON.stringify(o, null, 1);
      body.innerHTML = '<div class="k">EXECUTION OUTPUT</div><pre></pre>';
      body.querySelector('pre').textContent = String(txt == null ? '(no output)' : txt).slice(0, 6000);
    } else if (inspTab === 'preview') {
      const o = typeof r.output === 'string' ? r.output : (finalAnswer || '');
      body.innerHTML = '<div class="k">PREVIEW</div><div class="qbox">' +
        (o ? md(String(o).slice(0, 12000)) : '<span class="hint">nothing rendered yet</span>') + '</div>';
    } else if (inspTab === 'stats') {
      body.innerHTML =
        kv('model', (r.executed_model || r.model || r.provider || '—')) +
        kv('duration', fmtDur(d)) +
        kv('cost', cost, cost === '—' ? '' : 'g') +
        kv('input tokens', String(r.input_tokens != null ? r.input_tokens : '—')) +
        kv('output tokens', String(r.output_tokens != null ? r.output_tokens : '—')) +
        kv('retries', String(d.retries != null ? d.retries : 0));
    } else {
      const inputs = Array.isArray(d.inputs) ? d.inputs : [];
      const succ = Array.isArray(r.successors) ? r.successors : [];
      const o = r.output;
      const txt = (typeof o === 'string') ? o : JSON.stringify(o, null, 1);
      body.innerHTML =
        `<div class="k">USER QUERY</div><div class="qbox">${esc(topic || '—')}</div>` +
        (goal ? `<div class="k">AGENT GOAL</div><div class="qbox">${esc(String(goal).slice(0, 900))}</div>` : '') +
        `<div class="k">INPUTS (READS)</div><div class="chips">${inputs.map((x) => `<span class="chip">${esc(x)}</span>`).join('') || '<span class="hint">—</span>'}</div>` +
        `<div class="k">OUTPUTS (WRITES)</div><div class="chips">${succ.map((x) => `<span class="chip w">${esc(x)}</span>`).join('') || '<span class="hint">—</span>'}</div>` +
        `<div class="ri-meta"><span class="pill info">◷ ${esc(fmtDur(d))}</span>` +
        `<span class="pill ${cost === '—' ? 'muted' : 'ok'}">$ ${esc(cost)}</span></div>` +
        `<div class="k">EXECUTION OUTPUT</div><pre></pre>`;
      body.querySelector('pre').textContent = String(txt == null ? '(no output yet)' : txt).slice(0, 2500);
    }
  }

  document.querySelectorAll('#ri-tabs button').forEach((b) => {
    b.addEventListener('click', () => { inspTab = b.dataset.tab; paintInspector(); });
  });

  async function vote(v) {
    if (!inspNid) return;
    try {
      const r = await fetch('/api/feedback', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sid, node_id: inspNid, vote: v }),
      }).then((x) => x.json());
      if (r.status === 'ok') {
        $('vote-up').classList.toggle('picked', v === 1);
        $('vote-down').classList.toggle('picked', v === -1);
        $('vote-msg').textContent = 'recorded';
      } else {
        $('vote-msg').textContent = r.error || 'failed';
      }
    } catch (e) { $('vote-msg').textContent = e.message; }
  }

  /* ---------- replay ---------- */
  function rpOrder() {
    if (!lastGraph) return [];
    const { cols } = layout(lastGraph.nodes || [], lastGraph.edges || []);
    return Object.keys(cols).map(Number).sort((a, b) => a - b)
      .flatMap((d) => cols[d].map((n) => n.id));
  }

  function paintReplay() {
    document.querySelectorAll('#dag .node').forEach((el) => {
      const i = replayOrder.indexOf(el.getAttribute('data-nid'));
      el.classList.toggle('dim', !(i >= 0 && i <= replayIdx));
      el.classList.toggle('cur', i === replayIdx);
    });
    $('rp-label').textContent = `${Math.max(0, replayIdx + 1)} / ${replayOrder.length}`;
    $('rp-fill').style.width =
      (replayOrder.length ? ((replayIdx + 1) / replayOrder.length * 100) : 0) + '%';
    $('rp-play').textContent = replayTimer ? '⏸' : '▶';
  }

  function rpEnterReplay() {
    if (!lastGraph || !(lastGraph.nodes || []).length) return;
    replayOrder = rpOrder();
    if (!replayOrder.length) return;
    replayOn = true;
    replayIdx = -1;
    $('rp-mode').textContent = 'REPLAY MODE';
    rpStep(1);
  }

  function rpStep(d) {
    if (!replayOn) return;
    replayIdx = Math.min(replayOrder.length - 1, Math.max(0, replayIdx + d));
    paintReplay();
    showNode(replayOrder[replayIdx]);
  }

  function rpToggle() {
    if (!replayOn) { rpEnterReplay(); return; }
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
    $('rp-speed').textContent = replaySpeedMs === 1000 ? '1s' : '0.5s';
    if (replayTimer) { rpToggle(); rpToggle(); }
  }

  function rpExit() {
    replayOn = false;
    replayIdx = -1;
    if (replayTimer) { clearInterval(replayTimer); replayTimer = null; }
    $('rp-mode').textContent = running ? 'LIVE VIEW' : 'DONE';
    document.querySelectorAll('#dag .node').forEach((el) => el.classList.remove('dim', 'cur'));
  }

  /* ---------- run flow ---------- */
  function setPipe(txt, cls) {
    $('pipe-state').textContent = txt;
    $('pipe-state').className = 'pill ' + cls;
  }

  function briefFor(t, d) {
    const shape = d === 'quick'
      ? 'Keep it tight: a short summary with the key points and main sources. One pass only.'
      : d === 'deep'
        ? 'Be thorough: Executive Summary, Key Findings (with evidence), Competing Views, Risks & Unknowns, and a Sources section. Retrieve broadly before concluding.'
        : 'A structured report: Executive Summary, Key Findings, and Sources. Retrieve before concluding.';
    return `You are a research agent. Research this topic and deliver a final report as Markdown.\n\nTopic: ${t}\n\n${shape}\n\nUse web search, memory and any files available. The final message must be the complete report.`;
  }

  async function startResearch() {
    if (running) return;
    topic = $('r-topic').value.trim();
    if (!topic) { $('r-out').textContent = 'type a topic first.'; return; }
    running = true;
    finalAnswer = '';
    sid = '';
    inspNid = null;
    lastSig = '';
    t0 = Date.now();
    rpExit();
    $('go-btn').disabled = true;
    $('dl-btn').disabled = true;
    $('copy-btn').disabled = true;
    $('r-out').textContent = 'researching…';
    $('rinspector').hidden = true;
    $('rp-mode').textContent = 'LIVE VIEW';
    setPipe('running', 'info');
    try {
      const r = await fetch('/api/chat', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: briefFor(topic, depth) }),
      });
      if (!r.ok || !r.body) throw new Error('HTTP ' + r.status);
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const chunk = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          if (!chunk.startsWith('data: ')) continue;
          let ev;
          try { ev = JSON.parse(chunk.slice(6)); } catch (e) { continue; }
          if (ev.type === 'started') {
            sid = ev.session_id || '';
            $('p-sid').textContent = sid ? sid.slice(0, 12) + '…' : '—';
            startPoll();
          } else if (ev.type === 'done') {
            stopPoll();
            if (sid) await loadGraph(sid, false);
            finalAnswer = ev.answer || '(empty answer)';
            inspTab = 'preview';
            setPipe('done', 'ok');
            $('r-out').textContent = 'done in ' + ((Date.now() - t0) / 1000).toFixed(0) + 's.';
            $('dl-btn').disabled = false;
            $('copy-btn').disabled = false;
            // surface the final node in the inspector with the rich preview
            if (lastGraph && (lastGraph.nodes || []).length) {
              const nodes = lastGraph.nodes;
              const fin = nodes.filter((n) => /format|distil|deliver|final|answer/i.test(String(n.skill || n.id || '')));
              showNode((fin.length ? fin[fin.length - 1] : nodes[nodes.length - 1]).id);
            }
            rpExit();
            refreshTopics();
            running = false;
            $('go-btn').disabled = false;
          } else if (ev.type === 'error') {
            stopPoll();
            setPipe('error', 'err');
            $('r-out').textContent = ev.text || 'error';
            rpExit();
            running = false;
            $('go-btn').disabled = false;
          }
        }
      }
    } catch (e) {
      stopPoll();
      setPipe('error', 'err');
      $('r-out').textContent = e.message;
      running = false;
      $('go-btn').disabled = false;
    }
  }

  function rerun() {
    if (!topic && $('r-topic').value.trim()) topic = $('r-topic').value.trim();
    if (!topic || running) return;
    $('r-topic').value = topic;
    startResearch();
  }

  function downloadDoc() {
    if (!finalAnswer) return;
    const blob = new Blob(['# ' + topic + '\n\n' + finalAnswer], { type: 'text/markdown' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'research-' + Date.now() + '.md';
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }

  function copyDoc() {
    if (!finalAnswer) return;
    navigator.clipboard.writeText('# ' + topic + '\n\n' + finalAnswer).then(
      () => { $('r-out').textContent = 'copied to clipboard.'; },
      () => { $('r-out').textContent = 'copy failed — select the report manually.'; });
  }

  document.querySelectorAll('#depth-seg button').forEach((b) => {
    b.addEventListener('click', () => {
      document.querySelectorAll('#depth-seg button').forEach((x) => x.classList.remove('on'));
      b.classList.add('on');
      depth = b.dataset.d;
    });
  });
  $('r-topic').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) startResearch();
  });
  $('topic-search').addEventListener('input', refreshTopics);

  window.startResearch = startResearch;
  window.downloadDoc = downloadDoc;
  window.copyDoc = copyDoc;
  window.rerun = rerun;
  window.vote = vote;
  window.rpStep = rpStep;
  window.rpToggle = rpToggle;
  window.rpSpeed = rpSpeed;
  health();
  refreshTopics();
  setInterval(health, 15000);
})();
