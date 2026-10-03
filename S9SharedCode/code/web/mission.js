/* Aria Console — mission-control event feed. Polls /api/events (3s,
   signature-guarded: no re-render when nothing changed). */
let level = "all", paused = false, lastSig = "", lastEvents = [];
let search = "";

const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
  }[c]));
}

async function health() {
  try {
    const r = await fetch("/api/health");
    const j = await r.json();
    const up = j && (j.agent === "ready" || j.ok);
    $("health-dot").className = "dot " + (up ? "g" : "r");
    $("health-text").textContent = up ? (j.gateway_up ? "agent + gateway" : "agent only") : "agent down";
  } catch (e) {
    $("health-dot").className = "dot r";
    $("health-text").textContent = "agent down";
  }
}

function sig(evs) {
  return evs.length + ":" + (evs.length ? evs[0].t + "|" + evs[evs.length - 1].t : "");
}

function render(evs) {
  const counts = { run: 0, sched: 0, tool: 0, info: 0, err: 0 };
  for (const e of lastEventsAll) counts[e.level] = (counts[e.level] || 0) + 1;
  $("tiles").innerHTML =
    '<div class="tile"><div class="t-k">RUNS</div><div class="t-v">' + counts.run + '</div><div class="t-s">recent sessions</div></div>' +
    '<div class="tile"><div class="t-k">SCHEDULED</div><div class="t-v">' + counts.sched + '</div><div class="t-s">jobs armed</div></div>' +
    '<div class="tile"><div class="t-k">TOOL CHATTER</div><div class="t-v">' + counts.tool + '</div><div class="t-s">server log lines</div></div>' +
    '<div class="tile"><div class="t-k">ERRORS</div><div class="t-v" style="color:' + (counts.err ? "#ff8f8f" : "inherit") + '">' + counts.err + '</div><div class="t-s">need attention</div></div>';
  $("ev-count").textContent = evs.length + " shown";
  $("p-showing").textContent = level + (search ? ' · "' + search + '"' : "");
  if (!evs.length) {
    $("feed").innerHTML = '<div class="hint" style="padding:20px 14px;">No events for this filter yet.</div>';
    return;
  }
  $("feed").innerHTML = evs.map((e) =>
    '<div class="log-line lv-' + e.level + '"><span class="lt">' + esc(e.iso) + '</span>' +
    '<span class="lsrc">' + esc(e.src) + '</span>' +
    '<span class="lmsg">' + esc(e.msg) + '</span></div>').join("");
}

let lastEventsAll = [];

function applyFilter() {
  const q = search.toLowerCase();
  const evs = lastEventsAll.filter((e) =>
    (level === "all" || e.level === level) &&
    (!q || (e.src + " " + e.msg).toLowerCase().includes(q)));
  render(evs);
}

async function tick() {
  if (paused) return;
  try {
    const r = await fetch("/api/events?limit=200");
    const j = await r.json();
    const evs = (j && j.events) || [];
    const s = sig(evs);
    if (s === lastSig) return; // nothing new — leave the DOM alone
    lastSig = s;
    lastEventsAll = evs;
    lastEvents = evs;
    applyFilter();
  } catch (e) { /* keep last frame on blips */ }
}

function togglePause() {
  paused = !paused;
  $("pause-btn").textContent = paused ? "resume" : "pause";
  $("live-pill").textContent = paused ? "○ paused" : "● live";
  $("live-pill").className = "pill " + (paused ? "muted" : "info");
}

document.querySelectorAll("#levels button").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll("#levels button").forEach((x) => x.classList.remove("on"));
    b.classList.add("on");
    level = b.dataset.lv;
    applyFilter();
  });
});
$("ev-search").addEventListener("input", (e) => { search = e.target.value.trim(); applyFilter(); });

health();
setInterval(health, 10000);
tick();
setInterval(tick, 3000);
