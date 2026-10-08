/* Aria web client — chat + inspector + ops views.
 * No build step: single file, vanilla JS, works offline except for the
 * optional CDN upgrades (marked/DOMPurify/highlight.js for rich markdown,
 * Google Fonts). If the CDN is unreachable the built-in renderer takes over.
 */
(() => {
  "use strict";

  /* ═══ helpers ═══ */
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtAgo = (ts) => {
    if (ts == null) return "—";
    const s = Math.max(0, (Date.now() / 1000) - Number(ts));
    if (s < 60) return `${Math.round(s)}s ago`;
    if (s < 3600) return `${Math.round(s / 60)}m ago`;
    if (s < 86400) return `${(s / 3600).toFixed(s < 36000 ? 1 : 0)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  };
  const fmtDur = (s) => {
    if (typeof s !== "number" || !isFinite(s)) return "—";
    const m = Math.floor(s / 60), r = (s % 60).toFixed(1);
    return m > 0 ? `${m}m ${r}s` : `${r}s`;
  };
  const fmt$ = (v) => { const n = Number(v || 0); return isFinite(n) ? "$" + n.toFixed(4) : "—"; };
  const fmtN = (v) => Number(v || 0).toLocaleString();

  /* ═══ settings (persisted) ═══ */
  const store = {
    get(k, d) { try { const v = localStorage.getItem("aria2." + k); return v == null ? d : v; } catch { return d; } },
    set(k, v) { try { localStorage.setItem("aria2." + k, v); } catch {} },
  };
  const settings = {
    theme: store.get("theme", "dark"),
    tts: store.get("tts", "off") === "on",
    voice: store.get("voice", "af_heart"),
    speed: parseFloat(store.get("speed", "1.0")) || 1.0,
    compact: store.get("compact", "off") === "on",
  };
  function applyTheme() {
    let t = settings.theme;
    if (t === "system") t = matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
    document.documentElement.dataset.theme = t;
  }
  applyTheme();
  if (matchMedia) matchMedia("(prefers-color-scheme: light)").addEventListener?.("change", () => {
    if (settings.theme === "system") applyTheme();
  });
  if (settings.compact) document.body.classList.add("compact");

  /* ═══ state ═══ */
  const S = {
    convId: store.get("conv", null) || ("c-" + Math.random().toString(36).slice(2, 10)),
    busy: false, abortCtrl: null,
    sessions: [], graph: null, graphSid: null,
    shots: [], shotIdx: 0,
    lastPending: -1,
    costRows: [],
  };
  store.set("conv", S.convId);

  /* ═══ fetch with timeout + one retry ═══ */
  async function fetchJSON(url, opts = {}, timeoutMs = 15000, retry = true) {
    const run = async () => {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), timeoutMs);
      try {
        const r = await fetch(url, { ...opts, signal: ctrl.signal, cache: "no-store" });
        if (!r.ok) { const e = new Error(`HTTP ${r.status}`); e.status = r.status; throw e; }
        return await r.json();
      } finally { clearTimeout(timer); }
    };
    try { return await run(); }
    catch (e) {
      if (retry && (e.name === "AbortError" || e instanceof TypeError)) {
        await new Promise((r) => setTimeout(r, 700));
        return await run();
      }
      throw e;
    }
  }

  /* ═══ toasts ═══ */
  function toast(msg, kind = "info", ms = 3200) {
    const box = $("toasts");
    if (!box) return;
    const t = el("div", "toast " + kind, msg);
    box.append(t);
    requestAnimationFrame(() => t.classList.add("show"));
    setTimeout(() => { t.classList.remove("show"); setTimeout(() => t.remove(), 250); }, ms);
    while (box.children.length > 4) box.firstChild.remove();
  }

  /* ═══ status probe ═══ */
  async function probe() {
    const dot = $("statusDot"), txt = $("statusText"), lat = $("gwLatency");
    try {
      const t0 = performance.now();
      const d = await fetchJSON("/api/health", {}, 6000, false);
      const ms = Math.round(performance.now() - t0);
      if (d.agent === "ready") {
        dot.className = "dot ok";
        txt.textContent = d.gateway_up ? "ready" : "ready (gateway starting)";
        if (lat) lat.textContent = `${ms}ms`;
      } else {
        dot.className = "dot bad"; txt.textContent = "agent unavailable";
      }
    } catch {
      dot.className = "dot bad"; txt.textContent = "server offline";
      if (lat) lat.textContent = "—";
    }
  }

  /* ═══ markdown (CDN up, fallback down) ═══ */
  const useCDN = () => !!(window.marked && window.DOMPurify);
  function renderMarkdown(src) {
    src = String(src || "");
    if (!src.trim()) return "";
    if (useCDN()) {
      try {
        const raw = window.marked.parse(src, { breaks: true, gfm: true });
        const clean = window.DOMPurify.sanitize(raw, {
          ADD_ATTR: ["target", "rel", "class", "id"],
          FORBID_TAGS: ["script", "style", "iframe", "form", "input", "button"],
        });
        const host = el("div");
        host.innerHTML = clean;
        host.querySelectorAll("a[href]").forEach((a) => {
          try {
            const u = new URL(a.getAttribute("href"), location.origin);
            if (!["http:", "https:", "mailto:"].includes(u.protocol)) a.removeAttribute("href");
            else if (u.origin !== location.origin) { a.target = "_blank"; a.rel = "noopener noreferrer"; }
          } catch { a.removeAttribute("href"); }
        });
        host.querySelectorAll("pre code").forEach((c) => {
          const pre = c.parentElement;
          const bar = el("div", "code-head");
          const lang = el("span", "", (c.className.match(/language-(\w+)/) || [])[1] || "code");
          const btn = el("button", "", "Copy");
          btn.type = "button";
          btn.onclick = () => navigator.clipboard.writeText(c.textContent)
            .then(() => { btn.textContent = "Copied!"; setTimeout(() => (btn.textContent = "Copy"), 1500); })
            .catch(() => toast("Copy failed", "error"));
          bar.append(lang, btn);
          pre.prepend(bar);
          try { if (window.hljs) window.hljs.highlightElement(c); } catch {}
        });
        host.querySelectorAll("input[type=checkbox]").forEach((c) => { c.disabled = true; });
        return host.innerHTML;
      } catch { /* fall through to built-in */ }
    }
    return renderMarkdownBasic(src);
  }
  function renderMarkdownBasic(src) {
    const lines = esc(src).split("\n");
    const out = [];
    let inList = false, inCode = false, inQuote = false;
    const codeBuf = [];
    const closeList = () => { if (inList) { out.push("</ul>"); inList = false; } };
    const closeQuote = () => { if (inQuote) { out.push("</blockquote>"); inQuote = false; } };
    const flushCode = () => {
      if (!inCode) return;
      out.push(`<pre><code>${codeBuf.join("\n")}</code></pre>`);
      inCode = false; codeBuf.length = 0;
    };
    const inline = (t) => t
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*(?!\s)([^*\n]+?)\*(?!\*)/g, "$1<em>$2</em>")
      .replace(/`([^`]+?)`/g, "<code>$1</code>")
      .replace(/\[([^\]]+?)\]\((https?:\/\/[^\s)]+|mailto:[^\s)]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
      .replace(/^(\s*)- \[([ xX])\] /, (_, sp, c) =>
        `${sp}<input type="checkbox" disabled${c.toLowerCase() === "x" ? " checked" : ""}> `)
      .replace(/\*\*/g, "");
    for (const raw of lines) {
      const fence = raw.match(/^```(\w*)\s*$/);
      if (fence) { if (inCode) flushCode(); else { closeList(); closeQuote(); inCode = true; } continue; }
      if (inCode) { codeBuf.push(raw); continue; }
      const h = raw.match(/^(#{1,6})\s+(.*)$/);
      if (h) { closeList(); closeQuote(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); continue; }
      if (/^\s*\|.*\|\s*$/.test(raw) && raw.includes("|")) {
        const cells = raw.split("|").slice(1, -1).map((c) => inline(c.trim()));
        if (/^[\s|:|-]+$/.test(raw)) continue;
        closeList(); closeQuote();
        out.push(`<table><tr>${cells.map((c) => `<td>${c}</td>`).join("")}</tr></table>`);
        continue;
      }
      const q = raw.match(/^>\s?(.*)$/);
      if (q) { closeList(); if (!inQuote) { out.push("<blockquote>"); inQuote = true; } out.push(`<p>${inline(q[1])}</p>`); continue; }
      closeQuote();
      const m = raw.match(/^\s*[-*]\s+(.*)$/);
      if (m) { if (!inList) { out.push("<ul>"); inList = true; } out.push("<li>" + inline(m[1]) + "</li>"); continue; }
      closeList();
      if (raw.trim() === "") continue;
      out.push("<p>" + inline(raw) + "</p>");
    }
    flushCode(); closeList(); closeQuote();
    return out.join("");
  }
  function plainText(src) {
    return String(src || "")
      .replace(/\[([^\]]+?)\]\([^)]*\)/g, "$1").replace(/`([^`]+?)`/g, "$1")
      .replace(/\*\*([^*]+?)\*\*/g, "$1").replace(/(^|[^*])\*([^*\n]+?)\*(?!\*)/g, "$1$2")
      .replace(/^\s*[-*]\s+/gm, "").replace(/\n{2,}/g, "\n").trim();
  }

  /* ═══ messages ═══ */
  const messages = $("messages");
  function pinned() {
    if (!messages) return true;
    return messages.scrollHeight - messages.scrollTop - messages.clientHeight < 140;
  }
  function scrollDown(force) {
    if (!messages) return;
    if (force || pinned()) messages.scrollTop = messages.scrollHeight;
  }
  function addUser(text) {
    const wrap = el("div", "msg user");
    const b = el("div", "bubble", text);
    wrap.append(b); messages.append(wrap); scrollDown(true);
  }
  function addAssistant() {
    const wrap = el("div", "msg assistant");
    const b = el("div", "bubble");
    const thinking = el("div", "thinking");
    const head = el("div", "thinking-head");
    head.innerHTML = '<span class="chev">▾</span><span>Agent reasoning</span>';
    const body = el("pre", "thinking-body");
    thinking.append(head, body);
    head.addEventListener("click", () => thinking.classList.toggle("collapsed"));
    const answer = el("div", "answer");
    answer.innerHTML = '<span class="typing"><span></span><span></span><span></span></span>';
    const meta = el("div", "meta");
    meta.style.display = "none";
    const actions = el("div", "msg-actions");
    const copyBtn = el("button", "", "Copy");
    copyBtn.type = "button";
    copyBtn.onclick = () => navigator.clipboard.writeText(answer.textContent)
      .then(() => { copyBtn.textContent = "Copied!"; setTimeout(() => (copyBtn.textContent = "Copy"), 1500); })
      .catch(() => toast("Copy failed", "error"));
    const speakBtn = el("button", "", "🔊 Speak");
    speakBtn.type = "button";
    speakBtn.onclick = () => speak(answer.textContent, true);
    const retryBtn = el("button", "", "Retry");
    retryBtn.type = "button";
    retryBtn.onclick = () => {
      const last = [...document.querySelectorAll(".msg.user .bubble")].pop();
      if (last && last.textContent && !S.busy) send(last.textContent);
    };
    actions.append(copyBtn, speakBtn, retryBtn);
    b.append(thinking, answer, meta, actions);
    wrap.append(b); messages.append(wrap); scrollDown(true);
    return { wrap, thinking, body, answer, meta, bubble: b };
  }
  function addToolCard(bubble, title, payload, open) {
    const d = el("div", "tool-card" + (open ? " open" : ""));
    const head = el("div", "tool-head");
    const name = el("span", "", title);
    const chev = el("span", "chev", "▸");
    head.append(name, chev);
    const body = el("div", "tool-body",
      typeof payload === "string" ? payload : JSON.stringify(payload, null, 2).slice(0, 6000));
    head.onclick = () => d.classList.toggle("open");
    d.append(head, body);
    bubble.append(d);
  }

  /* ═══ gallery + lightbox ═══ */
  function renderShots(container, shots) {
    if (!shots || !shots.length) return;
    const wrap = el("div", "browser-shots");
    wrap.append(el("div", "browser-shots-label", `🌐 Websites visited (${shots.length}) — click to zoom`));
    const grid = el("div", "browser-shots-grid");
    shots.forEach((url, i) => {
      if (!/\.(png|jpg|jpeg|gif|webp)(\?|$)/i.test(url)) return;
      const a = el("a", "browser-shot");
      a.href = url; a.target = "_blank"; a.rel = "noopener";
      const img = document.createElement("img");
      img.src = url; img.loading = "lazy"; img.alt = `Screenshot ${i + 1}`;
      a.append(img);
      a.onclick = (e) => { e.preventDefault(); openLightbox(shots.filter((u) => /\.(png|jpg|jpeg|gif|webp)(\?|$)/i.test(u)), i); };
      grid.append(a);
    });
    if (grid.children.length) { wrap.append(grid); container.append(wrap); }
  }
  function openLightbox(urls, idx) {
    S.shots = urls; S.shotIdx = Math.max(0, idx);
    showShot();
    $("lightbox").hidden = false;
  }
  function showShot() {
    const img = $("lightboxImg");
    img.src = S.shots[S.shotIdx] || "";
    $("lightboxCap").textContent = S.shots.length ? `${S.shotIdx + 1} / ${S.shots.length}` : "";
  }
  function closeLightbox() { $("lightbox").hidden = true; }
  $("lightClose").onclick = closeLightbox;
  $("lightbox").addEventListener("click", (e) => { if (e.target.id === "lightbox") closeLightbox(); });
  $("lightPrev").onclick = () => { S.shotIdx = (S.shotIdx - 1 + S.shots.length) % S.shots.length; showShot(); };
  $("lightNext").onclick = () => { S.shotIdx = (S.shotIdx + 1) % S.shots.length; showShot(); };

  /* ═══ DAG (SVG, layered layout, pan/zoom) ═══ */
  const dagState = { scale: 1, x: 0, y: 0, nodes: [] };
  function renderDag(nodes, edges) {
    const box = $("dag");
    if (!box) return;
    dagState.nodes = nodes || [];
    if (!nodes || !nodes.length) {
      box.innerHTML = '<span class="muted">No run yet — send a message.</span>';
      return;
    }
    const W = 150, H = 40, GX = 26, GY = 22;
    const ids = new Set(nodes.map((n) => n.id));
    const kids = {};
    nodes.forEach((n) => { kids[n.id] = []; });
    (edges || []).forEach((e) => {
      if (ids.has(e.from) && ids.has(e.to)) kids[e.from].push(e.to);
    });
    const depth = {};
    const visit = (id, d, seen) => {
      if (seen.has(id)) return;
      seen.add(id);
      depth[id] = Math.max(depth[id] ?? 0, d);
      (kids[id] || []).forEach((k) => visit(k, d + 1, seen));
    };
    const roots = nodes.filter((n) => !(edges || []).some((e) => e.to === n.id && ids.has(e.from)));
    (roots.length ? roots : nodes.slice(0, 1)).forEach((n) => visit(n.id, 0, new Set()));
    nodes.forEach((n) => { if (!(n.id in depth)) depth[n.id] = 0; });
    const layers = {};
    nodes.forEach((n) => { (layers[depth[n.id]] = layers[depth[n.id]] || []).push(n); });
    const pos = {};
    Object.keys(layers).sort((a, b) => a - b).forEach((L) => {
      layers[L].forEach((n, i) => { pos[n.id] = { x: GX + i * (W + GX), y: GY + Number(L) * (H + GY) }; });
    });
    const maxX = Math.max(...Object.values(pos).map((p) => p.x)) + W + GX;
    const maxY = Math.max(...Object.values(pos).map((p) => p.y)) + H + GY;
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("width", Math.max(maxX, 300));
    svg.setAttribute("height", maxY);
    const g = document.createElementNS(NS, "g");
    (edges || []).forEach((e) => {
      const a = pos[e.from], b = pos[e.to];
      if (!a || !b) return;
      const p = document.createElementNS(NS, "path");
      const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2;
      const mx = (x1 + x2) / 2;
      p.setAttribute("d", `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`);
      p.setAttribute("class", "dag-edge");
      p.setAttribute("marker-end", "url(#arrow)");
      g.append(p);
    });
    const defs = document.createElementNS(NS, "defs");
    defs.innerHTML = `<marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="currentColor" style="color:var(--muted)"/></marker>`;
    g.prepend(defs);
    nodes.forEach((n) => {
      const p = pos[n.id];
      const gg = document.createElementNS(NS, "g");
      gg.setAttribute("class", "dag-node " + (n.status || "pending"));
      gg.setAttribute("transform", `translate(${p.x},${p.y})`);
      const r = document.createElementNS(NS, "rect");
      r.setAttribute("width", W); r.setAttribute("height", H); r.setAttribute("rx", 9);
      const t1 = document.createElementNS(NS, "text");
      t1.setAttribute("x", 10); t1.setAttribute("y", 17);
      t1.textContent = (n.skill || n.id).slice(0, 18);
      const t2 = document.createElementNS(NS, "text");
      t2.setAttribute("x", 10); t2.setAttribute("y", 31);
      t2.setAttribute("opacity", "0.65");
      t2.textContent = `${n.id} · ${n.status || "?"}`.slice(0, 24);
      const title = document.createElementNS(NS, "title");
      title.textContent = `${n.id} [${n.skill}] (${n.status})\ninputs: ${(n.inputs || []).join(", ")}`;
      gg.append(r, t1, t2, title);
      gg.addEventListener("click", () => showNodeDetail(n.id));
      g.append(gg);
    });
    svg.append(g);
    box.replaceChildren(svg);
    applyDagTransform();
    // pan
    let drag = null;
    svg.style.cursor = "grab";
    svg.onpointerdown = (e) => { drag = { x: e.clientX, y: e.clientY, ox: dagState.x, oy: dagState.y }; svg.setPointerCapture(e.pointerId); };
    svg.onpointermove = (e) => {
      if (!drag) return;
      dagState.x = drag.ox + (e.clientX - drag.x); dagState.y = drag.oy + (e.clientY - drag.y);
      applyDagTransform();
    };
    svg.onpointerup = () => { drag = null; };
    svg.onwheel = (e) => {
      e.preventDefault();
      dagState.scale = Math.min(2.2, Math.max(0.4, dagState.scale * (e.deltaY < 0 ? 1.12 : 0.89)));
      applyDagTransform();
    }, { passive: false };
  }
  function applyDagTransform() {
    const g = document.querySelector("#dag svg > g");
    if (g) g.setAttribute("transform", `translate(${dagState.x},${dagState.y}) scale(${dagState.scale})`);
  }
  $("dagZoomIn").onclick = () => { dagState.scale = Math.min(2.2, dagState.scale * 1.2); applyDagTransform(); };
  $("dagZoomOut").onclick = () => { dagState.scale = Math.max(0.4, dagState.scale / 1.2); applyDagTransform(); };
  $("dagRefresh").onclick = () => loadDag();
  async function loadDag() {
    const box = $("dag");
    if (box && !box.querySelector("svg")) box.innerHTML = '<span class="muted">Loading graph…</span>';
    try {
      const d = await fetchJSON("/api/sessions/s8-main/graph", {}, 10000);
      S.graph = d; S.graphSid = d.session_id;
      renderDag(d.nodes || [], d.edges || []);
      const det = $("runDetail");
      if (det && (d.nodes || []).length) {
        const ns = d.nodes;
        const ok = ns.filter((n) => n.status === "complete").length;
        const bad = ns.filter((n) => n.status === "failed").length;
        det.classList.remove("muted");
        det.textContent = `${d.session_id || ""} · ${ok}/${ns.length} complete${bad ? `, ${bad} failed` : ""}`;
      }
    } catch {
      if (box && !box.querySelector("svg")) box.innerHTML = '<span class="muted">No run yet — send a message.</span>';
    }
  }
  /* ═══ TTS / STT / mic ═══ */
  let audioEl = null;
  const ttsChk = $("tts"), voiceSel = $("voiceSel"), speedSel = $("speedSel");
  ttsChk.checked = settings.tts;
  voiceSel.value = settings.voice;
  speedSel.value = String(settings.speed);
  ttsChk.onchange = () => { settings.tts = ttsChk.checked; store.set("tts", settings.tts ? "on" : "off"); if (!settings.tts && audioEl) audioEl.pause(); };
  voiceSel.onchange = () => { settings.voice = voiceSel.value; store.set("voice", settings.voice); };
  speedSel.onchange = () => { settings.speed = parseFloat(speedSel.value); store.set("speed", String(settings.speed)); };
  (async function loadVoices() {
    const fallback = [["af_heart", "af_heart"], ["am_michael", "am_michael"], ["af_sarah", "af_sarah"], ["bm_george", "bm_george"]];
    let list = fallback;
    try {
      const d = await fetchJSON("/api/tts/voices", {}, 6000);
      if (Array.isArray(d.voices) && d.voices.length) {
        list = d.voices.map((v) => typeof v === "string" ? [v, v] : [v.id || v.name, v.label || v.id || v.name]);
      }
    } catch { /* fallback */ }
    voiceSel.replaceChildren();
    list.forEach(([id, label]) => {
      const o = document.createElement("option");
      o.value = id; o.textContent = label;
      if (id === settings.voice) o.selected = true;
      voiceSel.append(o);
    });
  })();
  async function speak(text, force) {
    if ((!settings.tts && !force) || !text) return;
    try {
      const r = await fetch("/api/tts", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: plainText(text).slice(0, 2000), voice: settings.voice, speed: settings.speed }),
      });
      if (!r.ok) return;
      const url = URL.createObjectURL(await r.blob());
      if (audioEl) audioEl.pause();
      audioEl = new Audio(url);
      audioEl.play().catch(() => {});
    } catch { /* best-effort */ }
  }
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  let rec = null, recognizing = false;
  const micBtn = $("mic"), voiceHint = $("voiceHint"), input = $("input");
  if (SR) {
    rec = new SR();
    rec.lang = "en-US"; rec.interimResults = true;
    rec.onresult = (e) => {
      let t = "";
      for (let i = e.resultIndex; i < e.results.length; i++) t += e.results[i][0].transcript;
      input.value = t.trim(); autoGrow();
    };
    rec.onend = () => { recognizing = false; micBtn.classList.remove("listening"); voiceHint.textContent = ""; };
    rec.onerror = (e) => {
      recognizing = false; micBtn.classList.remove("listening");
      voiceHint.textContent = e.error === "not-allowed" ? "microphone permission denied" : "voice input ended";
    };
  } else { micBtn.disabled = true; voiceHint.textContent = "voice input needs Chrome/Edge"; }
  micBtn.onclick = () => {
    if (!rec) return;
    if (recognizing) { rec.stop(); return; }
    try { input.value = ""; rec.start(); recognizing = true; micBtn.classList.add("listening"); voiceHint.textContent = "listening…"; } catch {}
  };
  $("sttBtn").onclick = () => $("audioFile").click();
  $("audioFile").addEventListener("change", async () => {
    const f = $("audioFile").files?.[0];
    if (!f) return;
    addUser(`[audio] ${f.name}`);
    const ui = addAssistant();
    ui.thinking.classList.add("collapsed");
    try {
      voiceHint.textContent = `transcribing ${f.name}…`;
      const fd = new FormData();
      fd.append("file", f);
      const d = await fetchJSON("/api/stt", { method: "POST", body: fd }, 60000);
      if (d.text) { ui.answer.innerHTML = renderMarkdown(d.text); input.value = d.text; autoGrow(); }
      else ui.answer.textContent = "Transcription failed: " + (d.error || "unknown");
    } catch { ui.answer.textContent = "Transcription request failed."; }
    voiceHint.textContent = "";
    $("audioFile").value = "";
  });

  /* ═══ chat send / stream ═══ */
  const form = $("composer"), sendBtn = $("send"), stopBtn = $("stop"), suggestions = $("suggestions");
  function autoGrow() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 170) + "px";
  }
  input.addEventListener("input", autoGrow);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  });
  suggestions.querySelectorAll(".chip").forEach((c) =>
    c.addEventListener("click", () => { const q = c.dataset.q; if (q && !S.busy) send(q); }));
  stopBtn.onclick = () => S.abortCtrl?.abort();
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q || S.busy) return;
    input.value = ""; autoGrow();
    send(q);
  });
  function setBusy(b) {
    S.busy = b; sendBtn.disabled = b; stopBtn.hidden = !b;
  }
  async function streamSSE(endpoint, body, ui, queryLabel) {
    S.abortCtrl = new AbortController();
    const res = await fetch(endpoint, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body), signal: S.abortCtrl.signal,
    });
    if (!res.ok || !res.body) { ui.answer.textContent = `Request failed (${res.status}).`; return null; }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "", rawText = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        if (!chunk.startsWith("data: ")) continue;
        let d;
        try { d = JSON.parse(chunk.slice(6)); } catch { continue; }
        if (d.type === "log") {
          ui.thinking.classList.remove("collapsed");
          ui.body.textContent += d.text + "\n";
          scrollDown();
        } else if (d.type === "meta") {
          const parts = [];
          if (typeof d.elapsed_s === "number") parts.push("⏱ " + fmtDur(d.elapsed_s));
          if (typeof d.cost_usd === "number") parts.push(`💲 $${d.cost_usd.toFixed(4)}`);
          if (typeof d.cost_in_tokens === "number") parts.push(`▾${fmtN(d.cost_in_tokens)} ▴${fmtN(d.cost_out_tokens || 0)}`);
          if (parts.length) { ui.meta.textContent = parts.join("  ·  "); ui.meta.style.display = "block"; }
          if (d.cost_usd != null) {
            const tc = $("turnCost");
            if (tc) {
              tc.classList.remove("muted");
              tc.textContent = `${queryLabel || "turn"}: $${Number(d.cost_usd).toFixed(4)} · ▾${fmtN(d.cost_in_tokens)} ▴${fmtN(d.cost_out_tokens)} tokens`;
            }
          }
        } else if (d.type === "done") {
          rawText = d.answer || "";
          ui.answer.innerHTML = renderMarkdown(rawText) || "<p>(no answer)</p>";
          renderShots(ui.bubble, d.browser_artifacts);
          if (d.conversation_id) { S.convId = d.conversation_id; store.set("conv", S.convId); }
          loadDag();
          speak(rawText);
          scrollDown(true);
          return d;
        } else if (d.type === "error") {
          ui.answer.textContent = "Error: " + d.text;
          return d;
        }
      }
    }
    if (rawText) ui.answer.innerHTML = renderMarkdown(rawText);
    return null;
  }
  async function send(query) {
    if (S.busy) return;
    setBusy(true);
    suggestions.style.display = "none";
    addUser(query);
    $("convTitle").textContent = query.slice(0, 70);
    const ui = addAssistant();
    S.abortCtrl = null;
    try {
      await streamSSE("/api/chat", { query, conversation_id: S.convId }, ui, "turn");
      loadSessionsQuiet();
      loadCost(true);
    } catch (err) {
      ui.answer.textContent = err?.name === "AbortError" ? "⏹ Stopped." : "Connection error: " + (err?.message || err);
    } finally {
      setBusy(false);
      S.abortCtrl = null;
      input.focus();
    }
  }
  function newChat() {
    S.convId = "c-" + Math.random().toString(36).slice(2, 10);
    store.set("conv", S.convId);
    messages.replaceChildren();
    $("convTitle").textContent = "New conversation";
    $("convHint").textContent = "";
    suggestions.style.display = "";
    welcome();
    markActiveConv();
    toast("New conversation", "info", 1500);
  }
  $("newChat").onclick = newChat;
  function welcome() {
    const w = addAssistant();
    w.thinking.classList.add("collapsed");
    w.answer.textContent = "Hi, I'm Aria. Ask me anything — research, code, browse, or chat. Press ⌘K for commands.";
    const b = w.bubble.querySelector(".msg-actions");
    if (b) b.remove();
  }

  function showNodeDetail(nid) {
    const det = $("runDetail");
    if (!det || !S.graph) return;
    const states = S.graph.node_states || [];
    const st = states.find((s) => s.node_id === nid);
    const g = (S.graph.nodes || []).find((n) => n.id === nid);
    det.classList.remove("muted");
    det.innerHTML = "";
    det.append(el("div", "", `${nid} · ${g?.skill || "?"} · ${g?.status || "?"}`));
    const dl = document.createElement("dl");
    const row = (k, v) => {
      dl.append(el("dt", "", k));
      const dd = el("dd", "", String(v ?? "—").slice(0, 600));
      dl.append(dd);
    };
    const r = st?.result || {};
    row("elapsed", r.elapsed_s != null ? fmtDur(r.elapsed_s) : "—");
    row("provider", r.provider || "—");
    row("cost", r.cost != null ? String(r.cost) : "—");
    if (r.error) row("error", r.error);
    if (r.error_code) row("error_code", r.error_code);
    const out = r.output || {};
    row("output keys", Object.keys(out).join(", ") || "—");
    if (out.layer) row("layer", out.layer);
    const wrap = el("div", "node-detail");
    wrap.append(dl);
    det.append(wrap);
  }

  /* ═══ sidebar conversations ═══ */
  let convFilter = "";
  $("convSearch").addEventListener("input", (e) => { convFilter = e.target.value.toLowerCase(); renderConvs(); });
  function markActiveConv() { renderConvs(); }
  function renderConvs() {
    const box = $("convList");
    box.replaceChildren();
    const list = S.sessions.filter((s) =>
      !convFilter || (s.query || "").toLowerCase().includes(convFilter) || s.session_id.includes(convFilter));
    if (!list.length) { box.append(el("div", "sess-empty", convFilter ? "No matches." : "No conversations yet.")); return; }
    list.slice(0, 60).forEach((s) => {
      const b = el("button", "sess" + (s.conversation_id === S.convId ? " active" : ""));
      b.setAttribute("role", "option");
      b.append(el("div", "t", s.query || s.session_id));
      b.append(el("div", "m", `${s.session_id} · ${s.nodes} node${s.nodes === 1 ? "" : "s"}`));
      b.title = `${s.query || s.session_id}\n${(s.skills || []).join(", ")}`;
      b.onclick = () => {
        if (s.conversation_id) { S.convId = s.conversation_id; store.set("conv", S.convId); }
        $("convTitle").textContent = (s.query || s.session_id).slice(0, 70);
        S.graphSid = s.session_id;
        fetchJSON(`/api/sessions/${encodeURIComponent(s.session_id)}/graph`, {}, 10000)
          .then((d) => { S.graph = d; renderDag(d.nodes || [], d.edges || []); })
          .catch(() => {});
        gotoView("chat");
        markActiveConv();
      };
      box.append(b);
    });
  }
  async function loadSessionsQuiet() {
    try {
      const d = await fetchJSON("/api/sessions?limit=100", {}, 12000, false);
      S.sessions = d.sessions || [];
      $("sessCount").textContent = `${S.sessions.length} session${S.sessions.length === 1 ? "" : "s"}`;
      renderConvs();
    } catch { /* sidebar is best-effort */ }
  }

  /* ═══ sessions view ═══ */
  $("sessRefresh").onclick = loadSessionsTable;
  $("sessFilter").addEventListener("input", loadSessionsTable);
  async function loadSessionsTable() {
    const box = $("sessTable");
    box.innerHTML = '<span class="muted">Loading…</span>';
    try {
      const q = ($("sessFilter").value || "").toLowerCase();
      const rows = S.sessions.filter((s) =>
        !q || (s.query || "").toLowerCase().includes(q) || s.session_id.includes(q)).slice(0, 80);
      if (!rows.length) { box.innerHTML = '<span class="muted">No sessions found.</span>'; return; }
      const t = document.createElement("table");
      t.innerHTML = "<thead><tr><th>Query</th><th>Session</th><th>Nodes</th><th>Skills</th><th></th></tr></thead>";
      const tb = document.createElement("tbody");
      rows.forEach((s) => {
        const tr = document.createElement("tr");
        const qtd = document.createElement("td"); qtd.textContent = (s.query || "—").slice(0, 90);
        const sid = document.createElement("td"); sid.innerHTML = `<code>${esc(s.session_id)}</code>`;
        const nn = document.createElement("td"); nn.className = "num"; nn.textContent = s.nodes;
        const sk = document.createElement("td"); sk.textContent = (s.skills || []).join(", ");
        const go = document.createElement("td");
        const view = el("button", "link", "View");
        view.onclick = () => {
          S.graphSid = s.session_id;
          fetchJSON(`/api/sessions/${encodeURIComponent(s.session_id)}/graph`, {}, 10000)
            .then((d) => { S.graph = d; renderDag(d.nodes || [], d.edges || []); gotoView("chat"); })
            .catch(() => toast("Graph load failed", "error"));
        };
        const reuse = el("button", "link", "Reuse");
        reuse.onclick = () => { input.value = s.query || ""; autoGrow(); gotoView("chat"); input.focus(); };
        go.append(view, reuse);
        tr.append(qtd, sid, nn, sk, go);
        tb.append(tr);
      });
      t.append(tb);
      box.replaceChildren(t);
    } catch { box.innerHTML = '<span class="muted">Sessions load failed.</span>'; }
  }

  /* ═══ analytics ═══ */
  $("costRefresh").onclick = () => loadAnalytics();
  function barChart(canvas, items, fmt) {
    const dpr = devicePixelRatio || 1;
    const W = canvas.clientWidth || canvas.parentElement.clientWidth || 600;
    const H = parseInt(canvas.getAttribute("height") || "220", 10);
    canvas.width = W * dpr; canvas.height = H * dpr;
    canvas.style.height = H + "px";
    const c = canvas.getContext("2d");
    c.scale(dpr, dpr);
    c.clearRect(0, 0, W, H);
    const cs = getComputedStyle(document.body);
    const muted = cs.getPropertyValue("--muted").trim() || "#888";
    const ink = cs.getPropertyValue("--text").trim() || "#fff";
    if (!items.length) {
      c.fillStyle = muted; c.font = "12px Inter, sans-serif";
      c.fillText("No data yet — run a task.", 12, 24);
      return;
    }
    const max = Math.max(...items.map((i) => i.v), 1e-9);
    const n = Math.min(items.length, 14);
    const gap = (W - 20) / n;
    const bw = Math.min(64, gap * 0.55);
    const grad = c.createLinearGradient(0, 0, 0, H - 40);
    grad.addColorStop(0, cs.getPropertyValue("--accent").trim() || "#6d5efc");
    grad.addColorStop(1, cs.getPropertyValue("--accent-2").trim() || "#36c5f0");
    c.textAlign = "center";
    items.slice(0, 14).forEach((it, i) => {
      const h = Math.max(3, (H - 56) * (it.v / max));
      const x = 10 + gap * i + (gap - bw) / 2;
      const y = H - 30 - h;
      c.fillStyle = grad;
      c.beginPath();
      if (c.roundRect) c.roundRect(x, y, bw, h, 4); else c.rect(x, y, bw, h);
      c.fill();
      c.fillStyle = muted; c.font = "10px Inter, sans-serif";
      c.fillText(String(it.k).slice(0, 12), x + bw / 2, H - 14);
      c.fillStyle = ink; c.font = "10px 'JetBrains Mono', monospace";
      c.fillText(fmt(it.v), x + bw / 2, y - 5);
    });
  }
  async function loadAnalytics() {
    try {
      const d = await fetchJSON("/api/cost", {}, 15000);
      const rows = d.rows || [];
      const t = d.totals || {};
      const cards = [
        ["Total spend", fmt$((t.dollars ?? t.usd))], ["Calls", fmtN(t.calls)],
        ["Input tokens", fmtN(t.in_tokens)], ["Output tokens", fmtN(t.out_tokens)],
        ["Turns", fmtN(d.turns)], ["Sessions", fmtN(S.sessions.length)],
      ];
      const sc = $("statCards");
      sc.replaceChildren();
      cards.forEach(([k, v]) => {
        const c = el("div", "stat");
        c.append(el("div", "v", v), el("div", "k", k));
        sc.append(c);
      });
      barChart($("chartSpend"), rows.map((r) => ({ k: r.agent || "?", v: Number(r.dollars ?? r.usd ?? 0) })), (v) => "$" + v.toFixed(4));
      barChart($("chartToks"), rows.map((r) => ({ k: r.agent || "?", v: (Number(r.in_tokens ?? r.in_tok ?? 0) + Number(r.out_tokens ?? r.out_tok ?? 0)) })), (v) => fmtN(Math.round(v)));
      const tb = $("costTable");
      tb.innerHTML = "";
      if (!rows.length) { tb.innerHTML = '<span class="muted">No cost rows yet.</span>'; }
      else {
        const t2 = document.createElement("table");
        t2.innerHTML = "<thead><tr><th>Agent</th><th>Provider</th><th>Calls</th><th>In</th><th>Out</th><th>$</th></tr></thead>";
        const b = document.createElement("tbody");
        rows.forEach((r) => {
          const tr = document.createElement("tr");
          const td = (txt, num) => {
            const c = document.createElement("td");
            c.textContent = txt; if (num) c.className = "num";
            tr.append(c);
          };
          td(r.agent || "—", false);
          td(r.provider || "—", false);
          td(fmtN(r.calls), true);
          td(fmtN(r.in_tokens ?? r.in_tok), true);
          td(fmtN(r.out_tokens ?? r.out_tok), true);
          td(fmt$(r.dollars ?? r.usd), true);
          b.append(tr);
        });
        t2.append(b);
        tb.append(t2);
      }
      const top = rows.slice().sort((a, b) => (b.calls || 0) - (a.calls || 0))[0];
      $("modelBadge").textContent = top ? `${top.provider || top.agent} · ${top.calls}` : "—";
      const tot = Number((d.totals || {}).dollars ?? (d.totals || {}).usd ?? 0);
      $("spendFoot").textContent = tot > 0 ? `${fmt$(tot)} today` : `${fmtN((d.totals || {}).calls || 0)} calls today`;
    } catch {
      $("costTable").innerHTML = '<span class="muted">Analytics load failed.</span>';
    }
  }

  /* ═══ tools / config ═══ */
  let _toolsCache = [], _skillsCache = [];
  $("toolsRefresh").onclick = loadTools;
  $("toolsSearch").addEventListener("input", renderTools);
  async function loadTools() {
    const grid = $("toolsGrid");
    grid.innerHTML = '<span class="muted">Loading…</span>';
    try {
      const [t, c] = await Promise.all([
        fetchJSON("/api/tools", {}, 12000),
        fetchJSON("/api/config", {}, 12000).catch(() => ({})),
      ]);
      _toolsCache = t.tools || [];
      const parsed = c.parsed || {};
      _skillsCache = Object.keys(parsed).map((k) => ({ name: k, ...(parsed[k] || {}) }));
      renderTools();
      renderSkills(_skillsCache, c.raw || "");
    } catch { grid.innerHTML = '<span class="muted">Tools load failed.</span>'; }
  }
  function renderTools() {
    const grid = $("toolsGrid");
    const q = ($("toolsSearch").value || "").toLowerCase();
    const list = _toolsCache.filter((t) =>
      !q || t.name.toLowerCase().includes(q) || (t.description || "").toLowerCase().includes(q));
    $("toolsCount").textContent = `${list.length} of ${_toolsCache.length} tools`;
    grid.replaceChildren();
    list.forEach((t) => {
      const card = el("div", "tool-card2");
      card.append(el("h4", "", t.name));
      card.append(el("p", "", t.description || "—"));
      const row = el("div", "meta-row");
      row.append(el("span", "tag", t.kind === "skill" ? "catalog" : t.kind || "mcp"));
      const usedBy = _skillsCache.filter((s) => (s.tools || s.tools_allowed || []).includes(t.name)).map((s) => s.name);
      if (usedBy.length) row.append(el("span", "tag", "used by: " + usedBy.join(", ")));
      const cp = el("button", "link", "Copy name");
      cp.onclick = () => navigator.clipboard.writeText(t.name).then(() => toast("Copied " + t.name, "success", 1500));
      row.append(cp);
      card.append(row);
      grid.append(card);
    });
    if (!list.length) grid.append(el("span", "muted", "No tools match."));
  }
  function renderSkills(skills, raw) {
    const box = $("skillCards");
    box.replaceChildren();
    skills.forEach((s) => {
      const card = el("div", "tool-card2");
      card.append(el("h4", "", s.name));
      card.append(el("p", "", (s.description || "").slice(0, 220)));
      const row = el("div", "meta-row");
      (s.tools || s.tools_allowed || []).forEach((t) => row.append(el("span", "tag", t)));
      if (s.temperature != null) row.append(el("span", "tag", `T=${s.temperature}`));
      if (s.max_tokens != null) row.append(el("span", "tag", `${s.max_tokens} tok`));
      card.append(row);
      box.append(card);
    });
    $("configPre").textContent = raw || "empty";
  }
  async function loadConfig() {
    try {
      const c = await fetchJSON("/api/config", {}, 12000);
      renderSkills(Object.keys(c.parsed || {}).map((k) => ({ name: k, ...(c.parsed[k] || {}) })), c.raw || c.error || "");
    } catch { $("configPre").textContent = "config load failed"; }
  }
  $("configSearch").addEventListener("input", () => {
    const q = $("configSearch").value;
    const pre = $("configPre");
    const raw = pre.textContent;
    if (!q) { pre.innerHTML = esc(raw); return; }
    pre.innerHTML = esc(raw).replace(new RegExp(`(${q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "gi"), "<mark>$1</mark>");
  });

  /* ═══ computer ops ═══ */
  let _lastPending = -1;
  $("cuRefresh").onclick = loadComputer;
  $("auditSearch").addEventListener("input", () => renderAudit());
  let _auditLines = [];
  async function loadComputer() {
    try {
      const d = await fetchJSON("/api/computer/approvals", {}, 15000);
      const caps = d.capabilities || {};
      $("cuCap").textContent = `daemon ${caps.daemon_running ? "up" : "offline"} · AX ${caps.ax_ok ? "ok" : "n/a"} · mode ${d.mode || "?"}`;
      const dry = $("dryBanner");
      if (dry) dry.hidden = (d.mode || "dry-run") !== "dry-run";
      const pending = d.approvals || [];
      const badge = $("cuBadge");
      badge.hidden = !pending.length;
      badge.textContent = String(pending.length);
      if (_lastPending >= 0 && pending.length > _lastPending) {
        toast(`${pending.length} computer action(s) need approval`, "warn", 6000);
      }
      _lastPending = pending.length;
      paintApprovals($("cuList"), pending);
      const panelAppr = $("cuApprovals");
      if (panelAppr) paintApprovals(panelAppr, pending);
      const status2 = $("cuStatus");
      if (status2) status2.textContent = $("cuCap").textContent;
    } catch { $("cuCap").textContent = "approvals load failed"; }
    try {
      const d = await fetchJSON("/api/computer/runs", {}, 12000);
      paintRuns($("replayList"), d.runs || [], $("replayDetail"));
      const panelRuns = $("replayList2");
      if (panelRuns) paintRuns(panelRuns, (d.runs || []).slice(0, 8), null);
    } catch { /* best-effort */ }
    try {
      const d = await fetchJSON("/api/notifications?limit=20", {}, 10000);
      paintNotif($("notifList"), d.notifications || []);
      const panelNotif = $("notifList2");
      if (panelNotif) paintNotif(panelNotif, (d.notifications || []).slice(0, 8));
    } catch { /* best-effort */ }
    try {
      const d = await fetchJSON("/api/audit?redact=true", {}, 12000);
      _auditLines = d.lines || [];
      renderAudit();
    } catch {
      $("cuAudit").textContent = "audit load failed";
    }
  }
  function renderAudit() {
    const q = ($("auditSearch").value || "").toLowerCase();
    const lines = _auditLines.filter((l) => !q || l.toLowerCase().includes(q)).slice(-120);
    $("cuAudit").textContent = lines.length ? lines.join("\n") : "Audit log is empty.";
  }
  function paintApprovals(ul, pending) {
    if (!ul) return;
    ul.replaceChildren();
    if (!pending.length) { ul.append(el("li", "muted", "No pending approvals.")); return; }
    pending.forEach((a) => {
      const li = el("li", "cu-item");
      li.append(el("div", "cu-label", `${a.action} ${JSON.stringify(a.params || {})}`));
      const row = el("div", "cu-actions");
      [["Approve", true, "cu-approve"], ["Reject", false, "cu-reject"]].forEach(([txt, ok, cls]) => {
        const b = el("button", cls, txt);
        b.type = "button";
        b.onclick = async () => {
          try {
            await fetchJSON(`/api/computer/approvals/${encodeURIComponent(a.id)}`, {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ approve: ok }),
            });
            toast(ok ? "Approved" : "Rejected", "success");
          } catch { toast("Approval request failed", "error"); }
          loadComputer();
        };
        row.append(b);
      });
      li.append(row);
      ul.append(li);
    });
  }
  function paintRuns(ul, runs, det) {
    if (!ul) return;
    ul.replaceChildren();
    if (!runs.length) { ul.append(el("li", "muted", "No recorded runs yet.")); return; }
    runs.forEach((r) => {
      const li = el("li", "cu-item");
      li.append(el("div", "cu-label", `${r.id} · ${r.turns} turn(s)`));
      const row = el("div", "cu-actions");
      const view = el("button", "replay-view", "View");
      view.type = "button";
      view.onclick = () => showRun(r.id, det);
      const rep = el("button", "replay-run", "Replay");
      rep.type = "button";
      rep.onclick = async () => {
        try {
          const x = await fetchJSON(`/api/computer/replay/${encodeURIComponent(r.id)}`, { method: "POST" });
          toast(x.status === "ok" ? "Replay started" : "Replay failed: " + (x.message || ""), x.status === "ok" ? "success" : "error");
        } catch { toast("Replay request failed", "error"); }
      };
      row.append(view, rep);
      li.append(row);
      ul.append(li);
    });
  }
  function paintNotif(ul, items) {
    if (!ul) return;
    ul.replaceChildren();
    if (!items.length) { ul.append(el("li", "muted", "No deliveries yet.")); return; }
    items.forEach((n) => {
      const li = el("li", "cu-item");
      const when = n.ts ? new Date(n.ts * 1000).toLocaleString() : "";
      li.textContent = `${when} · [${n.kind || "info"}] ${n.text || ""}`.slice(0, 400);
      li.title = n.text || "";
      ul.append(li);
    });
  }
  async function showRun(id, det) {
    det = det || $("replayDetail");
    if (!det) return;
    det.hidden = false;
    det.textContent = "loading…";
    try {
      const d = await fetchJSON(`/api/computer/runs/${encodeURIComponent(id)}`, {}, 12000);
      if (d.error) { det.textContent = d.error; return; }
      det.replaceChildren();
      det.append(el("h3", "", `Run ${d.id}`));
      (d.turns || []).forEach((t) => {
        det.append(el("div", "replay-turn", `Turn ${t.turn}: ${(t.calls || []).length} action(s)`));
        (t.calls || []).slice(0, 12).forEach((c) => {
          det.append(el("div", "replay-call", `${c.tool || "?"} ${JSON.stringify(c.args || {})}`));
        });
      });
    } catch { det.textContent = "failed to load run"; }
  }

  /* ═══ templates ═══ */
  $("tmplCreate").onclick = async () => {
    const name = ($("tmplName").value || "").trim();
    const query = ($("tmplQuery").value || "").trim();
    const vars = ($("tmplVars").value || "").split(",").map((s) => s.trim()).filter(Boolean);
    if (!name || !query) { toast("name and query are required", "error"); return; }
    try {
      const d = await fetchJSON("/api/templates", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, query, vars }),
      });
      if (d.status === "ok") {
        $("tmplName").value = $("tmplQuery").value = $("tmplVars").value = "";
        loadTemplates();
        toast("Template saved", "success");
      } else toast(d.message || "save failed", "error");
    } catch { toast("save request failed", "error"); }
  };
  async function loadTemplates() {
    const box = $("tmplList");
    box.innerHTML = '<span class="muted">Loading…</span>';
    try {
      const d = await fetchJSON("/api/templates", {}, 10000);
      box.replaceChildren();
      if (!(d.templates || []).length) { box.append(el("span", "muted", "No templates yet — save one above.")); return; }
      d.templates.forEach((t) => {
        const card = el("div", "tool-card2");
        card.append(el("h4", "", t.name));
        card.append(el("p", "tpl-query", t.query));
        const vars = t.vars || [];
        if (vars.length) {
          const vf = el("div", "tpl-vars");
          vars.forEach((v) => {
            const i = document.createElement("input");
            i.placeholder = v;
            i.dataset.var = v;
            vf.append(i);
          });
          card.append(vf);
        }
        const row = el("div", "meta-row");
        const run = el("button", "link", "▶ Run");
        run.onclick = () => {
          const values = {};
          card.querySelectorAll(".tpl-vars input").forEach((i) => { values[i.dataset.var] = i.value; });
          runTemplate(t.name, values);
        };
        const del = el("button", "link", "Delete");
        del.onclick = async () => {
          try {
            await fetchJSON(`/api/templates/${encodeURIComponent(t.name)}`, { method: "DELETE" });
            loadTemplates();
            toast("Template deleted", "success");
          } catch { toast("delete failed", "error"); }
        };
        row.append(run, del);
        card.append(row);
        box.append(card);
      });
    } catch { box.innerHTML = '<span class="muted">Templates load failed.</span>'; }
  }
  async function runTemplate(name, values) {
    if (S.busy) { toast("Busy — wait for the current run", "warn"); return; }
    gotoView("chat");
    setBusy(true);
    suggestions.style.display = "none";
    addUser(`▶ ${name} ${Object.entries(values).map(([k, v]) => `${k}=${v}`).join(" ")}`.trim());
    $("convTitle").textContent = "▶ " + name;
    const ui = addAssistant();
    try {
      await streamSSE(`/api/templates/${encodeURIComponent(name)}/run`,
        { vars: values, conversation_id: S.convId }, ui, name);
      loadSessionsQuiet();
      loadCost(true);
    } catch (err) {
      ui.answer.textContent = err?.name === "AbortError" ? "⏹ Stopped." : "Connection error: " + (err?.message || err);
    } finally { setBusy(false); input.focus(); }
  }

  /* ═══ memory + schedule panels ═══ */
  let memQ = "", memKind = "";
  async function loadMem() {
    const box = $("memPanel");
    try {
      const d = await fetchJSON("/api/memory?limit=60" + (memQ ? "&q=" + encodeURIComponent(memQ) : ""), {}, 10000);
      let items = d.items || [];
      if (memKind) items = items.filter((m) => m.kind === memKind);
      box.replaceChildren();
      const bar = el("div", "meta-row");
      ["", "fact", "preference", "tool_outcome", "scratchpad"].forEach((k) => {
        const b = el("button", "chip" + (memKind === k ? " active" : ""), k || "all");
        b.onclick = () => { memKind = k; loadMem(); };
        bar.append(b);
      });
      const search = document.createElement("input");
      search.className = "search";
      search.placeholder = "Search memory…";
      search.value = memQ;
      search.oninput = () => { clearTimeout(search._t); search._t = setTimeout(() => { memQ = search.value.trim(); loadMem(); }, 300); };
      box.append(search, bar);
      if (!items.length) box.append(el("div", "muted", memQ ? "No matches." : "No memories yet."));
      const ul = el("ul", "mem-list");
      items.slice(0, 60).forEach((m) => {
        const li = el("li", "mem-row");
        li.append(el("span", "mem-kind", m.kind));
        const v = el("span", "mem-val", m.descriptor);
        v.title = `${m.id} · ${(m.keywords || []).join(", ")} · ${m.source || ""}`;
        li.append(v);
        ul.append(li);
      });
      box.append(ul);
      await loadPolicies(box);
    } catch { box.innerHTML = '<span class="muted">Memory load failed.</span>'; }
  }
  /* ═══ policy editor (Phase 3 drawers: operator-owned rules) ═══ */
  let policyReplaceId = "";
  async function loadPolicies(box) {
    const wrap = el("div", "policy-wrap");
    wrap.append(el("div", "meta-row", "Policies (operator rules — injected into every skill prompt)"));
    try {
      const d = await fetchJSON("/api/memory?drawers=policy&limit=50", {}, 10000);
      const items = d.items || [];
      const ul = el("ul", "mem-list");
      if (!items.length) ul.append(el("li", "muted", "No policies yet."));
      items.forEach((m) => {
        const li = el("li", "mem-row");
        li.append(el("span", "mem-kind", m.superseded_by ? "revoked" : "policy"));
        const v = el("span", "mem-val", m.descriptor);
        v.title = `${m.id}${m.superseded_by ? " · superseded by " + m.superseded_by : ""}`;
        li.append(v);
        if (!m.superseded_by) {
          const rep = el("button", "chip", "replace");
          rep.title = "Revoke this rule by writing its replacement";
          rep.onclick = () => {
            policyReplaceId = m.id;
            $("policyInput").value = m.descriptor;
            $("policyInput").focus();
            toast("Replacement armed — edit and save", "success");
          };
          li.append(rep);
        }
        ul.append(li);
      });
      wrap.append(ul);
    } catch { wrap.append(el("div", "muted", "Policy load failed.")); }
    const form = el("form", "meta-row");
    const input = document.createElement("input");
    input.id = "policyInput";
    input.className = "search";
    input.placeholder = policyReplaceId ? "Replacement rule…" : "New policy rule…";
    const save = el("button", "chip", policyReplaceId ? "save replacement" : "add policy");
    form.append(input, save);
    form.onsubmit = async (e) => {
      e.preventDefault();
      const text = (input.value || "").trim();
      if (!text) return;
      try {
        const d = await fetchJSON("/api/memory/policy", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text, supersedes: policyReplaceId || undefined }),
        });
        if (d.status === "ok") {
          input.value = ""; policyReplaceId = "";
          loadMem();
          toast("Policy saved", "success");
        } else toast(d.message || "policy save failed", "error");
      } catch { toast("policy request failed", "error"); }
    };
    wrap.append(form);
    box.append(wrap);
  }
  async function loadSched() {
    try {
      const d = await fetchJSON("/api/schedule", {}, 10000);
      const ul = $("schedRows");
      ul.replaceChildren();
      if (!(d.schedules || []).length) ul.append(el("li", "muted", "No scheduled tasks. Add one above."));
      (d.schedules || []).forEach((s) => {
        const li = el("li", "sched-row");
        li.append(el("span", "sched-q", s.query));
        const fire = s.next_fire ? new Date(s.next_fire * 1000).toLocaleString() : (s.when || "");
        li.append(el("span", "sched-when", `${s.enabled === false ? "paused · " : ""}${s.recurring || ""} ${fire}`));
        const del = el("button", "chip", "✕");
        del.title = "Cancel task (pause)";
        del.onclick = async () => {
          try {
            await fetchJSON(`/api/schedule/${encodeURIComponent(s.id)}`, { method: "DELETE" });
            loadSched();
            toast("Schedule cancelled", "success");
          } catch { toast("Failed to cancel", "error"); }
        };
        const hard = el("button", "chip", "🗑");
        hard.title = "Delete permanently";
        hard.onclick = async () => {
          try {
            await fetchJSON(`/api/schedule/${encodeURIComponent(s.id)}?hard=true`, { method: "DELETE" });
            loadSched();
            toast("Schedule deleted", "success");
          } catch { toast("Failed to delete", "error"); }
        };
        li.append(del, hard);
        ul.append(li);
      });
    } catch { $("schedRows").innerHTML = '<li class="muted">schedule load failed</li>'; }
  }
  $("schedForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const query = ($("schedQuery").value || "").trim();
    const when = ($("schedWhen").value || "").trim();
    if (!query || !when) return;
    try {
      const d = await fetchJSON("/api/schedule", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, when, conversation_id: S.convId }),
      });
      if (d.status === "ok") {
        $("schedQuery").value = ""; $("schedWhen").value = "";
        loadSched();
        toast("Schedule added", "success");
      } else toast(d.message || "schedule failed", "error");
    } catch { toast("schedule request failed", "error"); }
  });

  /* ═══ quick chips / theme / palette ═══ */
  const chipAction = { costToggle: "cost", schedToggle: "sched", memToggle: "mem", compToggle: "comp" };
  Object.keys(chipAction).forEach((id) => {
    $(id).addEventListener("click", () => {
      const w = chipAction[id];
      const isOpen = document.body.classList.toggle("panel-open-" + w);
      $$(".quick-actions .chip").forEach((c) => c.classList.remove("active"));
      if (isOpen) $(id).classList.add("active");
      openPanel(w, isOpen);
    });
  });
  function openPanel(which, force) {
    ["cost", "sched", "mem", "comp"].forEach((w) => {
      $(w + "Panel").hidden = w !== which || force === false;
    });
    $("sideTitle").textContent = { cost: "Spend", sched: "Schedule", mem: "Memory", comp: "Computer" }[which];
    $("sidePanel").classList.add("open");
    $("sidePanel").setAttribute("aria-hidden", "false");
    if (which === "cost") loadCostPanel();
    if (which === "sched") loadSched();
    if (which === "mem") loadMem();
    if (which === "comp") loadComputer();
  }
  function closePanel() {
    $("sidePanel").classList.remove("open");
    $("sidePanel").setAttribute("aria-hidden", "true");
    $$(".quick-actions .chip").forEach((c) => c.classList.remove("active"));
    ["cost", "sched", "mem", "comp"].forEach((w) => document.body.classList.remove("panel-open-" + w));
  }
  $("sideClose").onclick = closePanel;
  async function loadCostPanel() {
    try {
      const d = await fetchJSON("/api/cost", {}, 12000);
      const t = d.totals || {};
      $("costTotals").innerHTML = "";
      const s1 = el("span", "", "Total: ");
      s1.append(el("b", "", fmt$((t.dollars ?? t.usd))));
      $("costTotals").append(s1,
        el("span", "", `${fmtN(t.in_tokens)} in · ${fmtN(t.out_tokens)} out`),
        el("span", "", `${fmtN(t.calls)} calls`));
      const ul = $("costRows");
      ul.replaceChildren();
      (d.rows || []).forEach((r) => {
        const li = el("li", "cost-row");
        li.innerHTML = `<span>${esc(r.agent || "?")}</span><span class="num">${fmtN(r.in_tokens)}</span>` +
          `<span class="num">${fmtN(r.out_tokens)}</span><span class="num">${fmtN(r.calls)}</span>` +
          `<span class="usd">${fmt$(r.dollars)}</span>`;
        ul.append(li);
      });
      if (!(d.rows || []).length) ul.append(el("li", "muted", "No usage yet — run a task."));
    } catch { $("costTotals").textContent = "cost load failed"; }
  }
  function cycleTheme() {
    settings.theme = settings.theme === "dark" ? "light" : settings.theme === "light" ? "system" : "dark";
    store.set("theme", settings.theme);
    applyTheme();
    toast("Theme: " + settings.theme, "info", 1500);
  }
  $("themeBtn").onclick = cycleTheme;

  /* ═══ tabs / mobile nav ═══ */
  const $$ = (sel) => document.querySelectorAll(sel);
  function gotoView(v) {
    document.querySelector(`.views-nav .tab[data-view="${v}"]`)?.click();
  }
  document.querySelectorAll(".views-nav .tab").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll(".views-nav .tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".pane").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $("view-" + b.dataset.view).classList.add("active");
    document.body.classList.remove("nav-open");
    const v = b.dataset.view;
    if (v === "sessions") loadSessionsTable();
    if (v === "analytics") loadAnalytics();
    if (v === "tools") loadTools();
    if (v === "config") loadConfig();
    if (v === "computer") loadComputer();
    if (v === "templates") loadTemplates();
  }));
  $("menuBtn").onclick = () => document.body.classList.add("nav-open");
  $("menuClose").onclick = () => document.body.classList.remove("nav-open");
  $("scrim").onclick = () => document.body.classList.remove("nav-open");

  /* ═══ command palette ═══ */
  const palWrap = $("paletteWrap"), palInput = $("paletteInput"), palList = $("paletteList");
  let palItems = [], palSel = 0;
  function openPalette() {
    palWrap.hidden = false;
    palInput.value = "";
    buildPalette("");
    setTimeout(() => palInput.focus(), 30);
  }
  function closePalette() { palWrap.hidden = true; }
  $("paletteBtn").onclick = openPalette;
  palWrap.addEventListener("click", (e) => { if (e.target === palWrap) closePalette(); });
  palInput.addEventListener("input", () => buildPalette(palInput.value));
  palInput.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); palSel = Math.min(palSel + 1, palItems.length - 1); paintPalette(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); palSel = Math.max(palSel - 1, 0); paintPalette(); }
    else if (e.key === "Enter") { e.preventDefault(); if (palItems[palSel]) { palItems[palSel].run(); closePalette(); } }
    else if (e.key === "Escape") closePalette();
  });
  const palEntry = (icon, title, sub, run) => ({ icon, title, sub, run });
  function buildPalette(q) {
    q = (q || "").toLowerCase();
    const items = [
      palEntry("+", "New chat", "start a fresh conversation", () => newChat()),
      palEntry("◐", "Cycle theme", "now: " + settings.theme, () => cycleTheme()),
      palEntry("🔊", (settings.tts ? "Disable" : "Enable") + " spoken responses", "Kokoro TTS", () => {
        ttsChk.checked = !settings.tts; ttsChk.onchange();
      }),
      palEntry("▦", "Toggle compact density", settings.compact ? "on" : "off", () => {
        settings.compact = !settings.compact;
        store.set("compact", settings.compact ? "on" : "off");
        document.body.classList.toggle("compact", settings.compact);
      }),
      palEntry("🗑", "Clear conversation", "clears visible messages", () => {
        if (confirm("Clear all messages? This can't be undone.")) {
          messages.replaceChildren(); suggestions.style.display = ""; welcome();
        }
      }),
      ...["chat", "sessions", "analytics", "tools", "config", "computer", "templates"].map((v) =>
        palEntry("→", "Go to " + v, "switch view", () => gotoView(v))),
      ...S.sessions.slice(0, 30).map((s) => palEntry("💬", (s.query || s.session_id).slice(0, 60),
        `${s.session_id} · ${s.nodes} nodes`, () => viewSession(s))),
    ];
    palItems = items.filter((i) => !q || (i.title + " " + (i.sub || "")).toLowerCase().includes(q)).slice(0, 40);
    palSel = 0;
    paintPalette();
  }
  function paintPalette() {
    palList.replaceChildren();
    palItems.forEach((it, i) => {
      const li = el("li", i === palSel ? "sel" : "");
      li.setAttribute("role", "option");
      li.append(el("span", "", it.icon + " " + it.title));
      if (it.sub) li.append(el("span", "sub", it.sub));
      li.onclick = () => { it.run(); closePalette(); };
      li.onmousemove = () => { if (palSel !== i) { palSel = i; paintPalette(); } };
      palList.append(li);
    });
    palList.querySelector(".sel")?.scrollIntoView({ block: "nearest" });
  }
  function viewSession(s) {
    S.graphSid = s.session_id;
    $("convTitle").textContent = (s.query || s.session_id).slice(0, 70);
    fetchJSON(`/api/sessions/${encodeURIComponent(s.session_id)}/graph`, {}, 10000)
      .then((d) => { S.graph = d; renderDag(d.nodes || [], d.edges || []); gotoView("chat"); })
      .catch(() => toast("Graph load failed", "error"));
  }
  function clearChat() {
    if (!confirm("Clear all messages? This can't be undone.")) return;
    messages.replaceChildren();
    suggestions.style.display = "";
    welcome();
    toast("Conversation cleared", "success");
  }

  /* ═══ global keys ═══ */
  document.addEventListener("keydown", (e) => {
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === "k") { e.preventDefault(); palWrap.hidden ? openPalette() : closePalette(); }
    else if (mod && e.key.toLowerCase() === "l") { e.preventDefault(); input.focus(); }
    else if (e.key === "Escape") {
      if (!palWrap.hidden) closePalette();
      else if (!$("lightbox").hidden) closeLightbox();
      else closePanel();
    } else if (mod && e.key === "/") {
      e.preventDefault();
      openPanel("cost", $("costPanel").hidden);
      $$(".quick-actions .chip").forEach((c) => c.classList.remove("active"));
      $("costToggle").classList.add("active");
    }
  });

  /* ═══ boot ═══ */
  async function loadCost(quiet) {
    try {
      const d = await fetchJSON("/api/cost", {}, 15000);
      const rows = d.rows || [];
      S.costRows = rows;
      if (!rows.length) { if (!quiet) toast("No cost rows yet", "info"); return; }
      const t = d.totals || {};
      $("spendFoot").textContent = `${fmt$((t.dollars ?? t.usd))} · ${fmtN(t.calls)} calls`;
      const top = rows.slice().sort((a, b) => (b.calls || 0) - (a.calls || 0))[0];
      $("modelBadge").textContent = top ? `${top.provider || top.agent} · ${top.calls}` : "—";
    } catch { if (!quiet) toast("Cost load failed", "error"); }
  }
  $("convHint").textContent = S.convId;
  welcome();
  probe();
  setInterval(probe, 10000);
  loadSessionsQuiet().then(() => loadCost(true));
  setInterval(loadSessionsQuiet, 30000);
  setInterval(async () => {
    try {
      const d = await fetchJSON("/api/computer/approvals", {}, 12000, false);
      const n = (d.approvals || []).length;
      const badge = $("cuBadge");
      badge.hidden = !n;
      badge.textContent = String(n);
      if (S.lastPending >= 0 && n > S.lastPending) toast(`${n} computer action(s) need approval`, "warn", 6000);
      S.lastPending = n;
    } catch { /* badge is best-effort */ }
  }, 15000);
})();
