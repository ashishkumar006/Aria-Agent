import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Plus, RefreshCw, Send, Download, Eye, X, Loader2, FileText, Table2, Presentation,
} from 'lucide-react';
import { Rail, TopBar, Empty, Skel, Pill, useHealth, SkipLink } from '../components/ui';
import { renderMarkdown } from '../components/markdown';
import { api, ago, CONF, headers, type ChatThread, type DocPreview } from '../api';

/* The document viewer drags the whole setup panel in behind it and chat has no
   setup panel, so it is route-split the same way App.tsx splits the views: the
   transcript does not pay for it until a produced file is actually opened. */
const DocumentViewer = lazy(() =>
  import('./DocSetup').then((m) => ({ default: m.DocumentViewer })));

/** Mirrors agent_server._CHAT_MAX_QUERY. The server rejects a longer query
 *  with a 400, so the composer has to refuse one - visibly, with the text
 *  still in the box - rather than quietly clamping it and losing the tail. */
const CHAT_MAX_QUERY = 4000;

interface ArtRef {
  id: string;
  /** The link text the model used, when it wrote one. The only name an
   *  answer carries; the server re-derives its own from the descriptor. */
  label?: string;
}

/** What the artifact store says about a handle the transcript named. */
type ArtState =
  | { status: 'pending' }
  | { status: 'missing' }
  | { status: 'ok'; preview: DocPreview };

interface Msg {
  id: number;
  who: 'you' | 'aria' | 'err';
  html?: string;
  text?: string;
  meta?: string;
  /** Artifact handles this answer names. See artRefs. */
  arts?: ArtRef[];
}

/** The artifact handles an answer names.
 *
 *  `/api/chat/simple/stream`'s `done` frame carries the answer text and
 *  nothing else - unlike the authoring endpoint it has no `files` array - so
 *  the handle the model printed in its own sentence is the only thing that
 *  crosses the wire. It is also what the thread store persists, which is why
 *  a handle found in a conversation reopened today is exactly as live as one
 *  from the turn streaming right now. The id grammar is the server's own. */
function artRefs(s: string): ArtRef[] {
  const text = String(s || '');
  const out = new Map<string, ArtRef>();
  for (const m of text.matchAll(/\[([^\]\n]{1,160})\]\((art:[0-9a-fA-F]{16})\)/g)) {
    out.set(m[2], { id: m[2], label: m[1].trim() });
  }
  for (const m of text.matchAll(/art:[0-9a-fA-F]{16}/g)) {
    if (!out.has(m[0])) out.set(m[0], { id: m[0] });
  }
  return Array.from(out.values());
}

/** A server failure, without its HTTP status.
 *
 *  The shared client prefixes the status so its own logs stay readable, which
 *  meant a rejected turn rendered in the transcript as the assistant having
 *  said "400 query too long (max 4000 chars)" - a status code in chat prose.
 *  The one failure a person can cause from the composer is said in their
 *  terms instead; anything else keeps the server's own words, minus the code. */
function humanError(raw: unknown, sentChars?: number): string {
  const s = String(raw ?? '')
    .replace(/^HTTP\s+\d{3}\s*/i, '')
    .replace(/^\d{3}\s+/, '')
    .trim();
  const tooLong = /query too long \(max (\d+) chars\)/i.exec(s);
  if (tooLong) {
    const max = Number(tooLong[1]) || CHAT_MAX_QUERY;
    const over = sentChars ? sentChars - max : 0;
    return (over > 0
      ? `That message is ${over.toLocaleString()} characters over the `
      : 'That message is over the ')
      + `${max.toLocaleString()}-character limit, so it was not sent. `
      + 'Trim it and send again.';
  }
  return s || 'chat failed.';
}

/** Did this turn die on the length limit? Those are the only failures where
 *  the text provably never reached the model, and so the only ones worth
 *  putting back into the composer. */
const isTooLong = (m: unknown) => /query too long|max \d+ chars/i.test(String(m || ''));

/** The name the server chose for a file, out of its Content-Disposition.
 *  `filename*` (RFC 5987) first: it is the UTF-8 form browsers actually use,
 *  and the plain `filename` beside it is ASCII-folded. */
