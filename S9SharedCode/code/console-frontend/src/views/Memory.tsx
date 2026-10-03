import { useCallback, useEffect, useRef, useState } from 'react';
import { Database } from 'lucide-react';
import { Rail, TopBar, Empty, Skel, Pill } from '../components/ui';
import { api, type MemItem } from '../api';

/* The four memory types the agent actually works with. This list used to be
   the seven gateway DRAWERS (working / episode / fact / playbook / policy /
   audit / document), which is the storage cabinet — permissions and lifetime
   — not what "a type of memory" means to a user. That mismatch is why a
   preference was invisible: KIND_TO_DRAWER routes `preference` into the
   `fact` drawer, so it sat among plain facts with no way to isolate it.
   `kinds` is the axis that answers "what kind of memory is this?", so the
   panel filters on that and leaves the cabinet to the storage layer.
   Adding a type later means adding a row here and a kind to the gateway's
   MemoryKind vocabulary. */
const KINDS = [
  { id: 'fact', label: 'facts', sub: 'things learned about the world' },
  { id: 'preference', label: 'preferences', sub: 'how you like things done' },
  { id: 'tool_outcome', label: 'tool outcomes', sub: 'what worked and what did not' },
  { id: 'scratchpad', label: 'scratchpad', sub: 'working notes for this run' },
];

