/* Aria — General AI Agent web client.
 *
 * Voice modality:
 *   - Speech-to-text  : Web Speech API (SpeechRecognition / webkitSpeechRecognition)
 *   - Text-to-speech  : Kokoro (server-side neural TTS). We POST the text to
 *                       /api/tts and play the returned WAV — far more natural
 *                       than the browser's built-in SpeechSynthesis.
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const messages = $("messages");
  const form = $("composer");
  const input = $("input");
  const micBtn = $("mic");
  const sendBtn = $("send");
  const stopBtn = $("stop");
  const ttsChk = $("tts");
  const voiceHint = $("voiceHint");
  const statusDot = $("statusDot");
  const statusText = $("statusText");
  const suggestions = $("suggestions");

  let ttsEnabled = false;
  let recognizing = false;
  let recognition = null;
  let busy = false;
  let abortCtrl = null;

  /* ── status probe ─────────────────────────────────────── */
  async function probe() {
    try {
      const r = await fetch("/api/health", { cache: "no-store" });
      const d = await r.json();
      if (d.agent === "ready") {
        statusDot.className = "dot ok";
        statusText.textContent = d.gateway_up ? "ready" : "ready (gateway starting)";
      } else {
        statusDot.className = "dot bad";
        statusText.textContent = "agent unavailable";
      }
    } catch {
      statusDot.className = "dot bad";
      statusText.textContent = "server offline";
    }
  }
  probe();
  setInterval(probe, 15000);

  /* ── speech recognition (input) ───────────────────────── */
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (SR) {
    recognition = new SR();
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.lang = "en-US";

    recognition.onresult = (e) => {
      let interim = "";
      let final = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const t = e.results[i][0].transcript;
        if (e.results[i].isFinal) final += t;
        else interim += t;
      }
      input.value = (final + interim).trim();
      autoGrow();
    };
    recognition.onend = () => {
      recognizing = false;
      micBtn.classList.remove("listening");
      voiceHint.textContent = "";
    };
    recognition.onerror = (e) => {
      recognizing = false;
      micBtn.classList.remove("listening");
      voiceHint.textContent =
        e.error === "not-allowed"
          ? "microphone permission denied"
          : "voice input ended";
    };
  } else {
    micBtn.disabled = true;
    voiceHint.textContent = "voice input needs Chrome/Edge";
  }

  micBtn.addEventListener("click", () => {
    if (!recognition) return;
    if (recognizing) {
      recognition.stop();
      return;
    }
    input.value = "";
    try {
      recognition.start();
      recognizing = true;
      micBtn.classList.add("listening");
      voiceHint.textContent = "listening… click 🎤 to stop";
    } catch {
      /* start() can throw if called too quickly after stop */
    }
  });

  /* ── text-to-speech (output) — Kokoro server-side ────── */
  let audioEl = null;
  ttsChk.addEventListener("change", () => {
    ttsEnabled = ttsChk.checked;
    if (!ttsEnabled && audioEl) audioEl.pause();
  });
  // Strip markdown to clean speech text so the TTS engine doesn't read out
  // asterisks, backticks, or [text](url) link syntax. Links become their
  // visible label; emphasis markers are dropped; list bullets are removed.
  function plainTextForSpeech(src) {
    if (!src) return "";
    return src
      .replace(/\[([^\]]+?)\]\([^)]*\)/g, "$1")   // [text](url) -> text
      .replace(/`([^`]+?)`/g, "$1")                // `code` -> code
      .replace(/\*\*([^*]+?)\*\*/g, "$1")          // **bold** -> bold
      .replace(/(^|[^*])\*([^*\n]+?)\*(?!\*)/g, "$1$2") // *italic* -> italic
      .replace(/^\s*[-*]\s+/gm, "")                // list bullets
      .replace(/\n{2,}/g, "\n")                    // collapse blank lines
      .trim();
  }
  async function speak(text) {
    if (!ttsEnabled || !text) return;
    try {
      const r = await fetch("/api/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, voice: "af_heart", speed: 1.0 }),
      });
      if (!r.ok) return;
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      if (audioEl) audioEl.pause();
      audioEl = new Audio(url);
      audioEl.play();
    } catch {
      /* TTS is best-effort; ignore failures */
    }
  }

  /* ── message rendering ────────────────────────────────── */
  function scrollDown() {
    messages.scrollTop = messages.scrollHeight;
  }

  function addUser(text) {
    const wrap = document.createElement("div");
    wrap.className = "msg user";
    const b = document.createElement("div");
    b.className = "bubble";
    b.textContent = text; // textContent => XSS-safe
    wrap.appendChild(b);
    messages.appendChild(wrap);
    scrollDown();
  }

  function addAssistant() {
    const wrap = document.createElement("div");
    wrap.className = "msg assistant";
    const b = document.createElement("div");
    b.className = "bubble";

    const thinking = document.createElement("div");
    thinking.className = "thinking";
    const head = document.createElement("div");
    head.className = "thinking-head";
    head.innerHTML = '<span class="chev">▾</span><span>Agent reasoning</span>';
    const body = document.createElement("pre");
    body.className = "thinking-body";
    thinking.appendChild(head);
    thinking.appendChild(body);
    head.addEventListener("click", () => thinking.classList.toggle("collapsed"));

    const answer = document.createElement("div");
    answer.className = "answer";
    answer.innerHTML = '<span class="typing"><span></span><span></span><span></span></span>';

    const meta = document.createElement("div");
    meta.className = "meta";
    meta.style.display = "none";

    const actions = document.createElement("div");
    actions.className = "msg-actions";
    const copyBtn = document.createElement("button");
    copyBtn.className = "copy-btn";
    copyBtn.textContent = "Copy";
    copyBtn.type = "button";
    copyBtn.onclick = () => {
      navigator.clipboard.writeText(answer.textContent).then(() => {
        copyBtn.textContent = "Copied!";
        setTimeout(() => (copyBtn.textContent = "Copy"), 1500);
      });
    };
    actions.appendChild(copyBtn);

    b.appendChild(thinking);
    b.appendChild(answer);
    b.appendChild(meta);
    b.appendChild(actions);
    wrap.appendChild(b);
    messages.appendChild(wrap);
    scrollDown();
    return { thinking, body, answer, meta };
  }

  function appendThinking(pre, text) {
    pre.textContent += text;
    scrollDown();
  }

  /* ── safe markdown → HTML ───────────────────────────────
   * The formatter returns plain text that may contain a limited markdown
   * subset (**bold**, *italic*, `code`, [text](url) links, - lists). We
   * render it as real HTML so links are clickable and emphasis shows.
   * Safety: escape ALL html first, then apply only the whitelisted tags
   * below. Link hrefs are restricted to http/https/mailto so a crafted
   * answer can't inject javascript: URLs. */
  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function renderMarkdown(src) {
    if (!src) return "";
    const lines = escapeHtml(src).split("\n");
    const out = [];
    let inList = false;
    const closeList = () => { if (inList) { out.push("</ul>"); inList = false; } };

    const inline = (t) => t
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*(?!\s)([^*\n]+?)\*(?!\*)/g, "$1<em>$2</em>")
      .replace(/`([^`]+?)`/g, "<code>$1</code>")
      // NOTE: group 1 = link TEXT, group 2 = URL. Put the URL in href and
      // the text in the label, otherwise the label shows the raw URL and
      // the href resolves the text as a relative path (localhost:8500/...).
      .replace(/\[([^\]]+?)\]\((https?:\/\/[^\s)]+|mailto:[^\s)]+)\)/g,
                '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
      // safety net: drop any orphaned `**` emphasis markers the model left
      // unbalanced, so raw asterisks never leak into the rendered answer.
      .replace(/\*\*/g, "");

    for (const raw of lines) {
      const m = raw.match(/^\s*[-*]\s+(.*)$/);
      if (m) {
        if (!inList) { out.push("<ul>"); inList = true; }
        out.push("<li>" + inline(m[1]) + "</li>");
      } else {
        closeList();
        if (raw.trim() === "") continue;
        out.push("<p>" + inline(raw) + "</p>");
      }
    }
    closeList();
    return out.join("");
  }

  /* ── send / stream ────────────────────────────────────── */
  async function send(query) {
    if (busy) return;
    busy = true;
    sendBtn.disabled = true;
    stopBtn.hidden = false;
    suggestions.style.display = "none";
    addUser(query);
    const ui = addAssistant();
    abortCtrl = new AbortController();

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query }),
        signal: abortCtrl.signal,
      });
      if (!res.ok) {
        ui.answer.textContent = "Request failed (" + res.status + ").";
        return;
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const chunk = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          if (!chunk.startsWith("data: ")) continue;
          let data;
          try {
            data = JSON.parse(chunk.slice(6));
          } catch {
            continue;
          }
          if (data.type === "log") {
            ui.thinking.classList.remove("collapsed");
            appendThinking(ui.body, data.text);
          } else if (data.type === "meta") {
            // Total wall-clock time for the whole response, sent just before
            // the `done` frame. Show it as a small footer under the answer.
            const s = data.elapsed_s;
            if (typeof s === "number") {
              const mins = Math.floor(s / 60);
              const secs = (s % 60).toFixed(1);
              const txt = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
              ui.meta.textContent = `⏱ Total time: ${txt}`;
              ui.meta.style.display = "block";
            }
          } else if (data.type === "done") {
            ui.answer.innerHTML = renderMarkdown(data.answer) || "<p>(no answer)</p>";
            speak(plainTextForSpeech(data.answer) || "");
            scrollDown();
          } else if (data.type === "error") {
            ui.answer.textContent = "Error: " + data.text;
          }
        }
      }
    } catch (err) {
      if (err.name === "AbortError") {
        ui.answer.textContent = "⏹ Stopped.";
      } else {
        ui.answer.textContent = "Connection error: " + err.message;
      }
    } finally {
      busy = false;
      sendBtn.disabled = false;
      stopBtn.hidden = true;
      abortCtrl = null;
      input.focus();
    }
  }

  stopBtn.addEventListener("click", () => {
    if (abortCtrl) abortCtrl.abort();
  });

  suggestions.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const q = chip.getAttribute("data-q");
      if (q && !busy) send(q);
    });
  });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    input.value = "";
    autoGrow();
    send(q);
  });

  /* ── textarea UX ──────────────────────────────────────── */
  function autoGrow() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 160) + "px";
  }
  input.addEventListener("input", autoGrow);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form.requestSubmit();
    }
  });

  /* ── welcome ─────────────────────────────────────────── */
  addUser("");
  const welcome = addAssistant();
  welcome.thinking.classList.add("collapsed");
  welcome.answer.textContent =
    "Hi, I'm Aria — a general AI agent. Ask me to research a topic, " +
    "summarise a page, write and run code, browse the web, or just chat. " +
    "Tap 🎤 to talk to me.";
  // hide the empty user bubble's empty text
  messages.querySelector(".msg.user .bubble").textContent = "";

  /* ── computer-use approval panel (internal, ?cu=1 only) ── */
  const cuEnabled = new URLSearchParams(location.search).has("cu");
  const cuPanel = $("cuPanel");
  const cuList = $("cuList");
  const cuCap = $("cuCap");

  if (cuEnabled) {
    cuPanel.hidden = false;
    async function refreshCu() {
      try {
        const r = await fetch("/api/computer/approvals", { cache: "no-store" });
        const d = await r.json();
        const caps = d.capabilities || {};
        cuCap.textContent = caps.daemon_running
          ? `daemon up · AX ${caps.ax_ok ? "ok" : "n/a"}`
          : "daemon offline";
        cuList.innerHTML = "";
        for (const a of d.approvals || []) {
          const li = document.createElement("li");
          li.className = "cu-item";
          const label = document.createElement("div");
          label.className = "cu-label";
          label.textContent = `${a.action} ${JSON.stringify(a.params)}`;
          const row = document.createElement("div");
          row.className = "cu-actions";
          const ok = document.createElement("button");
          ok.textContent = "Approve";
          ok.className = "cu-approve";
          ok.onclick = () => resolveCu(a.id, true);
          const no = document.createElement("button");
          no.textContent = "Reject";
          no.className = "cu-reject";
          no.onclick = () => resolveCu(a.id, false);
          row.append(ok, no);
          li.append(label, row);
          cuList.append(li);
        }
        if (!(d.approvals || []).length) {
          const li = document.createElement("li");
          li.className = "cu-empty";
          li.textContent = "No pending approvals.";
          cuList.append(li);
        }
      } catch {
        cuCap.textContent = "panel error";
      }
    }
    async function resolveCu(id, approve) {
      await fetch(`/api/computer/approvals/${id}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ approve }),
      });
      refreshCu();
    }
    refreshCu();
    setInterval(refreshCu, 5000);
  }

  /* ── computer-use replay viewer (internal, ?cu=1 only) ── */
  const replayPanel = $("replayPanel");
  const replayList = $("replayList");
  const replayDetail = $("replayDetail");
  const replayRefresh = $("replayRefresh");

  if (cuEnabled) {
    replayPanel.hidden = false;

    async function loadRuns() {
      try {
        const r = await fetch("/api/computer/runs", { cache: "no-store" });
        const d = await r.json();
        replayList.innerHTML = "";
        for (const run of d.runs || []) {
          const li = document.createElement("li");
          li.className = "replay-item";
          const label = document.createElement("div");
          label.className = "replay-label";
          label.textContent = `${run.id} · ${run.turns} turns`;
          const row = document.createElement("div");
          row.className = "replay-actions";
          const view = document.createElement("button");
          view.textContent = "View";
          view.className = "replay-view";
          view.onclick = () => showRun(run.id);
          const replay = document.createElement("button");
          replay.textContent = "Replay";
          replay.className = "replay-run";
          replay.onclick = () => doReplay(run.id);
          row.append(view, replay);
          li.append(label, row);
          replayList.append(li);
        }
        if (!(d.runs || []).length) {
          const li = document.createElement("li");
          li.className = "replay-empty";
          li.textContent = "No recorded runs yet.";
          replayList.append(li);
        }
      } catch {
        replayList.innerHTML = '<li class="replay-empty">panel error</li>';
      }
    }

    async function showRun(id) {
      try {
        const r = await fetch(`/api/computer/runs/${id}`, { cache: "no-store" });
        const d = await r.json();
        if (d.error) {
          replayDetail.hidden = false;
          replayDetail.textContent = d.error;
          return;
        }
        replayDetail.hidden = false;
        replayDetail.innerHTML = "";
        const h = document.createElement("h3");
        h.textContent = `Run ${id}`;
        replayDetail.append(h);
        for (const turn of d.turns || []) {
          const t = document.createElement("div");
          t.className = "replay-turn";
          t.textContent = `Turn ${turn.turn}: ${turn.calls.length} action(s)`;
          replayDetail.append(t);
          for (const call of turn.calls || []) {
            const c = document.createElement("div");
            c.className = "replay-call";
            c.textContent = `${call.tool || "?"} ${JSON.stringify(call.args || {})}`;
            replayDetail.append(c);
          }
        }
      } catch {
        replayDetail.hidden = false;
        replayDetail.textContent = "failed to load run";
      }
    }

    async function doReplay(id) {
      try {
        const r = await fetch(`/api/computer/replay/${id}`, {
          method: "POST",
          headers: { "content-type": "application/json" },
        });
        const d = await r.json();
        replayDetail.hidden = false;
        replayDetail.textContent = d.status === "ok"
          ? "Replay started successfully."
          : `Replay failed: ${d.message || "unknown error"}`;
      } catch {
        replayDetail.hidden = false;
        replayDetail.textContent = "replay request failed";
      }
    }

    replayRefresh.onclick = loadRuns;
    loadRuns();
    setInterval(loadRuns, 10000);
  }
})();
