/* Aria Scheduler — cron board over /api/schedule. */
(function () {
  'use strict';

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function fmtFire(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    const now = Date.now();
    const diff = ts * 1000 - now;
    const when = d.toLocaleString();
    if (diff <= 0) return when + ' (due)';
    const m = Math.round(diff / 60000);
    const rel = m < 60 ? `in ${m}m` : (m < 1440 ? `in ${Math.round(m / 60)}h` : `in ${Math.round(m / 1440)}d`);
    return `${when} (${rel})`;
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

  async function createJob() {
    const out = document.getElementById('job-out');
    const query = document.getElementById('j-query').value.trim();
    const when = document.getElementById('j-when').value.trim();
    if (!query || !when) { out.textContent = 'query and when are required.'; return; }
    out.textContent = 'scheduling…';
    try {
      const d = await fetch('/api/schedule', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({query, when}),
      }).then((r) => r.json());
      if (d.status === 'ok') {
        out.textContent = 'scheduled ' + d.id;
        document.getElementById('j-query').value = '';
        document.getElementById('j-when').value = '';
        loadJobs();
      } else {
        out.textContent = (d.message || JSON.stringify(d).slice(0, 160));
      }
    } catch (e) { out.textContent = e.message; }
  }

  async function cancelJob(sid) {
    const out = document.getElementById('job-out');
    try {
      const d = await fetch('/api/schedule/' + encodeURIComponent(sid), {method: 'DELETE'}).then((r) => r.json());
      out.textContent = d.status === 'ok' ? `cancelled ${sid}` : JSON.stringify(d).slice(0, 160);
      loadJobs();
    } catch (e) { out.textContent = '❌ ' + e.message; }
  }

  async function deleteJob(sid) {
    const out = document.getElementById('job-out');
    try {
      const d = await fetch('/api/schedule/' + encodeURIComponent(sid) + '?hard=true', {method: 'DELETE'}).then((r) => r.json());
      out.textContent = d.status === 'ok' ? `deleted ${sid}` : JSON.stringify(d).slice(0, 160);
      loadJobs();
    } catch (e) { out.textContent = '❌ ' + e.message; }
  }

  async function loadJobs() {
    const box = document.getElementById('jobs');
    const tb = document.querySelector('#job-table tbody');
    if (!box.dataset.loaded) {
      box.innerHTML = '<div class="skel"><div class="bar w85"></div><div class="bar w60"></div></div>'.repeat(3);
    }
    try {
      const d = await fetch('/api/schedule').then((r) => r.json());
      const jobs = d.schedules || [];
      box.dataset.loaded = '1';
      document.getElementById('job-count').textContent = jobs.length;
      const nActive = jobs.filter((j) => j.enabled).length;
      document.getElementById('job-tot').textContent = `· ${jobs.length} total · ${nActive} active`;
      document.getElementById('st-jobs').textContent = jobs.length;
      const stA = document.getElementById('st-active');
      stA.textContent = nActive;
      stA.className = 's-v' + (nActive ? ' ok' : '');
      const active = jobs.filter((j) => j.enabled).sort((a, b) => (a.next_fire || 0) - (b.next_fire || 0));
      document.getElementById('p-queue').textContent = active.length
        ? `${active.length} active · next ${fmtFire(active[0].next_fire)}` : 'empty';
      document.getElementById('st-next').textContent = active.length ? fmtFire(active[0].next_fire).split(' (')[0] : '—';
      document.getElementById('st-next-s').textContent = active.length ? (active[0].query || '').slice(0, 40) : 'no active jobs';
      box.innerHTML = '';
      if (!jobs.length) {
        box.innerHTML = '<div class="empty"><span class="e-ico">◷</span>' +
          '<div class="e-t">No jobs</div><div class="e-s">Schedule a reminder or a recurring agent run above.</div></div>';
      }
      jobs.forEach((j) => {
        const b = document.createElement('div');
        b.className = 'icard';
        b.innerHTML = `<div class="i-top"><span class="i-t"></span>` +
          `<span class="pill ${j.enabled ? 'ok' : 'muted'}">${j.enabled ? 'scheduled' : 'off'}</span></div>` +
          `<div class="i-sub"><span class="cron">${esc(j.when || '?')}</span><span>${esc(fmtFire(j.next_fire))}</span></div>`;
        b.querySelector('.i-t').textContent = (j.query || '(no query)').slice(0, 90);
        box.appendChild(b);
      });
      tb.innerHTML = jobs.map((j) =>
        `<tr><td>${esc((j.query || '').slice(0, 80))}</td>` +
        `<td><span class="cron">${esc(j.when || '')}</span></td>` +
        `<td class="num" style="text-align:left;">${esc(fmtFire(j.next_fire))}</td>` +
        `<td><span class="pill ${j.enabled ? 'ok' : 'muted'}">${j.enabled ? 'active' : 'off'}</span></td>` +
        `<td class="num">${j.enabled ? `<button class="icon-btn danger" onclick="cancelJob('${esc(j.id)}')" title="Cancel (pause)">✕</button>` : ''}` +
        `<button class="icon-btn danger" onclick="deleteJob('${esc(j.id)}')" title="Delete permanently">🗑</button></td></tr>`
      ).join('') || '<tr><td colspan="5"><div class="empty"><span class="e-ico">◷</span><div class="e-t">No jobs</div></div></td></tr>';
    } catch (e) {
      box.innerHTML = '<div class="empty"><span class="e-ico">⚠</span><div class="e-t">Scheduler unavailable</div></div>';
    }
  }

  document.getElementById('j-query').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') createJob();
  });

  window.loadJobs = loadJobs;
  window.createJob = createJob;
  window.cancelJob = cancelJob;
  window.deleteJob = deleteJob;
  health();
  loadJobs();
  setInterval(health, 15000);
  setInterval(loadJobs, 30000);
})();
