import { useCallback, useEffect, useRef, useState } from 'react';
import { Database, SearchX } from 'lucide-react';
import { Rail, TopBar, Empty, Skel, Pill, SkipLink } from '../components/ui';
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

/* The gateway truncates a stored descriptor at 200 characters and a keyword
   list at 8, and reports neither fact. Measured on the live store: 139 of the
   first 200 records came back at exactly 200 chars (cut mid-word, no
   ellipsis) and 151 carried the full 8 keywords, so "silently short" was the
   common case, not the edge case. These caps let the card say so instead of
   passing a severed string off as the whole memory. The server side is another
   file's; this only makes the damage visible. */
const DESC_CAP = 200;
const KW_CAP = 8;

/** Storage handles are not a user-facing concept. `mem:93129b1c` was
 *  printed under every card. Keep a short tail for support/debugging, in a
 *  colour that reads as metadata rather than as an id field. */
function shortId(id?: string): string {
  if (!id) return '';
  const tail = id.replace(/^mem:/, '').slice(0, 8);
  return `#${tail}`;
}

/** Make a descriptor presentable. A tool_outcome descriptor is a raw log line
 *  like `render_document({...}) -> 247 chars: {"ok": true, "artifact":
 *  "art:5a19…"}`. Printed verbatim it leaked artifact handles on 51 of 500
 *  cards, and the 200-char server cap cut it mid-JSON so it read as broken.
 *  Keep the tool call and its outcome, drop the raw payload. */
