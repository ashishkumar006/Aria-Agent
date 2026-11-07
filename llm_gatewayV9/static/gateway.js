/* Gateway console interactions — command palette + toasts.
   No build step: vanilla JS, auto-discovers sidebar links and buttons. */
(function () {
  'use strict';

  /* ---------- toasts ---------- */
  let toastBox = null;
  function toast(msg, kind) {
    if (!toastBox) {
      toastBox = document.createElement('div');
      toastBox.id = 'toast-box';
      document.body.appendChild(toastBox);
    }
    const el = document.createElement('div');
    el.className = 'toast ' + (kind || 'info');
    el.textContent = msg;
    toastBox.appendChild(el);
    setTimeout(() => { el.classList.add('out'); }, 3200);
    setTimeout(() => { el.remove(); }, 3600);
  }
  window.toast = toast;

  /* ---------- token rate ----------
     Tokens/second for one call, formatted in one place so the two calls
     tables cannot drift apart on what "slow" means. `output_tps` counts
     output PLUS thinking tokens over latency: a one-token answer preceded by
     180 thinking tokens is a fast call, and dividing by `output_tokens` alone
     is what made those rows read as 0.01 tok/s with nothing to explain them.
     A missing rate renders as a bare dash, the way a missing override already
     does — not as "0.0", which would claim the model generated nothing rather
     than that the column did not exist when the row was written. */
  const TPS_WARN = 8, TPS_SLOW = 3;
  function tpsCell(c) {
    const r = Number(c && c.output_tps);
    if (!isFinite(r) || r <= 0) return '—';
    const cls = r < TPS_SLOW ? 'err' : (r < TPS_WARN ? 'warn' : 'ok');
    const th = Number(c.thinking_tokens) || 0;
    return `<span class="${cls}">${r.toFixed(1)}</span>` +
      (th ? ` <span class="badge" title="thinking tokens, included in this rate">+${th}thk</span>` : '');
  }
  function tpsText(c) {
    const r = Number(c && c.output_tps);
    const th = Number(c.thinking_tokens) || 0;
    return `${isFinite(r) && r > 0 ? r.toFixed(1) + ' tk/s' : 'tk/s —'}` +
      (th ? ` thinking=${th}` : '');
  }
  window.tpsCell = tpsCell;
  window.tpsText = tpsText;

  /* ---------- command palette ---------- */
  let overlay = null, input = null, list = null, items = [], sel = 0;

  function collect() {
    items = [];
    document.querySelectorAll('a.side-item').forEach((a) => {
      const label = (a.textContent || '').trim().replace(/\s+/g, ' ');
      if (label) items.push({ label: 'Go to ' + label, hint: 'page', run: () => { location.href = a.href; } });
    });
    document.querySelectorAll('main button').forEach((b) => {
      const t = (b.textContent || '').trim().replace(/\s+/g, ' ');
      if (!t) return;
      const card = b.closest('section.card');
      const h = card ? card.querySelector('.card-head h2') : null;
      items.push({
        label: (h ? h.textContent.trim() + ' › ' : '') + t,
        hint: 'run',
        run: () => { close(); b.click(); },
      });
    });
  }

  function build() {
    overlay = document.createElement('div');
    overlay.className = 'palette-overlay';
    overlay.hidden = true;
    overlay.innerHTML =
      '<div class="palette" role="dialog" aria-label="command palette">' +
      '<input id="palette-input" type="text" placeholder="Type a command or page…" autocomplete="off" spellcheck="false" />' +
      '<div id="palette-list"></div>' +
      '<div class="palette-foot"><span><b>↑↓</b> move</span><span><b>↵</b> run</span><span><b>esc</b> close</span></div>' +
      '</div>';
    document.body.appendChild(overlay);
    input = overlay.querySelector('#palette-input');
    list = overlay.querySelector('#palette-list');
    overlay.addEventListener('mousedown', (e) => { if (e.target === overlay) close(); });
    input.addEventListener('input', () => render(input.value));
    input.addEventListener('keydown', (e) => {
      const vis = visibleItems();
      if (e.key === 'ArrowDown') { e.preventDefault(); sel = Math.min(sel + 1, vis.length - 1); paint(vis); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); sel = Math.max(sel - 1, 0); paint(vis); }
      else if (e.key === 'Enter') { e.preventDefault(); if (vis[sel]) vis[sel].run(); }
      else if (e.key === 'Escape') { close(); }
    });
  }

  function visibleItems() {
    const q = (input.value || '').toLowerCase();
    if (!q) return items.slice(0, 12);
    return items.filter((it) => it.label.toLowerCase().includes(q)).slice(0, 12);
  }

  function paint(vis) {
    list.innerHTML = '';
    if (!vis.length) {
      list.innerHTML = '<div class="palette-empty">No matching command</div>';
      return;
    }
    vis.forEach((it, i) => {
      const d = document.createElement('div');
      d.className = 'palette-item' + (i === sel ? ' on' : '');
      d.innerHTML = '<span></span><span class="palette-hint"></span>';
      d.firstChild.textContent = it.label;
      d.lastChild.textContent = it.hint;
      d.addEventListener('mousedown', (e) => { e.preventDefault(); it.run(); });
      d.addEventListener('mousemove', () => { sel = i; paint(vis); });
      list.appendChild(d);
    });
  }

  function render() { sel = 0; paint(visibleItems()); }

  function open() {
    if (!overlay) build();
    collect();
    render();
    overlay.hidden = false;
    input.value = '';
    render();
    setTimeout(() => input.focus(), 0);
  }

  function close() { if (overlay) overlay.hidden = true; }

  function toggle() {
    if (overlay && !overlay.hidden) close();
    else open();
  }
  window.togglePalette = toggle;

  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      toggle();
    } else if (e.key === 'Escape' && overlay && !overlay.hidden) {
      close();
    }
  });
})();
