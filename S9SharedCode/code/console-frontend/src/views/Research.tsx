import { useCallback, useEffect, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { Search, FlaskConical } from 'lucide-react';
import { isTerminal, isSuccess, renderMarkdown, runStatusFromNodes } from '../components/markdown';
import { Rail, TopBar, Pill, useHealth, SkipLink } from '../components/ui';
import { useMainId, useActivePoll } from '../components/KeepAlive';
import { DocumentViewer } from './DocSetup';
import { api, headers, displayTopic, ago, CONF, type GraphPayload, type GraphNode, type SessionSummary } from '../api';

type Depth = 'quick' | 'standard' | 'deep';
type CanvasView = 'graph' | 'report' | 'log';

/** Pull readable markdown out of a formatter node's stored output
 *  ({final_answer} today; tolerant of older shapes). */
function extractReport(out: unknown): string {
  if (typeof out === 'string') return unwrapAnswer(out);
  if (out && typeof out === 'object') {
    const o = out as Record<string, unknown>;
    for (const k of ['final_answer', 'report', 'markdown', 'content', 'text', 'answer']) {
      if (typeof o[k] === 'string' && (o[k] as string).trim()) return unwrapAnswer(o[k] as string);
    }
  }
  return '';
}

/** Recover plain markdown from a model reply that still wears its
 *  transport wrapper. Three shapes occur in practice, all observed live:
 *    1. fenced:        ```json\n{"final_answer": "…"}\n```
 *    2. closed object:  {"final_answer": "…"}
 *    3. UNTERMINATED:   {"final_answer": "…      (no closing brace)
 *  Left alone, shape 1 made renderMarkdown treat the ENTIRE report as one
 *  code block — headings, lists and links all vanished — and shape 3
 *  printed raw JSON as the report body. The server strips this on the
 *  sectioned path (`skills._strip_answer_envelope`) but a single-call
 *  formatter or an older stored run still ships the wrapper, so the
 *  client normalises too. Belt and braces: the user must never see JSON. */
function unwrapAnswer(s: string): string {
  let t = (s || '').trim();
  if (!t) return '';
  // Strip the opening and closing fences INDEPENDENTLY. The old regex
  // required both, so an envelope whose closing fence was missing (a stored
  // 4,597-char `final_answer` starting "```json" and ending mid-string) fell
  // through every branch: the fence regex missed, `startsWith('{')` was false
  // because the text began with backticks, and the bare-fence regex missed
  // too - so the whole report rendered as one code block of raw JSON, which
  // is precisely what this function exists to prevent.
  t = t.replace(/^```(?:json|markdown|md)?[ \t]*\r?\n?/i, '');
  t = t.replace(/```\s*$/, '');
  t = t.trim();
  if (t.startsWith('{')) {
    try {
      const obj = JSON.parse(t) as Record<string, unknown>;
      for (const k of ['final_answer', 'answer', 'report', 'text', 'content', 'markdown']) {
        const v = obj?.[k];
        if (typeof v === 'string' && v.trim()) return v.trim();
      }
    } catch {
      // Unterminated object: take everything after the first colon.
      const m = /^\{\s*"(?:final_answer|answer|report|text|content|markdown)"\s*:\s*"([\s\S]*)$/.exec(t);
      if (m) {
        let body = m[1].replace(/["'}\s,]+$/, '');
        // The value still carries escaped newlines from the JSON string.
        body = body.replace(/\\n/g, '\n').replace(/\\t/g, '\t').replace(/\\"/g, '"');
        if (body.trim()) return body.trim();
      }
      return '';
    }
  }
  // A bare fence (no envelope) around real markdown.
  const bare = /^```(?:markdown|md)?[ \t]*\r?\n([\s\S]*?)\r?\n?```\s*$/i.exec(t);
  if (bare) return bare[1].trim();
  return t;
}

export default function Research() {
  const mainId = useMainId();
  const gwUp = useHealth();
  const [topics, setTopics] = useState<SessionSummary[]>([]);
  const [topicsError, setTopicsError] = useState('');
  /** false until the first topics read settles, so an in-flight load is not
   *  rendered as "No research yet" (a false empty). */
  const [topicsLoaded, setTopicsLoaded] = useState(false);
  const [filter, setFilter] = useState('');
  const [brief, setBrief] = useState('');
  const [depth, setDepth] = useState<Depth>('standard');
  const [topic, setTopic] = useState('');
  const [sid, setSid] = useState('');
  const [graph, setGraph] = useState<GraphPayload | null>(null);
  const [running, setRunning] = useState(false);
  const [status, setStatus] = useState<'idle' | 'running' | 'done' | 'error'>('idle');
  const [note, setNote] = useState('');
  const [answer, setAnswer] = useState('');
const [files, setFiles] = useState<{ artifact: string; filename: string; format: string }[]>([]);
const [viewFile, setViewFile] = useState<string | null>(null);
  const [view, setView] = useState<CanvasView>('report');
  /* The conversation, oldest first. A page-level `answer` could only ever hold
     ONE answer, which is why a follow-up used to overwrite the question it was
     following up on. Each turn carries its own question, answer and session. */
  const [turns, setTurns] = useState<{
    id: string; q: string; a: string; sid: string; done: boolean; ts: number;
  }[]>([]);
  /* Live reasoning log. The server streams a `log` frame per orchestrator
     stdout line, and this view used to DISCARD them all — so a five-minute
     run showed a static canvas and a heartbeat, with no sense of what the
     agent was actually doing. Kept capped; auto-followed while live. */
  const [logs, setLogs] = useState<string[]>([]);
  const [logFollow, setLogFollow] = useState(true);
  const logBoxRef = useRef<HTMLDivElement | null>(null);
  const pollRef = useRef<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const reqRef = useRef(0);
  /* Which view the LIVE stream is allowed to write into. Bumped whenever the
     user switches topic or starts a new one, so the run in progress keeps
     streaming (and keeps its ledger entry) without painting its answer into a
     topic it does not belong to. Without this, opening another topic mid-run
     left the old stream writing turns and clearing `running` under the new
     topic — the pane said "Working on the last question…" with no run behind
     it. */
  const viewGenRef = useRef(0);
  const t0 = useRef(0);
  // Set by the Stop button so the abort path keeps "stopped." instead of
  // overwriting it with the generic "cancelled." a moment later.
  const stoppedRef = useRef(false);
  // Focus handoff for the Start/Stop pair: both buttons stay
  // mounted and the inactive one is disabled, so clicking one
  // disables it — focus moves to the button that just became the
  // live action instead of being thrown back to <body>.
  const stopBtnRef = useRef<HTMLButtonElement | null>(null);
  const startBtnRef = useRef<HTMLButtonElement | null>(null);
  // Post-stop catch-up poll (see stop()): the server skips the remaining
  // nodes a moment AFTER the cancel lands, so the canvas needs a few extra
  // fetches to show the partial graph instead of the pre-cancel frame.
  const catchUpRef = useRef<number | null>(null);
  // Conversation continuity across runs on this page (see the `started`
  // branch in start()).
  const cidRef = useRef('');
  const loc = useLocation();
  // Runs' Inspector "Run again" hands the original query over as state, so
  // re-running a run from its own graph no longer lands on an empty box.
  useEffect(() => {
    const b = (loc.state as { brief?: string } | null)?.brief;
    if (typeof b === 'string' && b.trim()) setBrief(b.trim());
  }, [loc.state]);
  // Monotonic counter so a slow graph response can never overwrite a newer
  // one, and so post-unmount writes are dropped.
  const pollSeq = useRef(0);
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; pollSeq.current++; }, []);

  const refreshTopics = useCallback(async () => {
    try {
        const d = await api.sessions(50);
        if (!aliveRef.current) return;
        /* Research runs only. An authoring run is in the same session store,
           so this list used to show documents being built on the Authoring
           page as if they were research topics - and the research sidebar
           offered no way to open one. `run_kind` is what the caller declared
           when it started the run (persisted at run start), not a guess from
           the skills a run happened to emit; the old guessed filter also hid
           a run whose query was a plain topic (it looked for the words
           "research agent" that only the retired brief contained), so a
           just-started topic vanished from the sidebar. */
        setTopics((d.sessions || []).filter((s) => {
          if (s.run_kind === 'authoring' || s.run_kind === 'chat') return false;
          /* A directory with neither a question nor a node is not a topic:
             test fixtures and runs that died before their setup wrote only
             `run_status.json`, and listing them put a screenful of
             "(untitled) · 0 nodes" above the real topics. */
          return !!((s.query || '').trim() || (s.nodes || 0) > 0);
        }));
      setTopicsError('');
    } catch (e) {
      if (!aliveRef.current) return;
      setTopicsError(e instanceof Error ? e.message : String(e));
    } finally {
      if (aliveRef.current) setTopicsLoaded(true);
    }
  }, []);

  useActivePoll(refreshTopics, CONF.pollSlowMs);

  const stopPoll = () => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
  };

  const stopCatchUp = () => {
    if (catchUpRef.current) { clearInterval(catchUpRef.current); catchUpRef.current = null; }
  };

  const abortRun = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    stopPoll();
    // The post-stop catch-up must die here too: openTopic() routes
    // through abortRun, and a catch-up left running keeps polling
    // the OLD session and painting its partial graph over the topic
    // the user just opened.
    stopCatchUp();
  };

  // Unmount (or navigation away) mid-run: kill the SSE stream, the graph
  // poll AND the post-stop catch-up — previously the first two leaked forever.
  useEffect(() => () => { abortRun(); stopCatchUp(); }, []);

  /* Log auto-follow: while live (or re-enabled), each new batch of lines
     pins the scrollback to the bottom — but only if the user hasn't
     scrolled up to read, same discipline as the Chat transcript. */
  useEffect(() => {
    const el = logBoxRef.current;
    if (el && logFollow) el.scrollTop = el.scrollHeight;
  }, [logs, logFollow]);

  const poll = useCallback(async (id: string) => {
    /* Sequence guard: the fast poll (2s) can be slower than its own
       interval on a large graph, so without this an older frame can land
       after a newer one and the canvas visibly steps backwards. */
    const seq = ++pollSeq.current;
    try {
      const g = await api.graph(id);
      if (seq !== pollSeq.current) return null;
      setGraph(g);
      return g;
    } catch {
      return null; // keep last frame
    }
  }, []);

  const [dlErr, setDlErr] = useState('');

  /* A top-level navigation CANNOT send X-Aria-Token, so the Get control used to
     be a bare <a href> that returned 403 on every click: the button existed
     and did nothing. Measured on the same session and URL — 403 via
     navigation, 200 application/pdf via fetch with the header. Fetch the bytes
     and save from a blob, the way Authoring does. */
  const downloadArtifact = useCallback(
    async (artifact: string, filename: string) => {
      setDlErr('');
      try {
        const r = await fetch(
          `/api/artifact/${encodeURIComponent(artifact)}?download=1`,
          { headers: headers() },
        );
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const blob = await r.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename || 'document';
        a.click();
        setTimeout(() => URL.revokeObjectURL(url), 10_000);
      } catch (e) {
        setDlErr(
          `could not download ${filename || 'the file'}: ${(e as Error)?.message || e}`,
        );
      }
    },
    [],
  );

  /* Clear the pane back to an empty brief without killing a run in flight, the
     same thing Chat's "Start new chat" does. Anything a run in progress
     streams afterwards is dropped by the reqRef bump, and the run keeps its
     place in the list. */
  const startNew = () => {
    /* Detach whatever is streaming into this pane. A run already in flight is
       NOT cancelled - it keeps its place in the sidebar and its ledger entry -
       but the view must stop claiming to be running it: `running` stayed true
       here, so "New topic" landed on a disabled composer reading "Working on
       the last question…" with no run behind it. */
    viewGenRef.current++;
    reqRef.current++;
    stopPoll();
    stopCatchUp();
    if (!running) abortRun();
    setRunning(false);
    cidRef.current = '';
    setSid('');
    setTopic('');
    /* Clear the brief too. Opening a topic seeds the composer with its title,
       so without this "New topic" dropped the user into a fresh pane with the
       PREVIOUS topic already typed in it - and pressing Research would have run
       that old question. */
    setBrief('');
    setAnswer('');
    setTurns([]);
    setLogs([]);
    setFiles([]);
    setGraph(null);
    setNote('');
    setView('report');
    setStatus('idle');
  };

  const openTopic = async (s: SessionSummary) => {
    /* Switching topics used to be REFUSED while a run was in flight:
       "stop or wait for the current run first." So clicking another topic did
       nothing at all and the pane kept showing the old report beside the new
       topic's name in the sidebar - the section looked like it was showing the
       wrong document. A run in flight is not a reason to trap the user on one
       topic; the running run keeps its own badge in the list. */
    const req = ++reqRef.current;
    /* `reqRef` already invalidates the live stream's updates, so the running
       run keeps going on the server without being able to write into this
       view. Aborting it here would KILL a paid run because the user clicked
       another topic in the list - which is why the switch used to be blocked
       outright, and why the pane then showed the old report under the new
       topic's name. */
    viewGenRef.current++;
    stopPoll();
    stopCatchUp();
    if (running) setNote('This topic is open. The run in progress is still going — it keeps its place in the list.');
    else abortRun();
    /* This pane is no longer the running run's pane. Leaving `running` true
       disabled the composer and showed "working" for a topic that had already
       answered. */
    setRunning(false);
    setSid(s.session_id);
    setTopic(displayTopic(s));
    // Only seed the brief when the box is empty. Overwriting whatever the
    // user had typed meant that peeking at a past report silently replaced
    // their brief with a one-line title, and "Research this topic" would
    // then launch a run built from that title.
    setBrief((prev) => prev || displayTopic(s));
    setAnswer('');
    setLogs([]);
    setFiles([]);
    setView('report');
    setTurns([]);
    setStatus('idle');
    setNote('');
    /* Seed the conversation id from the run being reopened. Without it a
       follow-up had nowhere to attach: `cidRef` stayed empty, the composer
       never appeared, and a run finished in an earlier session could not be
       continued at all — the one thing the follow-up box exists for. */
    cidRef.current = s.conversation_id || '';
    /* The Files row was only ever populated from the live `done` frame, so a
       run you opened again from the sidebar lost its files even though the
       server still had them. Read the produced record. */
    void api.runProduced(s.session_id).then((snap) => {
      if (reqRef.current !== req) return;
      if (snap?.files?.length) setFiles(snap.files);
      /* Reopening a finished run must tell the same story the live stream
         told. The "this answer is incomplete" warning and the operator's stop
         notice used to arrive once, in the SSE frame, and were gone after a
         reload - so a degraded report looked complete the second time you
         opened it. Both are persisted server-side now. */
      if (snap?.degraded) {
        const what = (snap.incomplete_detail || []).join('; ');
        setNote(`This answer is incomplete — ${what || 'part of the question went unanswered'}. Treat the missing parts as unknown rather than settled.`);
      } else if (snap?.stopped) {
        setNote('Stopped before the run finished — the report below may be partial.');
      }
    }).catch(() => { /* no files, or unreadable: the row simply stays empty */ });
    try {
      const g = await api.graph(s.session_id);
      if (reqRef.current !== req) return;
      setGraph(g);
      // An empty node list is the same "no graph" as a null payload
      // (the server returns 200 + empty nodes for a session whose
      // graph was never written) — the note must cover both.
      if (!g || !g.nodes.length) {
        setNote('that run has no graph — it may not have started.');
        return;
      }
      // History topics never streamed a done-frame, so load the stored
      // formatter output — otherwise past reports are invisible.
      const fmts = (g?.nodes || []).filter(
        (n) => /format/i.test(n.skill || '') && isTerminal(n.status),
      );
      const last = fmts[fmts.length - 1];
      if (last) {
        try {
          const nd = await api.node(s.session_id, last.id);
          if (reqRef.current === req) setAnswer(extractReport(nd?.result?.output));
        } catch { /* report stays empty */ }
      }
    } catch (e) {
      /* Surface it. A silent null left the canvas showing "The pipeline
         appears here", which is exactly what an empty topic looks like — a
         failed read was indistinguishable from a topic that produced nothing. */
      if (reqRef.current === req) {
        setGraph(null);
        setNote(`couldn't load that topic (${e instanceof Error ? e.message : String(e)})`);
      }
    }
  };

  const start = async () => {
    const t = brief.trim();
    if (!t || running) {
      if (!t) setNote('type a topic first.');
      return;
    }
    // A new brief discards the old thread and its cid, so the next run is a
    // fresh conversation. Follow-ups go through `send()` instead and keep it.
    cidRef.current = '';
    setTurns([]);
    setBrief('');
    return send(t, { first: true });
  };

  /** Send one turn, appending it to the transcript.
   *
   *  `first` distinguishes a new brief from a follow-up. A follow-up REUSES
   *  `cidRef`, which is what makes it a follow-up: the server maps that
   *  conversation id to the same session, so the DAG resumes with the previous
   *  question in context. The old code reset every run's state and minted a
   *  fresh id, so "and what about Europe?" started a second, context-free
   *  research run and replaced the first answer on screen.
   *
   *  Idempotency is applied to the FIRST turn only. Keying every turn on the
   *  question text would be right for a topic, but a follow-up is a different
   *  question in the same conversation - and `research:<depth>:<text>` keyed
   *  off the brief would collapse a legitimate follow-up into the run before
   *  it and silently return that run's answer instead.
   */
  const send = async (question: string, opts?: { first?: boolean }): Promise<void> => {
    const first = opts?.first === true;
    if (running) {
      if (!first) setNote('wait for the current run to finish.');
      return;
    }
    const q = question.trim();
    if (!q) return;
    abortRun();
    stopCatchUp();
    const ctl = new AbortController();
    abortRef.current = ctl;
    const myGen = ++viewGenRef.current;
    setRunning(true);
    /* Keep the composer focused. Moving focus to Stop after 600ms meant a
       second Enter (the user correcting a typo, or just impatient) stopped the
       run they had only just started. Stop is still reachable by Tab. */
    setStatus('running');
    /* Mint the conversation id before the request, not when `started` comes
       back: Stop before the first frame then has a server-side identifier to
       cancel with. The server uses the id we send, so `started` agrees. */
    if (!cidRef.current) {
      cidRef.current = `c-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    }
    stoppedRef.current = false;
    if (first) {
      setTopic(q);
      setAnswer('');
      setLogs([]);
      setLogFollow(true);
      setGraph(null);
      setSid('');
    }
    setNote(first ? 'researching…' : 'following up…');
    t0.current = Date.now();
    const turnId = `turn-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
    setTurns((prev) => [...prev, {
      id: turnId, q, a: '', sid: '', nodes: 0, done: false, ts: Date.now(),
    }]);
    try {
      /* `q` is the TOPIC. It used to be wrapped in `briefFor(q, depth)` - a
         multi-role instruction ("You are a research agent... deliver a final
         report as Markdown"). The DAG hands the run query to EVERY node, so
         each researcher read it as its own task and wrote a report instead of
         the JSON the pipeline needs: "researcher returned no usable JSON",
         after up to 300s, on every run. Depth now travels as a planner-only
         parameter. */
      for await (const ev of api.chat(q, cidRef.current, ctl.signal,
                                     { research: first, depth,
                                       runKind: 'research',
                                       idempotencyKey: first ? `research:${depth}:${q}` : undefined })) {
        /* Detached: another topic was opened (or a new one started) while this
           run was streaming. Keep draining so the stream and its ledger entry
           survive, but write nothing into the pane that no longer owns it. */
        if (ctl.signal.aborted) break;
        if (viewGenRef.current !== myGen) continue;
        if (ev.type === 'started') {
          const esid = (ev as { session_id?: string }).session_id;
          /* The server also returns a conversation_id. It was dropped here,
             so every run minted a fresh conversation: no memory continuity,
             no per-thread cost ledger, and no way to reattach to a live run
             after a reload — which is what resolve_session exists for. */
          const ecid = (ev as { conversation_id?: string }).conversation_id;
          if (ecid) cidRef.current = ecid;
          if (!esid) continue;
          setSid(esid);
          setTurns((prev) => prev.map((x) => (x.id === turnId ? { ...x, sid: esid } : x)));
          pollRef.current = window.setInterval(() => poll(esid), CONF.pollFastMs);
          poll(esid);
          /* The server writes run_meta.json as the run starts, so this is when
             a brand-new topic becomes a real session. Refreshing now puts it in
             the sidebar without waiting a whole slow-poll interval. */
          refreshTopics();
        } else if (ev.type === 'log') {
          const text = String((ev as { text?: string }).text ?? '').slice(0, 500);
          if (text) {
            setLogs((prev) => {
              const next = prev.length >= 300 ? prev.slice(prev.length - 299) : prev;
              return [...next, text];
            });
          }
        } else if (ev.type === 'done') {
          stopPoll();
          const id = ((ev as { session_id?: string }).session_id || sid) as string;
          if (id) {
            try {
              const g = await api.graph(id);
              // A Stop during that fetch must not resurrect "done"
              // over the "stopped." state stop() already set.
              if (!ctl.signal.aborted) setGraph(g);
            } catch { /* keep */ }
          }
          if (ctl.signal.aborted) return; // stop() owns the final state now
          /* Files the run ACTUALLY produced. The `done` frame carries them and
             this view used to ignore that field, so a research run that really
             did render a PDF showed only a "Download .md" button built from the
             answer text: the user was told a file existed and had no way to
             open it. Prefer what the server says exists over what the prose
             claims. */
          const evFiles = ((ev as { files?: { artifact: string; filename: string; format: string }[] }).files) || [];
          setFiles(evFiles);
          if (id) {
            const snap = await api.runProduced(id).catch(() => null);
            if (snap && snap.files?.length) setFiles(snap.files);
          }
          // Normalise here too, not just on the history path: the live
          // `done` frame is the same model text and can arrive fenced or
          // wrapped, which used to render the whole report as one code
          // block or print raw JSON.
          const raw = (ev as { answer?: string }).answer;
          const finalText = typeof raw === 'string' ? unwrapAnswer(raw) : '';
          setAnswer(finalText);
          // Land the answer on ITS OWN turn, not as a single page-level blob:
          // this is what makes the page read as a conversation, and it is why
          // a follow-up no longer appears to replace the question above it.
          setTurns((prev) => prev.map((x) => (
            x.id === turnId
              ? { ...x, a: finalText || x.a, done: true }
              : x)));
          setStatus('done');
          setView('report'); // the finished report is the readable thing
          setNote(`done in ${Math.round((Date.now() - t0.current) / 1000)}s.`);
          setRunning(false);
          requestAnimationFrame(() => startBtnRef.current?.focus());
          refreshTopics();
        } else if (ev.type === 'error') {
          stopPoll();
          setStatus('error');
          setNote(String((ev as { text?: string }).text || 'error'));
          setTurns((prev) => prev.map((x) => (x.id === turnId ? { ...x, done: true } : x)));
          setRunning(false);
          requestAnimationFrame(() => startBtnRef.current?.focus());
        }
      }
      if (ctl.signal.aborted) {
        setStatus('idle');
        setNote(stoppedRef.current ? 'stopped.' : 'cancelled.');
        setRunning(false);
        requestAnimationFrame(() => startBtnRef.current?.focus());
      }
    } catch (e) {
      if (ctl.signal.aborted) {
        setStatus('idle');
        setNote(stoppedRef.current ? 'stopped.' : 'cancelled.');
      } else {
        setStatus('error');
        setNote(String((e as Error).message || e));
      }
        stopPoll();
        setRunning(false);
        requestAnimationFrame(() => startBtnRef.current?.focus());
      } finally {
      if (abortRef.current === ctl) abortRef.current = null;
      /* Safety net: every terminal path above clears `running`, but a path
         that returns early must not leave the composer disabled reading
         "Working on the last question…" after the answer has arrived. */
      if (viewGenRef.current === myGen) setRunning(false);
    }
  };

  const stop = async () => {
    // Aborting the client stream alone only stops LISTENING — the server run
    // kept going (and kept billing). Ask the agent to stop too. Send BOTH ids:
    // before `started` there is no session id yet, but the conversation id was
    // minted before the request and is enough for the server to find the run.
    const liveSid = sid;
    const liveCid = cidRef.current;
    const ids: { conversation_id?: string; session_id?: string } = {};
    if (liveCid) ids.conversation_id = liveCid;
    if (liveSid) ids.session_id = liveSid;
    const cancelling = (liveCid || liveSid)
      ? api.cancelRun(ids).catch(() => null)
      : Promise.resolve(null);
    stoppedRef.current = true;
    abortRun();
    setRunning(false);
    requestAnimationFrame(() => startBtnRef.current?.focus());
    setStatus('idle');
    setNote('stopped.');
    const d = await cancelling;
    // The graph poll just died with the stream, but the executor may not
    // have reached its cancel check yet (gateway warmup + memory read run
    // before the first node), so graph.json can appear SECONDS after the
    // cancel lands. Keep fetching until the partial graph is on disk and
    // every node is terminal - or until we give up after ~20s. If Stop beat
    // `started`, use the session id the cancel response resolved.
    const catchSid = liveSid || d?.session_id || '';
    if (catchSid) {
      stopCatchUp();
      let n = 0;
      const tick = async () => {
        const g = await poll(catchSid);
        const nodes = g?.nodes || [];
        const settled = nodes.length > 0 && nodes.every((nd) => isTerminal(nd.status));
        // Refresh the topic list once, at settle (or give-up) — not
        // every tick: 20 ticks of full session-list reloads for one
        // stop, most of which show nothing new.
        if (settled || ++n >= 20) {
          stopCatchUp();
          refreshTopics();
        }
      };
      catchUpRef.current = window.setInterval(() => { void tick(); }, CONF.pollCatchUpMs);
      void tick();
    }
  };

  const download = () => {
    if (!answer) return;
    const blob = new Blob([`# ${topic}\n\n${answer}`], { type: 'text/markdown' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `research-${Date.now()}.md`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  };

  const copy = () => {
    if (!answer) return;
    navigator.clipboard.writeText(`# ${topic}\n\n${answer}`).then(
      () => setNote('copied to clipboard.'),
      () => setNote('copy failed — select the report manually.'),
    );
  };

  /* The sidebar row for a run that has just started. The server persists the
     run's kind the moment the run begins, but its list is polled, so a user
     who submitted and then looked away could come back to a sidebar without
     their new topic. Deriving one row from the live run closes that gap
     without any extra state to clean up: as soon as the server's list contains
     the session, the derived row disappears. */
  const shownTopics = (() => {
    if (sid && topic && !topics.some((t) => t.session_id === sid)) {
      return [{
        session_id: sid,
        conversation_id: cidRef.current || undefined,
        query: topic,
        topic,
        kind: 'research',
        run_kind: 'research',
        has_files: files.length > 0,
        nodes: graph?.nodes.length || 0,
        status_counts: {},
        skills: [],
        updated: Date.now() / 1000,
        updated_ago: 0,
      } as unknown as SessionSummary, ...topics];
    }
    return topics;
  })();

  const vis = shownTopics.filter((s) =>
    !filter || displayTopic(s).toLowerCase().includes(filter.toLowerCase()),
  );

  /* Is this pane already a conversation? If so the composer asks a follow-up
     and keeps the conversation id; if not it starts a new topic. One composer
     for both is what makes the page read like Chat instead of "a question box
     AND a follow-up box AND a canvas". */
  const threadActive = !!sid || turns.length > 0;

  const submit = () => {
    const q = brief.trim();
    if (!q || running) return;
    if (threadActive) {
      setBrief('');
      void send(q);
    } else {
      void start();
    }
  };

  const statusPill =
    status === 'running' ? <Pill tone="info">running</Pill> :
    status === 'done' ? <Pill tone="ok">done</Pill> :
    status === 'error' ? <Pill tone="err">error</Pill> :
    /* "idle" is server vocabulary, not a word a user would use. The pill is a
       state readout, so say the state: nothing is running. */
    <Pill tone="muted">ready</Pill>;

  return (
    <div className="flex h-full flex-col lg:flex-row">
      <SkipLink />
      <Rail />
      <div className="flex w-full max-h-[34vh] flex-none flex-col border-b border-white/10 bg-[#0b0b0e] lg:max-h-none lg:w-[248px] lg:border-b-0 lg:border-r">
        <div className="flex items-center justify-between px-3.5 pb-2 pt-3.5 text-xs font-bold tracking-wide">
          Topics
          <span className="rounded-full border border-white/10 bg-white/5 px-2.5 py-0.5 text-[10.5px] font-bold text-zinc-400">{shownTopics.length}</span>
        </div>
        {/* Chat has "Start new chat"; Research had no way back to an empty
            brief except reloading, which is how a user ends up staring at the
            last report instead of the topic they meant to ask about. */}
        <div className="px-2.5 pb-2">
          <button
            onClick={startNew}
            title="Start a new research topic"
            className="mb-2 w-full rounded-md border border-white/15 bg-white/5 px-2.5 py-1.5 text-xs text-zinc-300 transition-colors hover:border-violet-400 hover:text-zinc-100"
          >
            + New topic
          </button>
        </div>
        <div className="px-2.5 pb-2">
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter…"
            aria-label="Filter topics"
            className="w-full rounded-md border border-white/10 bg-black/40 px-2.5 py-1.5 text-xs outline-none focus:border-violet-400"
          />
        </div>
        <div className="min-h-0 flex-1 space-y-2 overflow-y-auto px-2.5 pb-3">
          {topicsError && !!topics.length && (
            <div className="rounded-lg border border-amber-300/30 bg-amber-300/5 p-2.5 text-[11px] text-amber-200">
              refresh failed — showing last data.
              <button onClick={refreshTopics} className="ml-1.5 underline">retry</button>
            </div>
          )}
          {topicsError && !topics.length && (
            <div className="rounded-lg border border-red-400/30 bg-red-400/5 p-3 text-center text-xs text-red-200">
              couldn't load topics ({topicsError}). <button onClick={refreshTopics} className="underline">retry</button>
            </div>
          )}
          {!topicsError && !topicsLoaded && (
            <div className="px-1 py-6 text-center text-xs text-zinc-muted">loading topics…</div>
          )}
          {!topicsError && topicsLoaded && !vis.length && (
            <div className="py-8 text-center">
              <FlaskConical size={26} className="mx-auto mb-2 text-zinc-muted" />
              <div className="text-sm font-bold">No research yet</div>
              <div className="mx-auto mt-1 max-w-[180px] text-xs leading-relaxed text-zinc-muted">Your briefs land here.</div>
            </div>
          )}
          {vis.map((s) => (
            <button
              key={s.session_id}
              onClick={() => openTopic(s)}
              className={`block w-full rounded-[10px] border p-2.5 text-left ${
                s.session_id === sid ? 'border-violet-400/60 bg-violet-400/5' : 'border-white/10 bg-[#0e0e12] hover:border-white/25'
              }`}
            >
              <div className="truncate text-[12.5px] font-semibold">{displayTopic(s).slice(0, 90)}</div>
              <div className="mt-1 text-[11px] text-zinc-muted">{s.nodes || 0} node{s.nodes === 1 ? '' : 's'} · {ago(s.updated_ago)}</div>
            </button>
          ))}
        </div>
        <div className="hidden border-t border-white/10 px-3.5 py-2.5 text-[11px] text-zinc-muted lg:block">
          <div className="text-[10px] font-bold tracking-[0.12em] text-zinc-muted">SESSION</div>
          <div className="mt-0.5 truncate font-mono">{sid ? sid.slice(0, 12) + '…' : '—'}</div>
        </div>
      </div>

      <main id={mainId} tabIndex={-1} className="flex min-h-0 min-w-0 flex-1 flex-col outline-none">
        <TopBar crumb="Research">
          {statusPill}
          <div className="flex gap-1 rounded-lg border border-white/10 bg-white/5 p-[3px]" role="group" aria-label="Output view">
            {(['report', 'log'] as const).map((v) => (
              <button
                key={v}
                onClick={() => setView(v)}
                disabled={v === 'log' && !logs.length && !running}
                aria-pressed={view === v}
                className={`rounded px-3 py-1.5 text-[11.5px] capitalize disabled:opacity-40 ${view === v ? 'bg-violet-400/20 font-bold text-zinc-100' : 'text-zinc-400 hover:text-zinc-200'}`}
              >
                {v === 'report' ? 'answer' : 'log'}
              </button>
            ))}
          </div>
          <button onClick={download} disabled={!answer} aria-label="Download report as markdown" className="rounded-md border border-white/15 bg-white/5 px-3.5 py-1.5 text-xs hover:border-violet-400 disabled:opacity-45">Download .md</button>
          <button onClick={copy} disabled={!answer} aria-label="Copy report to clipboard" className="rounded-md border border-white/15 bg-white/5 px-3.5 py-1.5 text-xs hover:border-violet-400 disabled:opacity-45">Copy</button>
        </TopBar>

        {/* The body is the conversation, the way Chat's is: one full-height
            scroll region with the composer pinned below it. The old layout put
            a topic textarea at the top, a follow-up textarea at the bottom and
            a bordered canvas between them, which read as a form with a viewer
            rather than as a conversation. */}
        <div className="min-h-0 flex-1">
          {viewFile ? (
            <div className="docview-frame m-3.5">
              <div className="docview-bar">
                <span className="font-semibold">
                  {files.find((f) => f.artifact === viewFile)?.filename || viewFile}
                </span>
                <button onClick={() => setViewFile(null)} className="rounded-md border border-white/15 px-2 py-1 text-[11px] hover:border-violet-400">
                  Back to report
                </button>
              </div>
              <DocumentViewer
                source={{
                  kind: 'artifact',
                  artifact: viewFile,
                  format: files.find((f) => f.artifact === viewFile)?.format || '',
                  label: files.find((f) => f.artifact === viewFile)?.filename || viewFile,
                }}
              />
            </div>
          ) : view === 'log' ? (
            <div className="flex h-full min-h-0 flex-col">
              {graph && graph.nodes.length > 0 && (
                <RunSummary graph={graph} running={running} sid={sid} />
              )}
              <div className="min-h-0 flex-1">
                <LogView logs={logs} running={running} follow={logFollow} setFollow={setLogFollow} boxRef={logBoxRef} />
              </div>
            </div>
          ) : (
            <div className="h-full overflow-y-auto">
              <div className="mx-auto flex w-full max-w-[840px] flex-col gap-6 px-5 py-6">
                {/* Real files first. This row exists because research would say "I
                    created report.pdf" and offer only a markdown download - the file
                    was never reachable. Anything listed here came from the server's
                    produced-files record, not from the answer's prose. */}
                {files.length > 0 && (
                  <div className="flex flex-wrap items-center gap-2 rounded-[10px] border border-white/10 bg-[#0e0e12] p-2.5">
                    <span className="text-[11px] font-bold uppercase tracking-[0.12em] text-zinc-muted">
                      Files
                    </span>
                    {files.map((f) => (
                      <span key={f.artifact} className="flex items-center gap-1.5 rounded-md border border-white/10 bg-white/5 px-2 py-1 text-[11.5px]">
                        <Pill tone="muted">{f.format || 'file'}</Pill>
                        <span className="max-w-[220px] truncate text-zinc-200" title={f.filename}>
                          {f.filename}
                        </span>
                        <button
                          onClick={() => setViewFile(f.artifact)}
                          className="rounded border border-violet-400/50 px-1.5 py-0.5 text-[11px] text-violet-200 hover:bg-violet-400/20"
                        >
                          View
                        </button>
                        <button
                          type="button"
                          onClick={() => downloadArtifact(f.artifact, f.filename)}
                          className="rounded border border-white/15 px-1.5 py-0.5 text-[11px] text-zinc-300 hover:border-violet-400"
                        >
                          Get
                        </button>
                      </span>
                    ))}
                    {dlErr && (
                      <span role="alert" className="text-[11px] text-red-300">{dlErr}</span>
                    )}
                  </div>
                )}
                {!turns.length && !answer ? (
                  <div className="flex min-h-[50vh] flex-col items-center justify-center text-center">
                    <Search size={30} className="mb-2.5 text-zinc-muted" />
                    <div className="text-sm font-bold">
                      {running ? 'Starting the pipeline…' : 'Ask a topic to begin'}
                    </div>
                    <div className="mt-1 max-w-[380px] text-xs leading-relaxed text-zinc-muted">
                      {running
                        ? 'The agent is warming up the gateway and reading memory — the first node lands in a few seconds.'
                        : 'Aria breaks the question into parts, researches each, and writes one report here. The graph view of the run lives in Runs.'}
                    </div>
                  </div>
                ) : (
                  <>
                    {/* A run reopened from the sidebar has no in-memory turns,
                        but `openTopic` fetched its stored report into `answer`.
                        That report was fetched and then never rendered, so
                        reopening any past run showed an empty pane reading "Ask a
                        topic to begin" beneath a real answer that already existed
                        — as if the run had produced nothing. Show it. */}
                    {!turns.length && !!answer && (
                      <section aria-label="Report" className="flex flex-col gap-3">
                        <article className="prose-chat prose-report text-[14px] leading-[1.75] text-zinc-200">
                          <div dangerouslySetInnerHTML={{ __html: renderMarkdown(answer, 1, true) }} />
                        </article>
                        <div className="font-mono text-[10.5px] text-zinc-muted">
                          stored report{sid ? ` · ${sid.slice(0, 12)}…` : ''}
                        </div>
                      </section>
                    )}
                    {turns.map((t, i) => (
                      <section key={t.id} aria-label={`Turn ${i + 1}`} className="flex flex-col gap-3">
                        <div className="flex justify-end">
                          <p className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-violet-500/15 px-3.5 py-2.5 text-[13.5px] leading-relaxed text-zinc-100">
                            {t.q}
                          </p>
                        </div>
                        <article
                          aria-live="polite"
                          aria-busy={!t.done && running}
                          className="prose-chat prose-report text-[14px] leading-[1.75] text-zinc-200"
                        >
                          {t.a
                            ? <div dangerouslySetInnerHTML={{ __html: renderMarkdown(t.a, 1, true) }} />
                            : (
                              <p className="flex items-center gap-2 text-[13px] text-zinc-muted">
                                <FlaskConical size={13} className="animate-pulse" />
                                {running ? 'Researching…' : 'No answer produced.'}
                              </p>
                            )}
                        </article>
                        <div className="font-mono text-[10.5px] text-zinc-muted">
                          {t.done ? `turn ${i + 1} · answered` : `turn ${i + 1} · running`}
                          {t.sid ? ` · ${t.sid.slice(0, 12)}…` : ''}
                        </div>
                      </section>
                    ))}
                  </>
                )}
              </div>
            </div>
          )}
        </div>

        {/* One composer for both a new topic and a follow-up, pinned below the
            conversation exactly like Chat's. It replaces the old pair of
            textareas (one at the top, one at the bottom) that made the page
            read as a form wrapped around a viewer. */}
        <div className="flex-none border-t border-white/10 bg-[#0b0b0e] px-4 py-3">
          <form
            className="mx-auto flex w-full max-w-[840px] flex-col gap-2"
            onSubmit={(e) => { e.preventDefault(); submit(); }}
          >
            <label className="sr-only" htmlFor="research-composer">
              {threadActive ? 'Follow-up question' : 'Research topic'}
            </label>
            <textarea
              id="research-composer"
              value={brief}
              onChange={(e) => setBrief(e.target.value)}
              onKeyDown={(e) => {
                if (e.nativeEvent.isComposing || (e.nativeEvent as unknown as { keyCode?: number }).keyCode === 229) return;
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  submit();
                }
              }}
              placeholder={running
                ? 'Researching… type ahead and send when it lands'
                : threadActive
                  ? 'Ask a follow-up — the run keeps its context…'
                  : 'Research topic — e.g. solid-state battery supply chain: who leads, where are the bottlenecks'}
              rows={1}
              aria-label={threadActive ? 'Follow-up question' : 'Research topic'}
              className="max-h-40 min-h-[46px] w-full resize-y rounded-lg border border-white/10 bg-black/40 px-3 py-2.5 text-[13.5px] leading-relaxed text-zinc-100 outline-none focus:border-violet-400"
            />
            <div className="flex flex-wrap items-center gap-2">
              <div className="flex gap-1 rounded-lg border border-white/10 bg-white/5 p-[3px]" role="group" aria-label="Research depth">
                {(['quick', 'standard', 'deep'] as const).map((d) => (
                  <button
                    key={d}
                    type="button"
                    onClick={() => setDepth(d)}
                    disabled={running}
                    aria-pressed={depth === d}
                    className={`rounded px-3 py-1.5 text-[11.5px] disabled:opacity-50 ${depth === d ? 'bg-violet-400/20 font-bold text-zinc-100' : 'text-zinc-400 hover:text-zinc-200'}`}
                  >
                    {d}
                  </button>
                ))}
              </div>
              <span className="min-w-0 truncate text-[11px] text-zinc-muted" role="status">{note}</span>
              <span className="flex-1" />
              {/* Both buttons stay mounted; the inactive one is
                  disabled — never swapped out, because removing a
                  focused button from the DOM throws focus back to
                  <body>. start()/stop() (and the done/error paths)
                  hand focus to whichever button just became live. */}
              <button
                ref={stopBtnRef}
                type="button"
                onClick={stop}
                disabled={!running}
                aria-label="Stop the run"
                className="rounded-md border border-red-400/50 bg-red-400/10 px-4 py-2 text-xs font-bold text-red-200 hover:bg-red-400/20 disabled:opacity-40"
              >
                Stop
              </button>
              <button
                ref={startBtnRef}
                type="submit"
                disabled={running || !brief.trim()}
                aria-label={threadActive ? 'Send the follow-up' : 'Research this topic'}
                className="rounded-md bg-violet-400 px-4 py-2 text-xs font-bold text-[#0b0b0e] shadow-[0_0_16px_rgba(139,124,246,0.35)] hover:brightness-110 disabled:opacity-50"
              >
                {threadActive ? 'Send' : 'Research'}
              </button>
            </div>
          </form>
          <div className="mx-auto mt-2 flex max-w-[840px] flex-wrap items-center gap-x-3 text-[11px] text-zinc-muted">
            <span>pipeline runs on the agent DAG</span>
            <span aria-hidden="true">·</span>
            <span>graph view lives in Runs</span>
            <span aria-hidden="true">·</span>
            <span>gateway {gwUp ? 'up' : 'down'}</span>
            {sid && <span className="ml-auto truncate font-mono" title={sid}>{sid.slice(0, 12)}…</span>}
          </div>
        </div>
      </main>
    </div>
  );
}

function fmtDur(s?: number | null): string {
  if (s == null || !isFinite(s)) return '—';
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s - m * 60)}s`;
}

/** One-line run summary + duration bars, docked above the canvas. Span is
    min(started) → max(completed, now if live), straight from the graph
    rollup's own timestamps — no extra requests. The pill reflects the RUN
    (done/failed/live), not the page: showing "idle" for a finished run
    buried the outcome. */
function RunSummary({ graph, running, sid }: {
  graph: GraphPayload;
  running: boolean;
  sid: string;
}) {
  const starts = graph.nodes.map((n) => n.started_at).filter((t): t is number => typeof t === 'number');
  const ends = graph.nodes.map((n) => n.completed_at).filter((t): t is number => typeof t === 'number');
  const span = starts.length ? fmtDur((ends.length && !running ? Math.max(...ends) : Date.now() / 1000) - Math.min(...starts)) : null;
  /* `isTerminal` answers "is it over?" — a skipped or interrupted node is over
     but did not succeed. Using it for a green `done` is what made a cancelled
     run (planner+researcher complete, formatter skipped) satisfy
     `doneCount === nodes.length` and render as a success with an empty report.
     Six finished runs sat in the store that way. `runStatusFromNodes` is the
     shared rule; Runs.tsx uses the same one. */
  const okCount = graph.nodes.filter((n) => isSuccess(n.status)).length;
  const status = runStatusFromNodes(graph.nodes, running);
  const pill = <Pill tone={status.tone}>{status.label}</Pill>;
  return (
    <div className="border-b border-white/10 bg-[#0b0b0e]/80 px-4 py-2.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        {pill}
        {/* Node count and wall-clock span are the two numbers a run is
            actually judged by, and every other data view (Ledger, Documents,
            Scheduler, Runs) leads with 20px numerals via the shared `Stat`
            component. Research hand-rolled this strip at 11px monospace, which
            is why the page reads as denser and smaller than its siblings
            before a single node has been read. The labels stay small; the
            values do not. */}
        <span className="flex items-baseline gap-1.5">
          <span className="text-xl font-bold tabular-nums leading-none text-zinc-100">
            {okCount}/{graph.nodes.length}
          </span>
          <span className="text-[10px] font-bold uppercase tracking-[0.12em] text-zinc-muted">
            nodes
          </span>
          {okCount < graph.nodes.length && (
            <span className="text-[11px] text-zinc-muted" title="Nodes that finished without succeeding (skipped, interrupted, failed)">
              (terminal but not successful)
            </span>
          )}
        </span>
        {span && (
          <span className="flex items-baseline gap-1.5" title="Wall-clock span across node timestamps">
            <span className="text-xl font-bold tabular-nums leading-none text-zinc-100">
              {span}
            </span>
            <span className="text-[10px] font-bold uppercase tracking-[0.12em] text-zinc-muted">
              {running ? 'so far' : 'total'}
            </span>
          </span>
        )}
        {sid && (
          <span className="truncate font-mono text-[11px] text-zinc-muted" title={sid}>
            {sid.slice(0, 12)}…
          </span>
        )}
      </div>
      <div className="mt-2 max-w-[720px]">
        <Timeline nodes={graph.nodes} running={running} />
      </div>
    </div>
  );
}

/** Per-node duration bars, from the timing the executor stamps into the
    graph rollup. This is what makes a 310s run legible: instead of "3
    nodes, took forever", you see planner 6s, researcher 275s, formatter
    8s — and you know exactly which hop stalled. Bars scale to the longest
    node; the running node pulses. */
function Timeline({ nodes, running }: { nodes: GraphNode[]; running: boolean }) {
  const nowS = Date.now() / 1000;
  const rows = nodes.map((n) => {
    const start = typeof n.started_at === 'number' ? n.started_at : null;
    const end = typeof n.completed_at === 'number' ? n.completed_at : null;
    const live = running && start != null && end == null;
    const dur = typeof n.elapsed_s === 'number' ? n.elapsed_s
      : start != null ? Math.max(0, (end ?? nowS) - start) : null;
    return { n, dur, live };
  });
  const max = Math.max(0.001, ...rows.map((r) => r.dur ?? 0));
  return (
    <div className="space-y-1" role="list" aria-label="Node durations">
      {rows.map(({ n, dur, live }) => (
        <div key={n.id} role="listitem" className="flex items-center gap-2">
          <span className="w-32 flex-none truncate font-mono text-[11.5px] text-zinc-300" title={n.id}>
            {(n.skill || n.id).replace(/agent$/i, '').slice(0, 18) || n.id}
          </span>
          <div className="h-2.5 min-w-0 flex-1 overflow-hidden rounded-full bg-white/5">
            <div
              className={`h-full rounded-full ${live ? 'animate-pulse bg-violet-400' : n.status === 'failed' || n.status === 'error' ? 'bg-red-400/70' : isTerminal(n.status) ? 'bg-emerald-400/60' : 'bg-zinc-600'}`}
              style={{ width: `${Math.max(2, ((dur ?? 0) / max) * 100)}%` }}
            />
          </div>
          <span className="w-16 flex-none text-right font-mono text-[11.5px] tabular-nums text-zinc-300">
            {dur == null ? '—' : fmtDur(dur)}
          </span>
        </div>
      ))}
    </div>
  );
}

/** The run's live reasoning log, streamed as SSE `log` frames. Previously
    discarded entirely; now a first-class view so a long run shows its work
    instead of a static canvas. Capped server-side (queue) and client-side
    (300 lines); follow can be paused to read. */
function LogView({ logs, running, follow, setFollow, boxRef }: {
  logs: string[];
  running: boolean;
  follow: boolean;
  setFollow: (v: boolean) => void;
  boxRef: React.RefObject<HTMLDivElement | null>;
}) {
  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-none items-center gap-2 border-b border-white/10 px-4 py-2">
        <span className="text-[10.5px] font-bold tracking-[0.14em] text-zinc-muted">
          LIVE LOG{running ? ' · streaming' : ''}
        </span>
        <span className="flex-1" />
        <span className="font-mono text-[10.5px] text-zinc-muted">{logs.length} lines</span>
        <button
          onClick={() => setFollow(!follow)}
          aria-pressed={follow}
          className="rounded-md border border-white/15 bg-white/5 px-2.5 py-1 text-[11px] text-zinc-300 hover:border-violet-400"
        >
          {follow ? 'pause follow' : 'follow'}
        </button>
      </div>
      <div
        ref={boxRef}
        onScroll={() => {
          const el = boxRef.current;
          if (el) setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 120);
        }}
        className="min-h-0 flex-1 overflow-y-auto p-4 font-mono text-[11.5px] leading-relaxed"
        aria-live="off"
      >
        {!logs.length && (
          <div className="text-zinc-muted">
            {running ? 'waiting for the first log line…' : 'no log lines captured for this run.'}
          </div>
        )}
        {logs.map((l, i) => (
          <div key={i} className="whitespace-pre-wrap break-words text-zinc-400">
            <span className="mr-2 select-none text-zinc-600">{String(i + 1).padStart(3, ' ')}</span>{l}
          </div>
        ))}
      </div>
    </div>
  );
}