function readable(desc?: string): string {
  const d = (desc || '').trim();
  if (!d) return '(no descriptor)';
  /* `render_document({"blocks": …}) -> 247 chars: {"ok": true, "artifact":
     "art:5a19…"}`. Keep the call and its size; NEVER the payload. An earlier
     version appended the body when it did not end in "}" — which is exactly
     the truncated case, so every cut-off descriptor re-printed the raw
     JSON, leaking artifact handles on 51 of 500 cards. */
  const call = /^([A-Za-z_][\w.]*)\(([\s\S]*?)\)\s*->\s*([^:{]*)/.exec(d);
  if (call) {
    const [, name, args, size] = call;
    const argHead = args.length > 70 ? `${args.slice(0, 67)}...` : args;
    const tail = size.trim();
    return `${name}(${argHead})${tail ? ` → ${tail}` : ''}`;
  }
  return d;
}

/** True when the descriptor hit the gateway's 200-char cap, so the text on
 *  screen is a prefix rather than the whole memory. */
function cutOff(desc?: string): boolean {
  return (desc || '').trim().length >= DESC_CAP;
}

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
  /* The write form is a disclosure below lg. It used to be permanently open
     and sat between the top bar and the list, and with the kind panel above it
     the list was left 119px tall on a 390x844 phone — about one card — while
     10,210px of cards scrolled behind it. */
  const [formOpen, setFormOpen] = useState(false);
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
      /* A load failure used to go into `out`, the same status line as
         remember/forget/wipe, and a later good load never cleared it: "memory
         unavailable: partial failure" then sat under the Remember button and
         read like a failed save. Load problems now live in their own region,
         and every success overwrites them. */
      setLoadError(d.error || '');
      setItems(d.items || []);
      setLoaded(true);
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
  const clearSearch = () => { setQ(''); setSubmitted(''); };

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

  const remove = async (it: MemItem) => {
    const id = it.id;
    if (busy || !id) return;
    /* Forget is the one irreversible control here: no audit trail, no undo.
       The confirm names the drawer and the id so the user is confirming a
       specific record, not "the top one". Deliberately still one small
       low-contrast button per card — this must not get easier to hit. */
    const drawer = it.drawer || 'unknown drawer';
    if (!confirm(
      `Forget this memory?\n\n`
      + `id: ${id}\nkind: ${it.kind || 'unknown'}\ndrawer: ${drawer}\n\n`
      + `${(it.descriptor || '(no descriptor)').slice(0, 160)}`
      + `${cutOff(it.descriptor) ? '…' : ''}\n\n`
      + 'It is deleted immediately. There is no undo and no audit entry.',
    )) return;
    setBusy(true);
    setOut('deleting…');
    try {
      await api.deleteMemory(id);
      setOut(`deleted ${id}`);
      /* Drop the row optimistically: the refetch below can fail
         (gateway hiccup), and a successful delete must never
         leave a ghost row on screen because of it. */
      setItems((prev) => prev.filter((m) => m.id !== id));
      load();
    } catch (e) { setOut(String((e as Error).message || e)); }
    finally { setBusy(false); }
  };

  const wipe = async () => {
    if (busy) return;
    if (!sess.trim()) { setOut('session id required.'); return; }
    if (!confirm(
      `Wipe gateway memory for session ${sess.trim()}?\n\n`
      + 'Every drawer for that session is deleted. There is no undo; the '
      + 'audit drawer is kept.',
    )) return;
    setBusy(true);
    setOut('wiping…');
    try {
      const r = await api.wipeMemory(sess.trim());
      /* The gateway nests the per-drawer counts under `result`
         ({status, result:{legacy, working, ..., audit_kept}}).
         The old check ran Object.entries() on the TOP-level object, so
         it saw the string "ok" and one nested object, never a number:
         `cleared` was always false and EVERY successful wipe reported
         "nothing to wipe" even though the rows were really deleted.
         Read the nested counts, and fall back to the flat shape if a
         deployment ever returns it. */
      const counts = (r && typeof r.result === 'object' && r.result)
        ? r.result
        : (r as unknown as Record<string, unknown>);
      const cleared = Object.entries(counts).some(
        ([k, v]) => k !== 'audit_kept' && typeof v === 'number' && v > 0);
      setOut(cleared
        ? `wiped: ${JSON.stringify(counts).slice(0, 160)}`
        : `nothing to wipe for session ${sess.trim().slice(0, 24)}`);
      load();
    } catch (e) { setOut(String((e as Error).message || e)); }
    finally { setBusy(false); }
  };

  /* Announced once per result set, not per token — the list is not a stream
     and re-announcing each render would make a screen reader unusable. */
  const statusText = !loaded
    ? 'loading memory'
    : submitted
      ? `${items.length} nearest items for ${submitted}`
      : `${items.length} memory items shown`;

  return (
    <div className="flex h-full flex-col lg:flex-row">
      <SkipLink />
      {/* The rail is a horizontally scrolling icon strip below lg and 5 of its
          14 items sat past the 390px edge (Authoring@392 … Settings@572) with
          nothing to suggest the swipe. A fade at the trailing edge plus a
          visible scrollbar is the affordance; `lg:contents` keeps the desktop
          two-column flex layout exactly as it was. */}
      <div className="relative flex-none lg:contents
                      [&_aside::-webkit-scrollbar]:h-2
                      [&_aside::-webkit-scrollbar-thumb]:bg-white/25
                      [&_nav::-webkit-scrollbar]:h-2
                      [&_nav::-webkit-scrollbar-thumb]:bg-white/25">
        <Rail />
        {/* The fade alone is a weak cue on a dark strip, and the scrollbar
            itself is 0px tall on overlay-scrollbar platforms, so name the
            gesture: "swipe \u2192" sits in the fade at the trailing edge. */}
        <div aria-hidden className="pointer-events-none absolute inset-y-0 right-0 z-10 flex w-14 items-center justify-end bg-gradient-to-l from-[#0b0b0e] to-transparent pr-0.5 text-[9px] font-bold tracking-[0.1em] text-zinc-500 lg:hidden">
          swipe &rarr;
        </div>
      </div>
      {/* Kind panel: a wrapping chip row on phones (2 short rows instead of a
          34vh block), the 248px column from lg up. */}
      <div className="flex w-full flex-none flex-col border-b border-white/10 bg-[#0b0b0e] lg:h-full lg:w-[248px] lg:flex-none lg:border-b-0 lg:border-r">
        <div className="flex items-center justify-between px-3.5 pb-2 pt-3 text-xs font-bold tracking-wide">
          <h2 className="text-xs font-bold tracking-wide">Memory types</h2>
          {/* this badge counted filter buttons, not memories: 4 kinds + "all"
              rendered as "5" under the heading "Memory types", which reads as
              "5 memories stored". the API returns no per-kind counts and no
              total, so it cannot show real ones — say what it is instead. */}
          <span
            className="rounded-full border border-white/10 bg-white/5 px-2.5 py-0.5 text-[10.5px] font-bold text-zinc-400"
            title="This is the number of type filters, not the number of stored memories"
          >
            {KINDS.length + 1} filters
          </span>
        </div>
        <p className="hidden px-3.5 pb-2 text-[10.5px] leading-snug text-zinc-muted lg:block">
          the gateway does not report how many memories each type holds, so
          these filters carry no counts
        </p>
        {/* ONE scroll container for the whole list. The kind list used to sit
            in a fixed block above a separately-scrolling drawer list, which
            squeezed the scroll area to a few pixels on a phone — the list
            appeared not to scroll at all. */}
        <div className="flex flex-wrap gap-2 px-2.5 pb-3 lg:block lg:min-h-0 lg:flex-1 lg:space-y-2 lg:overflow-y-auto">
          {[{ id: '', label: 'all memory', sub: 'search everything' },
            ...KINDS].map((k) => {
            const on = kindFilter === k.id;
            return (
            <button
              key={k.id || 'all'}
              onClick={() => pickKind(k.id)}
              aria-pressed={on}
              className={`block w-full rounded-[10px] border p-2.5 text-left max-lg:w-auto max-lg:flex-none max-lg:whitespace-nowrap max-lg:py-1.5 ${on ? 'border-violet-400/60 bg-violet-400/5' : 'border-white/10 bg-[#0e0e12] hover:border-white/25'}`}
            >
              <div className="flex items-center text-[12.5px] font-semibold">
                {/* selection is not colour-only: aria-pressed covers screen
                    readers, this dot covers everyone else. */}
                <span aria-hidden className={`mr-1.5 h-1.5 w-1.5 flex-none rounded-full ${on ? 'bg-violet-400' : 'bg-transparent'}`} />
                <span className="truncate">{k.label}</span>
              </div>
              <div className="mt-1 hidden text-[11px] text-zinc-muted lg:block">{k.sub}</div>
            </button>
            );
          })}
        </div>
      </div>
      <main id="main" tabIndex={-1} className="flex min-h-0 min-w-0 flex-1 flex-col outline-none">
        <h1 className="sr-only">Memory</h1>
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
        {/* Result provenance. `api.memory` returns the 60 nearest items and no
            distance or score field (checked against the live endpoint), so
            there is no relevance floor to apply and no per-item ranking to
            show. State what the list is and name the query that produced it,
            otherwise "60 cards came back" reads as "60 things matched". */}
        <div className="mx-3.5 mt-2.5 flex flex-wrap items-baseline gap-x-2 gap-y-1 rounded-[10px] border border-white/10 bg-[#0e0e12] px-3 py-2">
          {submitted ? (<>
            <span className="text-[11px] font-bold tracking-[0.12em] text-zinc-muted">SEARCH</span>
            <span className="min-w-0 max-w-full truncate font-mono text-[11.5px] text-zinc-200" title={submitted}>{submitted}</span>
            <span className="text-[11.5px] text-zinc-300">
              <span className="font-bold">{items.length}</span> nearest stored {items.length === 1 ? 'item' : 'items'}
            </span>
            <span className="text-[11px] leading-snug text-zinc-muted">
              semantic recall, not a text match: no score is returned, so these
              are simply the {items.length} closest records — a query with no
              real meaning still returns {items.length} of them
            </span>
            <button
              onClick={clearSearch}
              className="ml-auto rounded border border-white/15 bg-white/5 px-2 py-0.5 text-[10.5px] font-bold text-zinc-300 hover:border-violet-400"
            >
              clear search
            </button>
          </>) : (<>
            <span className="text-[11px] font-bold tracking-[0.12em] text-zinc-muted">MEMORY</span>
            <span className="text-[11.5px] text-zinc-300">
              <span className="font-bold">{items.length}</span> items
              {kindFilter ? ` of type ${kindFilter}` : ' across all types'}
              {' · '}newest first, up to 60 per fetch
            </span>
          </>)}
        </div>
        <p className="sr-only" role="status" aria-live="polite">{statusText}</p>
        {/* Load failures get their own region, above the list, so they are
            never mistaken for the result of a Remember/Forget/Wipe and are
            cleared by the next successful load. */}
        {loadError && (
          <div role="alert" className="mx-3.5 mt-2 flex flex-wrap items-center gap-2 rounded-[10px] border border-red-400/30 bg-red-400/5 px-3 py-2 text-[11.5px] text-red-200">
            <span className="min-w-0 flex-1">couldn't load memory — {loadError}</span>
            <button onClick={load} className="rounded border border-red-400/40 px-2 py-0.5 text-[10.5px] font-bold underline">retry</button>
          </div>
        )}
        {/* write form: a disclosure below lg so the list gets the viewport */}
        <h2 className="mx-3.5 mt-2.5 lg:hidden">
          <button
            onClick={() => setFormOpen((o) => !o)}
            aria-expanded={formOpen}
            aria-controls="remember-form"
            className="flex w-full items-center justify-between rounded-[10px] border border-white/10 bg-[#0e0e12] px-3.5 py-2.5 text-[10px] font-bold tracking-[0.12em] text-zinc-300"
          >
            REMEMBER
            <span className="text-[10.5px] font-medium normal-case tracking-normal text-zinc-muted">
              {formOpen ? 'hide' : 'write a memory'}
            </span>
          </button>
        </h2>
        <div id="remember-form" className={`${formOpen ? 'block' : 'hidden'} lg:block`}>
          <div className="mx-3.5 mt-2.5 rounded-[10px] border border-white/10 bg-[#0e0e12] p-3.5">
            <div className="mb-1 hidden text-[10px] font-bold tracking-[0.12em] text-zinc-muted lg:block">REMEMBER</div>
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
                  <input value={sess} onChange={(e) => setSess(e.target.value)} placeholder="session id" aria-label="Session id to wipe" className="w-32 rounded-md border border-white/10 bg-black/40 px-2 py-1.5 text-xs outline-none focus:border-violet-400" />
                  <button onClick={wipe} disabled={busy} aria-label="Wipe session memory" className="rounded-md border border-white/15 bg-white/5 px-3 py-1.5 text-xs hover:border-red-400 hover:text-red-300 disabled:opacity-50">Wipe</button>
                </span>
              </label>
            </div>
            {out && <div className="mt-2 text-[11px] text-zinc-muted" role="status">{out}</div>}
          </div>
        </div>
        <div className="min-h-0 flex-1 space-y-2.5 overflow-y-auto p-3.5">
          {!loaded && <Skel />}
          {loaded && !loadError && !items.length && submitted && (
            /* An empty list behind a query is a search result, not a lack of
               memories: the old copy ("Nothing stored here — write a memory
               above") told the user to create memories after a search that
               matched nothing. Offer the way back to the full list instead. */
            <>
              <Empty
                icon={<SearchX size={30} />}
                title="No memories for that search"
                sub={`Nothing came back for "${submitted.slice(0, 120)}". Semantic recall usually returns the 60 nearest records whatever the query, so an empty list here means the store has nothing for this type filter — not that your query failed.`}
              />
              <div className="-mt-6 flex justify-center">
                <button
                  onClick={clearSearch}
                  className="rounded-md border border-white/15 bg-white/5 px-3 py-1.5 text-xs font-bold text-zinc-200 hover:border-violet-400"
                >
                  clear search
                </button>
              </div>
            </>
          )}
          {loaded && !loadError && !items.length && !submitted && <Empty icon={<Database size={30} />} title="Nothing stored here" sub="Write a memory with the Remember form, pick another type, or search." />}
          {items.map((it) => {
            const cut = cutOff(it.descriptor);
            const body = readable(it.descriptor);
            const kws = it.keywords || [];
            return (
            <section key={it.id || `${it.kind}-${it.descriptor}`} className="rounded-[10px] border border-white/10 bg-[#0e0e12]">
              <div className="flex items-center gap-2 border-b border-white/10 px-3.5 py-2.5">
                {it.superseded_by ? <Pill tone="err">superseded</Pill> : <Pill tone="ok">{it.kind || '?'}</Pill>}
                <span className="flex-1" />
                {it.id && (
                  <button
                    onClick={() => remove(it)}
                    disabled={busy}
                    aria-label={`Forget memory ${it.id}`}
                    title="Forget this memory"
                    className="rounded border border-white/15 bg-white/5 px-2 py-0.5 text-[10.5px] font-bold text-zinc-400 hover:border-red-400/50 hover:text-red-300 disabled:opacity-40"
                  >
                    forget
                  </button>
                )}
                <span className="font-mono text-[11px] text-zinc-600">{shortId(it.id)}</span>
              </div>
              {/* A tool_outcome descriptor is a raw JSON log line. Rendered
                  verbatim it exposed internal artifact handles on 51 of 500
                  cards (`"artifact": "art:5a193fb23cbf6079"`) and was cut
                  mid-JSON by the 200-char server cap, so it looked broken as
                  well as leaking internals. Show the readable head instead. */}
              <div className="px-3.5 py-2.5 text-[12.5px] break-words text-zinc-200">
                {body}
                {/* the cap is silent: without this a 660-char fact read as a
                    complete sentence that happened to stop. */}
                {cut && <span aria-hidden className="text-zinc-500">…</span>}
                {cut && (
                  <span className="ml-1.5 text-[10.5px] text-zinc-muted">
                    (descriptor truncated by the memory store at {DESC_CAP} characters)
                  </span>
                )}
              </div>
              <div className="flex flex-wrap items-center gap-1.5 px-3.5 pb-2.5">
                {kws.map((k) => (
                  <span key={k} className="max-w-[240px] truncate rounded border border-white/10 bg-white/5 px-2 py-0.5 font-mono text-[10.5px] text-zinc-400">{k}</span>
                ))}
                {kws.length >= KW_CAP && (
                  <span
                    className="text-[10.5px] text-zinc-muted"
                    title={`the gateway caps a stored keyword list at ${KW_CAP} and does not report the total`}
                  >
                    +more (list capped at {KW_CAP})
                  </span>
                )}
                {it.source && <span className="text-[11px] text-zinc-muted">{it.source}</span>}
              </div>
            </section>
            );
          })}
        </div>
      </main>
    </div>
  );
}