import { useCallback, useEffect, useRef, useState } from 'react';
import { Plus, RefreshCw, Send } from 'lucide-react';
import { Rail, TopBar, Empty, Skel, Pill, useHealth } from '../components/ui';
import { renderMarkdown } from '../components/markdown';
import { api, ago, CONF, type ChatThread } from '../api';

interface Msg {
  id: number;
  who: 'you' | 'aria' | 'err';
  html?: string;
  text?: string;
  meta?: string;
}

/* Lightweight chat: one direct LLM call per turn via /api/chat/simple.
   No DAG, no sessions, no cost ledger — threads live in their own store
   and never appear in Runs. Research keeps the full pipeline. */
export default function Chat() {
  const [threads, setThreads] = useState<ChatThread[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [filter, setFilter] = useState('');
  const [cid, setCid] = useState('');
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  // Thread id currently being deleted, so the control can disable itself and
  // a rapid second click cannot fire a second DELETE.
  const [dropping, setDropping] = useState('');
  /* Per-conversation document access. On by default; turning it off means no
     uploaded document may contribute to this conversation's answers. */
  const [useDocs, setUseDocs] = useState(true);
  const [status, setStatus] = useState('');
  /* The gateway pill reads the shared health hook like every other page.
     A hand-rolled one-shot fetch here stayed stale after a gateway flap
     while the rail dot next to it updated — two indicators disagreeing. */
  const gwUp = useHealth();
  const [crumb, setCrumb] = useState('Chat');
  const boxRef = useRef<HTMLDivElement>(null);
  const reqRef = useRef(0);
  const idRef = useRef(1);
  const abortRef = useRef<AbortController | null>(null);
  const stickRef = useRef(true);
  const mountedRef = useRef(true);
  const [nowS, setNowS] = useState(Date.now() / 1000);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      abortRef.current?.abort(); // stop listening if we navigate away mid-stream
    };
  }, []);

  const nextId = () => idRef.current++;

  const refresh = useCallback(async () => {
    try {
      const d = await api.chatThreads(50);
      if (!mountedRef.current) return;
      setThreads(d.threads || []);
      setLoaded(true);
      setLoadError('');
    } catch (e) {
      if (!mountedRef.current) return;
      setLoaded(true);
      setLoadError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  /* Follow the stream only while the user is already at the bottom. The
     unconditional `scrollTop = scrollHeight` on every delta physically
     prevented scrolling up to re-read earlier text mid-answer, because
     every arriving frame yanked the transcript back down. */
  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    if (nearBottom || stickRef.current) {
      el.scrollTop = el.scrollHeight;
      stickRef.current = true;
    }
  }, [msgs]);

  /* "5m ago" was frozen between renders because nothing re-rendered on a
     timer, and a thread created in another tab never appeared. */
  useEffect(() => {
    const t = setInterval(() => {
      if (!mountedRef.current) return;
      setNowS(Date.now() / 1000);
      refresh();
    }, CONF.pollSlowMs);
    return () => clearInterval(t);
  }, [refresh]);

  const select = async (t: ChatThread) => {
    // Switching threads abandons any in-flight stream. Bumping reqRef makes
    // its loop bail, and the abort actually stops the request — without it
    // the abandoned turn kept streaming and holding a gateway call.
    abortRef.current?.abort();
    const req = ++reqRef.current;
    setCid(t.conversation_id);
    setCrumb((t.title || 'Chat').slice(0, 40));
    // Load this conversation's document preference so the toggle reflects
    // what the server will actually do, rather than always reading "on".
    try {
      const pr = await api.chatThreadPrefs(t.conversation_id);
      if (!mountedRef.current || reqRef.current !== req) return;
      setUseDocs(pr.prefs?.use_documents !== false);
    } catch { /* leave the default */ }
    try {
      const d = await api.chatThread(t.conversation_id);
      if (!mountedRef.current || reqRef.current !== req) return; // stale
      setMsgs((d.messages || []).map((m) => m.role === 'user'
        ? { id: nextId(), who: 'you' as const, text: m.content }
        : { id: nextId(), who: 'aria' as const, html: renderMarkdown(m.content) }));
    } catch {
      if (!mountedRef.current || reqRef.current !== req) return;
      setMsgs([{ id: nextId(), who: 'err', text: 'could not load this thread.' }]);
    }
  };

  const STARTERS = [
    'What can you do?',
    'Summarize my recent runs',
    'What is in my memory?',
  ];

  const send = async (override?: string) => {
    const q = (override ?? input).trim();
    if (!q || sending) return;
    const req = ++reqRef.current;
    setSending(true);
    setInput('');
    setStatus('thinking…');
    setMsgs((m) => [...m, { id: nextId(), who: 'you', text: q }]);
    setCrumb(q.length > 40 ? q.slice(0, 40) + '…' : q);
    const t0 = Date.now();
    // Aria's bubble appears immediately and fills in as deltas arrive.
    const bubbleId = nextId();
    const ac = new AbortController();
    abortRef.current = ac;
    let acc = '';
    // Coalesce paints to one per animation frame. The server sends a frame
    // every ~12ms; re-running renderMarkdown over the whole accumulated
    // answer and rebuilding the array on every one is O(n²) on the main
    // thread, which is what made long answers feel like they stuttered.
    let queued = 0;
    const paint = () => {
      if (!mountedRef.current || reqRef.current !== req) return;
      if (queued) return;
      queued = requestAnimationFrame(() => {
        queued = 0;
        if (!mountedRef.current || reqRef.current !== req) return;
        const secs = ((Date.now() - t0) / 1000).toFixed(1);
        setMsgs((m) => {
          const rest = m.filter((x) => x.id !== bubbleId);
          const meta = acc ? `streaming · ${secs}s` : 'working…';
          return [...rest, { id: bubbleId, who: 'aria', html: renderMarkdown(acc || '…'), meta }];
        });
      });
    };
    const settle = (meta: string) => {
      if (queued) { cancelAnimationFrame(queued); queued = 0; }
      setMsgs((m) => [...m.filter((x) => x.id !== bubbleId), {
        id: bubbleId, who: 'aria', html: renderMarkdown(acc || '…'), meta,
      }]);
    };
    try {
      for await (const ev of api.chatSimpleStream(q, cid, ac.signal)) {
        if (!mountedRef.current || reqRef.current !== req) return; // superseded
        if (ev.type === 'started' && (ev as { conversation_id?: string }).conversation_id) {
          setCid((ev as { conversation_id?: string }).conversation_id as string);
        } else if (ev.type === 'status') {
          setStatus((ev as { text?: string }).text || 'working…');
        } else if (ev.type === 'delta') {
          acc += (ev as { text?: string }).text ?? '';
          paint();
        } else if (ev.type === 'done') {
          acc = (ev as { answer?: string }).answer ?? acc;
          if ((ev as { conversation_id?: string }).conversation_id) {
            setCid((ev as { conversation_id?: string }).conversation_id as string);
          }
          settle(`done in ${((Date.now() - t0) / 1000).toFixed(1)}s · lightweight`);
          refresh();
        } else if (ev.type === 'error') {
          setMsgs((m) => [...m.filter((x) => x.id !== bubbleId), {
            id: nextId(), who: 'err', text: (ev as { text?: string }).text || 'chat failed',
          }]);
        }
      }
    } catch (e) {
      if (!mountedRef.current || reqRef.current !== req) return;
      if ((e as Error).name === 'AbortError') {
        // Stopping mid-answer must not throw away what already streamed:
        // the gateway call was billed, so the user keeps the partial text.
        if (acc.trim()) settle(`stopped after ${((Date.now() - t0) / 1000).toFixed(1)}s · partial answer`);
        else setMsgs((m) => [...m.filter((x) => x.id !== bubbleId), {
          id: nextId(), who: 'err', text: 'stopped before any answer arrived.' }]);
      } else {
        setMsgs((m) => [...m.filter((x) => x.id !== bubbleId), {
          id: nextId(), who: 'err', text: String((e as Error).message || e),
        }]);
      }
    } finally {
      // The busy flag and the controller must clear even when this turn was
      // superseded (thread switch, New chat). Guarding them on `req` left
      // Send disabled and `stop` a no-op forever — a wedged composer.
      if (abortRef.current === ac) abortRef.current = null;
      if (mountedRef.current) {
        setSending(false);
        setStatus('');
      }
    }
  };

  const stop = () => {
    abortRef.current?.abort();
    setStatus('stopping…');
  };

  const fresh = () => {
    reqRef.current++; // invalidate any in-flight select/send
    abortRef.current?.abort(); // and stop any open stream
    setCid('');
    setMsgs([]);
    setCrumb('Chat');
    setSending(false);
    setStatus('');
  };

  const dropThread = async (id: string) => {
    if (!id || sending || dropping === id) return;
    // This is the only destructive control in the console with neither a
    // confirm() nor a disabled state, so N clicks issued N DELETEs.
    if (!confirm('Delete this conversation? This cannot be undone.')) return;
    setDropping(id);
    abortRef.current?.abort();
    reqRef.current++;
    try {
      await api.chatThreadDelete(id);
    } catch (e) {
      setMsgs([{ id: nextId(), who: 'err', text: `could not delete thread: ${e instanceof Error ? e.message : String(e)}` }]);
      setDropping('');
      return;
    }
    if (id === cid) {
      setCid('');
      setMsgs([]);
      setCrumb('Chat');
    }
    setDropping('');
    refresh();
  };

  const vis = threads.filter((t) => !filter || (t.title || '').toLowerCase().includes(filter.toLowerCase()));

  return (
    <div className="flex h-full flex-col lg:flex-row">
      <Rail />
      <div className="flex w-full max-h-[34vh] flex-none flex-col border-b border-white/10 bg-[#0b0b0e] lg:max-h-none lg:w-[248px] lg:border-b-0 lg:border-r">
        <div className="flex items-center justify-between px-3.5 pb-2 pt-3.5 text-xs font-bold tracking-wide">
          Threads
          <span className="rounded-full border border-white/10 bg-white/5 px-2.5 py-0.5 text-[10.5px] font-bold text-zinc-400">{threads.length}</span>
        </div>
        <div className="px-2.5 pb-2">
          <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter…" aria-label="Filter threads" className="w-full rounded-md border border-white/10 bg-black/40 px-2.5 py-1.5 text-xs outline-none focus:border-violet-400" />
        </div>
        <div className="min-h-0 flex-1 space-y-2 overflow-y-auto px-2.5 pb-3">
          {!loaded && <Skel />}
          {loaded && loadError && !threads.length && (
            <div className="rounded-lg border border-red-400/30 bg-red-400/5 p-3 text-center text-xs text-red-200">
              couldn't load threads ({loadError}). <button onClick={refresh} className="underline">retry</button>
            </div>
          )}
          {/* A failed 15s refresh with threads on screen used to be silent
              (stale list, no indication) while Runs/Research show an amber
              banner for exactly this state. */}
          {loaded && loadError && !!threads.length && (
            <div className="rounded-lg border border-amber-400/30 bg-amber-400/5 p-2.5 text-center text-[11px] text-amber-200">
              refresh failed — showing last data. <button onClick={refresh} className="underline">retry</button>
            </div>
          )}
          {loaded && !loadError && !vis.length && <Empty title="No threads" sub="Say hello below to start the first one." />}
          {vis.map((t) => (
            <div
              key={t.conversation_id}
              className={`group relative rounded-[10px] border ${t.conversation_id === cid && cid ? 'border-violet-400/60 bg-violet-400/5' : 'border-white/10 bg-[#0e0e12] hover:border-white/25'}`}
            >
              <button
                onClick={() => select(t)}
                className="block w-full p-2.5 text-left"
              >
                <div className="truncate pr-5 text-[12.5px] font-semibold">{(t.title || '(untitled)').slice(0, 90)}</div>
                <div className="mt-1 text-[11px] text-zinc-muted">{t.updated ? ago(nowS - t.updated) : '—'}{t.turns ? ` · ${t.turns} turns` : ''}</div>
              </button>
              {/* Threads were undeletable in UI despite a working DELETE
                  endpoint and client. The control appears on hover/focus so
                  the rail stays clean. */}
              <button
                onClick={(e) => { e.stopPropagation(); dropThread(t.conversation_id); }}
                disabled={dropping === t.conversation_id}
                title={`Delete thread "${(t.title || 'untitled').slice(0, 40)}"`}
                aria-label={`Delete thread ${(t.title || 'untitled').slice(0, 40)}`}
                className="absolute right-1.5 top-1.5 rounded px-1.5 py-0.5 text-sm leading-none text-zinc-muted opacity-0 hover:bg-red-400/15 hover:text-red-200 focus:opacity-100 group-hover:opacity-100 disabled:opacity-40"
              >
                ×
              </button>
            </div>
          ))}
        </div>
      </div>
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <TopBar crumb={crumb}>
          <Pill tone="muted">lightweight · no pipeline</Pill>
          <button
            onClick={async () => {
              if (!cid) return;
              const next = !useDocs;
              setUseDocs(next);
              try { await api.setChatThreadPrefs(cid, next); }
              catch { setUseDocs(!next); }
            }}
            disabled={!cid}
            aria-pressed={useDocs}
            title={useDocs
              ? 'Uploaded documents can inform this chat'
              : 'This chat will not use uploaded documents'}
            className="rounded-full border border-white/15 bg-white/5 px-2.5 py-1 text-[10.5px] font-bold text-zinc-400 hover:border-violet-400 disabled:opacity-40"
          >
            {useDocs ? 'docs on' : 'docs off'}
          </button>
          <Pill tone={gwUp ? 'ok' : 'muted'}>{gwUp ? 'gateway' : 'gateway…'}</Pill>
          <button onClick={refresh} title="Refresh" aria-label="Refresh threads" className="rounded-md border border-white/15 bg-white/5 px-2.5 py-1.5 hover:border-violet-400"><RefreshCw size={14} /></button>
          <button onClick={fresh} title="New chat" aria-label="Start new chat" className="rounded-md border border-white/15 bg-white/5 px-2.5 py-1.5 hover:border-violet-400"><Plus size={14} /></button>
        </TopBar>
        <div
          ref={boxRef}
          onScroll={() => {
            const el = boxRef.current;
            if (el) stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
          }}
          className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4"
        >
          {!msgs.length && (
            <div className="max-w-[640px] rounded-[10px] border border-white/10 bg-[#0e0e12] p-4">
              <div className="mb-1 text-xs font-bold text-violet-300">Aria</div>
              <p className="text-[13px] leading-relaxed text-zinc-300">
                Quick answers, no pipeline, nothing recorded in Runs. For deep research with a visible pipeline, use <a href="/research" className="text-violet-300 hover:underline">Research</a>.
              </p>
              <div className="mt-3 flex flex-wrap gap-2" role="group" aria-label="Starter prompts">
                {STARTERS.map((s) => (
                  <button
                    key={s}
                    onClick={() => send(s)}
                    disabled={sending}
                    className="rounded-full border border-violet-400/30 bg-violet-400/[0.07] px-3 py-1.5 text-xs text-violet-200 transition-colors hover:border-violet-400/60 hover:bg-violet-400/[0.14] disabled:opacity-50"
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}
          {msgs.map((m) => (
            <div
              key={m.id}
              aria-live={m.who === 'aria' ? 'polite' : undefined}
              aria-atomic={m.who === 'aria' ? 'false' : undefined}
              className={`max-w-[720px] rounded-[10px] border p-4 ${m.who === 'you' ? 'ml-auto border-violet-400/30 bg-violet-400/[0.07]' : m.who === 'err' ? 'border-red-400/40 bg-red-400/5' : 'border-white/10 bg-[#0e0e12]'}`}
            >
              <div className="mb-1 text-xs font-bold text-violet-300">{m.who === 'you' ? 'You' : m.who === 'err' ? 'Error' : 'Aria'}</div>
              {m.html
                ? <div className="prose-chat space-y-2 text-[13px] leading-relaxed text-zinc-200" dangerouslySetInnerHTML={{ __html: m.html }} />
                : <p className="whitespace-pre-wrap break-words text-[13px] leading-relaxed text-zinc-200">{m.text}</p>}
              {m.meta && <div className="mt-1.5 text-[11px] text-zinc-muted">{m.meta}</div>}
            </div>
          ))}
          {!stickRef.current && msgs.length > 0 && (
            <div className="sticky bottom-0 flex justify-center">
              <button
                onClick={() => { stickRef.current = true; const el = boxRef.current; if (el) el.scrollTop = el.scrollHeight; }}
                className="rounded-full border border-white/15 bg-[#0e0e12] px-3 py-1 text-[11px] font-bold text-zinc-300 hover:border-violet-400"
              >
                jump to latest
              </button>
            </div>
          )}
          {sending && (
            <div className="flex items-center gap-2 text-xs text-zinc-muted" role="status" aria-live="polite">
              <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-violet-400" />
              {status || 'thinking…'}
              <button onClick={stop} className="rounded border border-white/15 bg-white/5 px-2 py-0.5 text-[10.5px] font-bold text-zinc-300 hover:border-red-400/60 hover:text-red-200">stop</button>
            </div>
          )}
        </div>
        <div className="border-t border-white/10 p-3">
          <div className="flex gap-2">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                // Enter while an IME is composing confirms the candidate, not
                // the message — sending there posts half-composed text.
                if (e.nativeEvent.isComposing || (e.nativeEvent as unknown as { keyCode?: number }).keyCode === 229) return;
                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
              }}
              placeholder="Message Aria… (Enter to send, Shift+Enter for newline)"
              aria-label="Message Aria"
              rows={2}
              className="min-h-[44px] flex-1 resize-y rounded-lg border border-white/10 bg-black/40 p-2.5 text-[13px] outline-none focus:border-violet-400"
            />
            <button onClick={() => send()} disabled={sending} aria-label="Send message" className="flex items-center gap-1.5 self-end rounded-md bg-violet-400 px-4 py-2.5 text-xs font-bold text-[#0b0b0e] shadow-[0_0_16px_rgba(139,124,246,0.35)] hover:brightness-110 disabled:opacity-50">
              <Send size={13} /> Send
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