function serverFileName(cd: string | null): string {
  if (!cd) return '';
  const star = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(cd);
  if (star) {
    try { return decodeURIComponent(star[1].trim()); }
    catch { /* not valid percent-encoding: fall back */ }
  }
  const plain = /filename\s*=\s*"?([^";]+)"?/i.exec(cd);
  return plain ? plain[1].trim() : '';
}

const KIND_ICON: Record<string, typeof FileText> = {
  pdf: FileText, pptx: Presentation, docx: FileText, xlsx: Table2,
};
/** One produced document, inside the answer that named it: the file row, the
 *  download and the preview.
 *
 *  Rendered per handle rather than in a side panel because a transcript can
 *  name several files across several turns, and each belongs to the sentence
 *  that promised it. Owns its own download state so two files in one answer
 *  do not share a spinner. */
function ArtRow({ art, label, state }: { art: string; label?: string; state: ArtState }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ text: string; bad: boolean } | null>(null);
  const p = state.status === 'ok' ? state.preview : null;
  const kind = p && p.kind !== 'unknown' ? p.kind : '';
  const Icon = KIND_ICON[kind] || FileText;
  // Prefer the model's own link text when it looks like a filename. The server
  // re-derives a name from the artifact descriptor and that is the name the
  // saved file actually gets, so this is a label, not the save name.
  const raw = (label || '').trim();
  const name = raw
    ? (/\.[A-Za-z0-9]{1,6}$/.test(raw) ? raw : `${raw}.${kind || 'bin'}`)
    : `document.${kind || 'bin'}`;

  const get = async () => {
    if (busy) return;
    setBusy(true);
    setNote(null);
    try {
      // An <a href> navigation cannot send the auth header, so this fetch is
      // authed and the browser is handed a blob url instead.
      const r = await fetch(`/api/artifact/${art}?download=1`, { headers: headers() });
      if (!r.ok) {
        let msg = `the server refused it (${r.status})`;
        try { msg = String((await r.json())?.error || msg); } catch { /* not json */ }
        throw new Error(msg);
      }
      const blob = await r.blob();
      const saved = serverFileName(r.headers.get('content-disposition')) || name;
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = saved;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 4000);
      setNote({
        text: `saved ${saved} (${Math.max(1, Math.round(blob.size / 1024))} KB)`,
        bad: false,
      });
    } catch (e) {
      setNote({ text: `download failed: ${String((e as Error)?.message || e)}`, bad: true });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-lg border border-white/10 bg-black/30 p-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <Icon size={14} className="flex-none text-violet-300" />
        <span className="min-w-0 flex-1 basis-40">
          <span className="block truncate text-[12.5px] font-semibold">{p ? name : art}</span>
          {p?.subtitle && (
            <span className="block truncate text-[10.5px] text-zinc-muted">{p.subtitle}</span>
          )}
        </span>
        {kind && <Pill tone="info">{kind}</Pill>}
        {state.status === 'ok' && p && (
          <>
            {p.kind !== 'unknown' && (
              <button
                type="button"
                onClick={() => setOpen(!open)}
                aria-expanded={open}
                aria-controls={`artpv-${art}`}
                aria-label={`${open ? 'Hide' : 'Show'} preview of ${name}`}
                className="flex flex-none items-center gap-1 rounded-md border border-white/10 px-2 py-1 text-[11px] font-semibold hover:border-violet-400"
              >
                {open ? <X size={12} /> : <Eye size={12} />} preview
              </button>
            )}
            <button
              type="button"
              onClick={get}
              disabled={busy}
              aria-label={`Download ${name}`}
              className="flex flex-none items-center gap-1 rounded-md border border-white/10 px-2 py-1 text-[11px] font-semibold hover:border-violet-400 disabled:opacity-60"
            >
              {busy ? <Loader2 size={12} className="animate-spin" /> : <Download size={12} />} Get
            </button>
          </>
        )}
      </div>
      {state.status === 'pending' && (
        <p className="mt-1 text-[11px] text-zinc-muted">checking the artifact store…</p>
      )}
      {/* Say it rather than quietly dropping the handle: a dead download
          button for a file that was never rendered is the same unreachable
          document, wearing a different hat. */}
      {state.status === 'missing' && (
        <p className="mt-1 text-[11px] text-amber-300">
          {art} is not in the artifact store - the answer names a file that was never rendered.
        </p>
      )}
      {note && (
        <p role="status"
          className={`mt-1 text-[11px] ${note.bad ? 'text-red-300' : 'text-zinc-muted'}`}>
          {note.text}
        </p>
      )}
      {open && p && p.kind !== 'unknown' && (
        <div id={`artpv-${art}`} className="docview-frame mt-2.5">
          <Suspense fallback={<div className="p-3 text-[11.5px] text-zinc-muted">loading the preview…</div>}>
            <DocumentViewer source={{ kind: 'artifact', artifact: art, format: p.kind, label: name }} />
          </Suspense>
        </div>
      )}
    </div>
  );
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
  const nearLimit = input.length > CHAT_MAX_QUERY * 0.9;
  const overLimit = input.length > CHAT_MAX_QUERY;
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
  // Synchronous mirror of `sending` — see the guard in send().
  const sendingRef = useRef(false);
  const stickRef = useRef(true);
  // Mirror of `threads` so loadMore can read the current length without
  // re-creating its callback on every poll.
  const threadsRef = useRef<ChatThread[]>([]);
  useEffect(() => { threadsRef.current = threads; }, [threads]);
  const mountedRef = useRef(true);
  const [nowS, setNowS] = useState(Date.now() / 1000);
  /* A composer-level message: why a send did not happen. Whitespace-only Enter
     used to clear the box and say nothing, which reads as a dead key. */
  const [hint, setHint] = useState<{ text: string; tone: 'warn' | 'err' } | null>(null);
  /* What chat can actually reach. null means "not read yet or unreadable" —
     never a fallback number, because a wrong number here is a lie. */
  const [toolCount, setToolCount] = useState<number | null>(null);
  const [toolsOn, setToolsOn] = useState<boolean | null>(null);
  const [arts, setArts] = useState<Record<string, ArtState>>({});
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      abortRef.current?.abort(); // stop listening if we navigate away mid-stream
    };
  }, []);

  const nextId = () => idRef.current++;

  const PAGE = 50;
  const [total, setTotal] = useState(0);
  const [moreLoading, setMoreLoading] = useState(false);
  const loadedOnceRef = useRef(false);

  const refresh = useCallback(async () => {
    try {
      const d = await api.chatThreads(PAGE, 0);
      if (!mountedRef.current) return;
      loadedOnceRef.current = true;
      const first = d.threads || [];
      /* Keep whatever loadMore paged in. This used to `setThreads(page0)`
         outright, so fifteen seconds after "load 50 older threads" the poll
         quietly threw them away and offered to load them all over again. */
      setThreads((prev) => {
        const seen = new Set(first.map((t) => t.conversation_id));
        return [...first, ...prev.filter((t) => !seen.has(t.conversation_id))];
      });
      setTotal(typeof d.total === 'number' ? d.total : first.length);
      setLoaded(true);
      setLoadError('');
    } catch (e) {
      if (!mountedRef.current) return;
      setLoaded(true);
      setLoadError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  /* Older threads were unreachable: the rail requested limit=50 and had no
     way to ask for more, while the badge showed "50" as if it were the
     total. Fetch the next page on demand (the server's `offset` +
     `has_more`). */
  const loadMore = useCallback(async () => {
    if (moreLoading) return;
    setMoreLoading(true);
    try {
      const from = threadsRef.current.length;
      const d = await api.chatThreads(PAGE, from);
      if (!mountedRef.current) return;
      const extra = d.threads || [];
      setThreads((prev) => {
        const seen = new Set(prev.map((t) => t.conversation_id));
        return [...prev, ...extra.filter((t) => !seen.has(t.conversation_id))];
      });
      setTotal(typeof d.total === 'number' ? d.total : from + extra.length);
    } catch (e) {
      if (mountedRef.current) setLoadError(e instanceof Error ? e.message : String(e));
    } finally {
      if (mountedRef.current) setMoreLoading(false);
    }
  }, [moreLoading]);
  /* What the tool pill says, read from the server rather than asserted here.
     It was the literal "21 tools", so it would go on claiming 21 the day the
     next tool landed. Two reads, both already-authed client calls:

     `/api/capabilities` builds `tools.tools` from the server's own
     `_CHAT_TOOLS` list — the same list that generates the chat system prompt,
     and the one definition of what chat can reach. That is the single source
     of truth this pill uses. `/api/tools` is NOT it: it is the whole skill
     catalog (40 entries), including the ones chat deliberately refuses
     (send_email, delete_file, computer_action), so counting it would overstate
     the surface by nineteen.

     The `chat.tools` flag is read as well, because it can switch tool calling
     off entirely — and then the honest number is zero, not the size of the
     catalog. */
  const loadCaps = useCallback(async () => {
    const [caps, flags] = await Promise.allSettled([
      fetch('/api/capabilities', { headers: headers() }).then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return (await r.json()) as { tools?: { tools?: unknown } };
      }),
      api.flags(),
    ]);
    if (!mountedRef.current) return;
    if (caps.status === 'fulfilled') {
      const names = caps.value?.tools?.tools;
      setToolCount(Array.isArray(names) ? names.length : null);
    } else {
      setToolCount(null);
    }
    if (flags.status === 'fulfilled') {
      const row = (flags.value.flags || []).find((f) => f.name === 'chat.tools');
      setToolsOn(row ? row.value !== false : null);
    } else {
      setToolsOn(null);
    }
  }, []);

  useEffect(() => { loadCaps(); }, [loadCaps]);

  /* Retry a read that failed rather than leaving "tools unknown" parked in
     the header until someone hits refresh. Only while it is unknown: the
     capability document is static per deploy, so polling it forever would be
     one request every 15s to re-read a constant. */
  useEffect(() => {
    if (toolCount !== null && toolsOn !== null) return;
    const t = setTimeout(() => { loadCaps(); }, CONF.pollSlowMs);
    return () => clearTimeout(t);
  }, [toolCount, toolsOn, loadCaps]);

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

  /* Every artifact handle the transcript names, verified against the artifact
     store. `api.artifactPreview` resolves the bytes, so it is the honest
     check: a model can print an `art:` id it never rendered, and only the
     store knows which ones are real. A handle that does not resolve gets no
     download button. Keyed by the joined id set, so a turn that names no new
     file causes no request and the effect cannot loop on its own state. */
  const artKey = useMemo(
    () => Array.from(new Set(msgs.flatMap((m) => m.arts || []).map((a) => a.id))).sort().join(','),
    [msgs],
  );
  useEffect(() => {
    const ids = artKey ? artKey.split(',') : [];
    if (!ids.length) {
      setArts({});
      return;
    }
    let alive = true;
    (async () => {
      const got: Record<string, ArtState> = {};
      for (const id of ids) {
        if (!alive) return;
        try {
          const pv = await api.artifactPreview(id);
          if (!alive) return;
          got[id] = { status: 'ok', preview: pv };
        } catch {
          if (!alive) return;
          got[id] = { status: 'missing' };
        }
      }
      if (!alive) return;
      setArts(got);
    })();
    return () => { alive = false; };
  }, [artKey]);

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
    setHint(null);
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
      // Hands are scanned on reload too, so a document produced in an earlier
      // conversation comes back reachable rather than as prose.
      setMsgs((d.messages || []).map((m) => m.role === 'user'
        ? { id: nextId(), who: 'you' as const, text: m.content }
        : { id: nextId(), who: 'aria' as const, html: renderMarkdown(m.content), arts: artRefs(m.content) }));
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
    const raw = override ?? input;
    const q = raw.trim();
    if (!q) {
      /* Whitespace-only Enter was a dead key: `if (!q) return` fired before
         anything, so the composer kept its spaces, no request went out and
         nothing on screen said why. Drop the spaces — they were never a
         message — and say that nothing was sent. Enter on a genuinely empty
         box stays silent on purpose: there is nothing there to explain. */
      if (raw.length > 0) {
        setInput('');
        setHint({ text: 'That message was blank, so nothing was sent.', tone: 'warn' });
      }
      return;
    }
    /* Refuse an over-long query here, in the open, instead of relying on
       maxLength to swallow the tail. The cap used to clamp the textarea at
       4000, so pasting 5000 characters dropped 1000 of them without a word
       and left the counter showing a reassuring amber "4000 / 4000": the
       user's text was gone and the UI said it was fine. Nothing is clamped
       now. The overflow is visible, the reason is stated, and the text stays
       in the box to be edited down. */
    if (q.length > CHAT_MAX_QUERY) {
      setHint({
        text: `${q.length.toLocaleString()} characters is ${(q.length - CHAT_MAX_QUERY).toLocaleString()} over the `
          + `${CHAT_MAX_QUERY.toLocaleString()}-character limit, so it was not sent. `
          + 'Trim it and send again.',
        tone: 'err',
      });
      return;
    }
    // `sending` is React state, so the guard read a stale closure: two
    // activations inside ONE tick (a dblclick on a starter chip, Enter
    // twice) both saw false, both posted a billed turn, and the second
    // reqRef bump made the first stream bail before creating its bubble
    // — two user bubbles, one answer. A ref is synchronous, so the claim
    // is atomic; the state flag still drives the disabled UI.
    if (sendingRef.current) return;
    sendingRef.current = true;
    const req = ++reqRef.current;
    setSending(true);
    setHint(null);
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
    // Scanned only at settle, never mid-stream: the handle is the last thing
    // an answer contains, and a file row appearing inside the live region
    // while the text is still arriving reads as a glitch.
    const settle = (meta: string) => {
      if (queued) { cancelAnimationFrame(queued); queued = 0; }
      setMsgs((m) => [...m.filter((x) => x.id !== bubbleId), {
        id: bubbleId, who: 'aria', html: renderMarkdown(acc || '…'), meta,
        arts: artRefs(acc),
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
            id: nextId(), who: 'err', text: humanError((ev as { text?: string }).text),
          }]);
        }
      }
    } catch (e) {
      if (!mountedRef.current || reqRef.current !== req) return;
      if ((e as Error).name === 'AbortError') {
        // Stopping mid-answer must not throw away what already streamed:
        // the gateway call was billed, so the user keeps the partial text.
        if (acc.trim()) settle(`stopped after ${((Date.now() - t0) / 1000).toFixed(1)}s · partial answer`);
        /* A user-initiated Stop is not a failure. It was rendered as
           who:'err' — a red bubble whose author reads literally "Error" and
           whose text says "stopped before any answer arrived", for a
           deliberate action that simply had nothing to show. Say what
           happened, in the normal voice. */
        else setMsgs((m) => [...m.filter((x) => x.id !== bubbleId), {
          id: nextId(), who: 'aria', text: 'Stopped.' }]);
      } else {
        const msg = String((e as Error).message || e);
        /* The composer was emptied when the turn started, so on a length
           rejection the text would be stranded in a bubble having reached
           nobody. Put it back: this failure is the one case where the turn
           provably never got to the model. */
        if (isTooLong(msg)) setInput(q);
        setMsgs((m) => [...m.filter((x) => x.id !== bubbleId), {
          id: nextId(), who: 'err', text: humanError(msg, q.length),
        }]);
      }
    } finally {
      // The busy flag and the controller must clear even when this turn was
      // superseded (thread switch, New chat). Guarding them on `req` left
      // Send disabled and `stop` a no-op forever — a wedged composer.
      if (abortRef.current === ac) abortRef.current = null;
      // The ref mirror clears even when unmounted — a stale `true` here
      // would wedge the composer after a thread switch.
      sendingRef.current = false;
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
    setHint(null);
    sendingRef.current = false;
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
      // Appended, not assigned: this used to replace the whole transcript with
      // the failure, so a delete that did not work threw away the conversation
      // the user was reading.
      setMsgs((m) => [...m, {
        id: nextId(), who: 'err',
        text: `could not delete thread: ${e instanceof Error ? e.message : String(e)}`,
      }]);
      setDropping('');
      return;
    }
    if (id === cid) {
      setCid('');
      setMsgs([]);
      setCrumb('Chat');
    }
    // Out of the list here as well as on the server, so the poll's page-0
    // merge cannot bring a deleted row back from the paged-in tail.
    setThreads((prev) => prev.filter((t) => t.conversation_id !== id));
    setDropping('');
    refresh();
  };

  const vis = threads.filter((t) => !filter || (t.title || '').toLowerCase().includes(filter.toLowerCase()));

  /* Only the bubble streaming right now is a live region. Every aria
     bubble used to carry aria-live, so a screen reader treated the
     whole transcript as a stack of regions to re-announce whenever
     any of them re-rendered. */
  const streamingId = sending && msgs.length ? msgs[msgs.length - 1].id : null;

  /* Never a fallback number. Unread is "tools unknown", and switched off is
     "no tools" — both honest, both distinguishable from a real count. */
  const toolLabel = toolsOn === false
    ? 'no tools'
    : toolCount === null ? 'tools unknown' : `${toolCount} tool${toolCount === 1 ? '' : 's'}`;
  return (
    <div className="flex h-full flex-col lg:flex-row">
      <SkipLink />
      <Rail />
      <div className="flex w-full max-h-[34vh] flex-none flex-col border-b border-white/10 bg-[#0b0b0e] lg:max-h-none lg:w-[248px] lg:border-b-0 lg:border-r">
          <div className="flex items-center justify-between px-3.5 pb-2 pt-3.5 text-xs font-bold tracking-wide">
            Threads
          {/* "showing 50 of 65", not a bare 50 that reads as a total. */}
          <span className="rounded-full border border-white/10 bg-white/5 px-2.5 py-0.5 text-[10.5px] font-bold text-zinc-400">
            {total > threads.length ? `${threads.length} of ${total}` : threads.length}
          </span>
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
          {threads.length < total && (
            <button
              onClick={loadMore}
              disabled={moreLoading}
              className="w-full rounded-lg border border-white/10 bg-white/5 px-2.5 py-1.5 text-[11px] text-zinc-300 hover:border-violet-400 disabled:opacity-50"
            >
              {moreLoading ? 'loading…' : `load ${Math.min(PAGE, total - threads.length)} older threads (${total - threads.length} hidden)`}
            </button>
          )}
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
      <main id="main" tabIndex={-1} className="flex min-h-0 min-w-0 flex-1 flex-col outline-none">
        <TopBar crumb={crumb}>
          {/* What chat can reach, counted from the server's own chat toolset
              rather than asserted here. It used to read a hardcoded
              "21 tools · no pipeline", which would keep claiming 21 after
              the next tool landed. */}
          {/* Pill takes no title, so the provenance rides on a wrapper. */}
          <span
            title={`Tools chat can call, counted from /api/capabilities${
              toolsOn === false ? ' — tool calling is switched off (chat.tools)' : ''}`}
          >
            <Pill tone={toolsOn === false ? 'warn' : 'muted'}>
              {toolLabel} · no pipeline
            </Pill>
          </span>
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
          <button onClick={() => { refresh(); loadCaps(); }} title="Refresh" aria-label="Refresh threads" className="rounded-md border border-white/15 bg-white/5 px-2.5 py-1.5 hover:border-violet-400"><RefreshCw size={14} /></button>
          <button onClick={fresh} title="New chat" aria-label="Start new chat" className="rounded-md border border-white/15 bg-white/5 px-2.5 py-1.5 hover:border-violet-400"><Plus size={14} /></button>
        </TopBar>        <div
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
              /* An error is an error, not something Aria said. role=alert
                 announces it as one; without it a rejected turn was read out
                 in the transcript's own voice, in the middle of a
                 conversation. */
              role={m.who === 'err' ? 'alert' : undefined}
              aria-live={m.who === 'aria' && streamingId === m.id ? 'polite' : undefined}
              aria-atomic={m.who === 'aria' && streamingId === m.id ? 'false' : undefined}
              className={`max-w-[720px] rounded-[10px] border p-4 ${m.who === 'you' ? 'ml-auto border-violet-400/30 bg-violet-400/[0.07]' : m.who === 'err' ? 'border-red-400/40 bg-red-400/5' : 'border-white/10 bg-[#0e0e12]'}`}
            >
              <div className="mb-1 text-xs font-bold text-violet-300">{m.who === 'you' ? 'You' : m.who === 'err' ? 'Error' : 'Aria'}</div>
              {m.html
                ? <div className="prose-chat space-y-2 text-[13px] leading-relaxed text-zinc-200" dangerouslySetInnerHTML={{ __html: m.html }} />
                : <p className="whitespace-pre-wrap break-words text-[13px] leading-relaxed text-zinc-200">{m.text}</p>}
              {/* A document this run rendered. It used to exist only as prose:
                  the model says "you can download it here", the markdown
                  sanitiser keeps http(s) links only, so the handle in that
                  sentence was dropped on the floor and a real 4,698-byte PDF
                  sat in the artifact store with no row, no preview and no
                  download. */}
              {m.who === 'aria' && !!m.arts?.length && (
                <div className="mt-3 space-y-2">
                  {m.arts.map((a) => (
                    <ArtRow
                      key={a.id}
                      art={a.id}
                      label={a.label}
                      state={arts[a.id] || { status: 'pending' }}                    />
                  ))}
                </div>
              )}
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
        </div>        <div className="border-t border-white/10 p-3">
          <div className="flex items-end gap-2">
            <textarea
              value={input}
              /* No maxLength, deliberately. It was the whole bug: the browser
                 clamped the value at 4000, so a 5000-character paste lost 1000
                 characters with no indication at all, and `overLimit` — which
                 is what turns the counter red — could never be true, so the
                 counter showed a reassuring amber "4000 / 4000" for text that
                 was already truncated. Nothing is clamped now: the composer
                 shows the real length, refuses the send, and says why. The
                 server's 400 is still the backstop for any path that sets the
                 value without typing it. */
              onChange={(e) => { setInput(e.target.value); setHint(null); }}
              onKeyDown={(e) => {
                // Enter while an IME is composing confirms the candidate, not
                // the message — sending there posts half-composed text.
                if (e.nativeEvent.isComposing || (e.nativeEvent as unknown as { keyCode?: number }).keyCode === 229) return;
                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
              }}
              placeholder="Message Aria… (Enter to send, Shift+Enter for newline)"
              aria-label="Message Aria"
              aria-describedby="chat-len chat-help"
              aria-invalid={overLimit || undefined}
              rows={2}
              className="min-h-[44px] flex-1 resize-y rounded-lg border border-white/10 bg-black/40 p-2.5 text-[13px] outline-none focus:border-violet-400 aria-invalid:border-red-400/70"
            />
            {/* Not disabled while over the limit: a disabled Send is a dead
                key that explains nothing. send() refuses it and states the
                reason, so clicking and pressing Enter both say the same
                thing. */}
            <button onClick={() => send()} disabled={sending} aria-label="Send message" className="flex items-center gap-1.5 self-end rounded-md bg-violet-400 px-4 py-2.5 text-xs font-bold text-[#0b0b0e] shadow-[0_0_16px_rgba(139,124,246,0.35)] hover:brightness-110 disabled:opacity-50">
              <Send size={13} /> Send
            </button>
          </div>
          <div className="mt-1.5 flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
            {/* The limit, stated once, read out when the composer takes focus.
                The live counter below is deliberately NOT a live region: it
                changes on every keystroke, and a polite announcement per
                character is noise. */}
            <span id="chat-help" className="sr-only">
              Up to 4000 characters. Longer than that cannot be sent.
            </span>
            {hint && (
              <span
                role="status"
                className={`min-w-0 text-[11.5px] ${hint.tone === 'err' ? 'text-red-300' : 'text-amber-300'}`}
              >
                {hint.text}
              </span>
            )}
            <span
              id="chat-len"
              className={`ml-auto tabular-nums text-[11px] ${overLimit ? 'text-red-300' : nearLimit ? 'text-amber-300' : 'text-zinc-muted'}`}
            >
              {overLimit
                ? `${input.length.toLocaleString()} / ${CHAT_MAX_QUERY.toLocaleString()}`
                  + ` · ${(input.length - CHAT_MAX_QUERY).toLocaleString()} over`
                : nearLimit
                  ? `${input.length.toLocaleString()} / ${CHAT_MAX_QUERY.toLocaleString()}`
                  : ''}
            </span>
          </div>
        </div>
      </main>
    </div>
  );
}