export default function Memory() {
  /* '' means no kind filter (all memory). */
  const [kindFilter, setKindFilter] = useState('');
  const [items, setItems] = useState<MemItem[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [q, setQ] = useState('');
  const [submitted, setSubmitted] = useState('');
  const [hide, setHide] = useState(true);
  const [kind, setKind] = useState('fact');
  const [desc, setDesc] = useState('');
  const [kw, setKw] = useState('');
  const [sess, setSess] = useState('');
  // Blocks double-submit on Remember/Wipe (a double-click duplicated a
  // memory or fired two wipes).
  const [busy, setBusy] = useState(false);
  const [out, setOut] = useState('');
  const reqRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);

  // Fetch on submit/scope change only — typing must not refetch (the old
  // version fired a gateway recall per keystroke and reset nothing).
  const load = useCallback(async () => {
    const req = ++reqRef.current;
    abortRef.current?.abort();
    const ctl = new AbortController();
    abortRef.current = ctl;
    const params = new URLSearchParams({ limit: '60' });
    if (submitted) params.set('q', submitted);
    /* Filter by kind, not drawer: the four types are the vocabulary the agent
       works with, and `preference` is only isolatable this way because it is
       stored in the `fact` drawer. */
    if (kindFilter) params.set('kinds', kindFilter);
    if (hide) params.set('hide_superseded', 'true');
    try {
      /* One client for every request: the hand-rolled fetch duplicated the
         parsing and error handling inline and already diverged (it cleared
         items on the error path where api.memory's contract does not). */
      const d = await api.memory(params, ctl.signal);
      if (reqRef.current !== req || ctl.signal.aborted) return;
      if (d.error) setOut(d.error);
      setItems(d.items || []);
      setLoaded(true);
      setLoadError('');
    } catch (e) {
      if (ctl.signal.aborted || reqRef.current !== req) return;
      setLoaded(true);
      setLoadError(e instanceof Error ? e.message : String(e));
    }
  }, [submitted, kindFilter, hide]);

  useEffect(() => {
    load();
    return () => { abortRef.current?.abort(); };
  }, [load]);

  const submit = () => setSubmitted(q.trim());

  const pickKind = (id: string) => {
    setKindFilter(id);
    setQ('');
    setSubmitted('');
  };

  const remember = async () => {
    if (busy) return;
    if (!desc.trim()) { setOut('descriptor required.'); return; }
    setBusy(true);
    setOut('saving…');
    try {
      const r = await api.remember({
        kind, descriptor: desc.trim(),
        keywords: kw.split(',').map((s) => s.trim()).filter(Boolean),
        value: {}, source: 'dashboard', run_id: `dashboard-${Date.now()}`,
      });
      if (r.status === 'ok') {
        setOut(`remembered${r.id ? ` ${r.id}` : ''}`);
        setDesc('');
        load();
      } else setOut('save failed');
    } catch (e) { setOut(String((e as Error).message || e)); }
    finally { setBusy(false); }
  };

  const remove = async (id: string, descriptor: string) => {
    if (busy || !id) return;
    if (!confirm(`Forget this memory?\n\n${descriptor.slice(0, 160)}`)) return;
    setBusy(true);
    setOut('deleting…');
    try {
      await api.deleteMemory(id);
      setOut(`deleted ${id}`);
      load();
    } catch (e) { setOut(String((e as Error).message || e)); }
    finally { setBusy(false); }
  };

  const wipe = async () => {
    if (busy) return;
    if (!sess.trim()) { setOut('session id required.'); return; }
    if (!confirm(`Wipe gateway memory for session ${sess.trim()}?`)) return;
    setBusy(true);
    setOut('wiping…');
    try {
      const r = await api.wipeMemory(sess.trim());
      setOut(`wiped: ${JSON.stringify(r).slice(0, 160)}`);
      load();
    } catch (e) { setOut(String((e as Error).message || e)); }
    finally { setBusy(false); }
  };

  return (
    <div className="flex h-full flex-col lg:flex-row">
      <Rail />
      <div className="flex w-full max-h-[34vh] flex-none flex-col border-b border-white/10 bg-[#0b0b0e] lg:max-h-none lg:w-[248px] lg:border-b-0 lg:border-r">
        <div className="flex items-center justify-between px-3.5 pb-2 pt-3.5 text-xs font-bold tracking-wide">
          Memory types
          <span className="rounded-full border border-white/10 bg-white/5 px-2.5 py-0.5 text-[10.5px] font-bold text-zinc-400">{KINDS.length + 1}</span>
        </div>
        {/* ONE scroll container for the whole list. The kind list used to sit
            in a fixed block above a separately-scrolling drawer list, which
            squeezed the scroll area to a few pixels on a phone — the list
            appeared not to scroll at all. */}
        <div className="min-h-0 flex-1 space-y-2 overflow-y-auto px-2.5 pb-3">
          {[{ id: '', label: 'all memory', sub: 'search everything' },
            ...KINDS].map((k) => (
            <button
              key={k.id || 'all'}
              onClick={() => pickKind(k.id)}
              className={`block w-full rounded-[10px] border p-2.5 text-left ${kindFilter === k.id ? 'border-violet-400/60 bg-violet-400/5' : 'border-white/10 bg-[#0e0e12] hover:border-white/25'}`}
            >
              <div className="truncate text-[12.5px] font-semibold">{k.label}</div>
              <div className="mt-1 text-[11px] text-zinc-muted">{k.sub}</div>
            </button>
          ))}
        </div>
      </div>
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <TopBar crumb="Memory">
          <input
            value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') submit(); }}
            placeholder="Search memory…" aria-label="Search memory"
            className="w-44 rounded-md border border-white/10 bg-black/40 px-2.5 py-1.5 text-xs outline-none focus:border-violet-400"
          />
          <label className="flex items-center gap-1.5 text-[11px] text-zinc-400">
            <input type="checkbox" checked={hide} onChange={(e) => setHide(e.target.checked)} className="accent-violet-400" />
            hide superseded
          </label>
          <button onClick={submit} aria-label="Run memory search" className="rounded-md border border-white/15 bg-white/5 px-3.5 py-1.5 text-xs hover:border-violet-400">Search</button>
        </TopBar>
        <div className="mx-3.5 mt-3 rounded-[10px] border border-white/10 bg-[#0e0e12] p-3.5">
          <div className="mb-1 text-[10px] font-bold tracking-[0.12em] text-zinc-muted">REMEMBER</div>
          <div className="flex flex-wrap items-end gap-2.5">
            <label className="text-[11px] text-zinc-muted">kind<br />
              <select value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Memory kind" className="mt-1 rounded-md border border-white/10 bg-black/40 px-2 py-1.5 text-xs outline-none">
                <option>fact</option><option>preference</option><option>tool_outcome</option><option>scratchpad</option>
              </select>
            </label>
            <label className="min-w-[220px] flex-1 text-[11px] text-zinc-muted">descriptor<br />
              <input value={desc} onChange={(e) => setDesc(e.target.value)} placeholder="one short human-readable line" aria-label="Memory descriptor" className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-2.5 py-1.5 text-xs outline-none focus:border-violet-400" />
            </label>
            <label className="text-[11px] text-zinc-muted">keywords<br />
              <input value={kw} onChange={(e) => setKw(e.target.value)} placeholder="a, b" aria-label="Memory keywords" className="mt-1 w-36 rounded-md border border-white/10 bg-black/40 px-2.5 py-1.5 text-xs outline-none focus:border-violet-400" />
            </label>
            <button onClick={remember} disabled={busy} aria-label="Remember this fact" className="rounded-md bg-violet-400 px-4 py-2 text-xs font-bold text-[#0b0b0e] disabled:opacity-50 hover:brightness-110">{busy ? 'Saving…' : 'Remember'}</button>
            <label className="text-[11px] text-zinc-muted">wipe session<br />
              <span className="mt-1 flex gap-1.5">
                <input value={sess} onChange={(e) => setSess(e.target.value)} placeholder="session id" aria-label="Session id to wipe" className="w-32 rounded-md border border-white/10 bg-black/40 px-2.5 py-1.5 text-xs outline-none focus:border-violet-400" />
                <button onClick={wipe} disabled={busy} aria-label="Wipe session memory" className="rounded-md border border-white/15 bg-white/5 px-3 py-1.5 text-xs hover:border-red-400 hover:text-red-300 disabled:opacity-50">Wipe</button>
              </span>
            </label>
          </div>
          {out && <div className="mt-2 text-[11px] text-zinc-muted" role="status">{out}</div>}
        </div>
        <div className="min-h-0 flex-1 space-y-2.5 overflow-y-auto p-3.5">
          {!loaded && <Skel />}
          {loaded && loadError && !items.length && (
            <div className="rounded-lg border border-red-400/30 bg-red-400/5 p-3 text-center text-xs text-red-200">
              couldn't load memory ({loadError}). Gateway recall may be slow — <button onClick={load} className="underline">retry</button>
            </div>
          )}
          {loaded && !loadError && !items.length && <Empty icon={<Database size={30} />} title="Nothing stored here" sub="Write a memory above, pick another type, or search." />}
          {items.map((it) => (
            <section key={it.id || `${it.kind}-${it.descriptor}`} className="rounded-[10px] border border-white/10 bg-[#0e0e12]">
              <div className="flex items-center gap-2 border-b border-white/10 px-3.5 py-2.5">
                {it.superseded_by ? <Pill tone="err">superseded</Pill> : <Pill tone="ok">{it.kind || '?'}</Pill>}
                <span className="flex-1" />
                {it.id && (
                  <button
                    onClick={() => remove(it.id!, it.descriptor || '')}
                    disabled={busy}
                    aria-label={`Forget memory ${it.id}`}
                    title="Forget this memory"
                    className="rounded border border-white/15 bg-white/5 px-2 py-0.5 text-[10.5px] font-bold text-zinc-400 hover:border-red-400/50 hover:text-red-300 disabled:opacity-40"
                  >
                    forget
                  </button>
                )}
                <span className="font-mono text-[11px] text-zinc-muted">{(it.id || '').slice(0, 18)}</span>
              </div>
              <div className="px-3.5 py-2.5 text-[12.5px] text-zinc-200">{it.descriptor || '(no descriptor)'}</div>
              <div className="flex flex-wrap gap-1.5 px-3.5 pb-2.5">
                {(it.keywords || []).map((k) => (
                  <span key={k} className="rounded border border-white/10 bg-white/5 px-2 py-0.5 font-mono text-[10.5px] text-zinc-400">{k}</span>
                ))}
                {it.source && <span className="text-[11px] text-zinc-muted">{it.source}</span>}
              </div>
            </section>
          ))}
        </div>
      </div>
    </div>
  );
}
