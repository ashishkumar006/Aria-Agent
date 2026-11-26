import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowLeft, ArrowRight, RefreshCw, X } from 'lucide-react';
import {
  api, type StudioBrowserEvent, type StudioBrowserShot,
  type StudioBrowserStatus,
} from '../../api';

/** The browser pane: watch it, read what it said, take the wheel.
 *
 *  Aria's browser was, until this slice, invisible in three separate ways:
 *  `headless=True` hardcoded, no surface anywhere that rendered a page, and
 *  screenshots that were written, served and put on the `done` frame for a
 *  consumer that did not exist in this console — only the legacy `web/app.js`
 *  ever looked at them. A capability nobody can see is a capability nobody
 *  trusts, and "take over" is not true of something you cannot look at.
 *
 *  So the pane has three jobs, in this order:
 *   - **live view** — CDP screencast frames streamed over SSE, newest only.
 *     If the runtime cannot screencast, the pane says so and shows the
 *     filmstrip instead of leaving an empty box to be read as a blank page;
 *   - **evidence** — the console and network the pages produced, plus every
 *     screenshot the agent filed. `dropped` is shown, because an overflowing
 *     log that looks quiet is the same lie as a truncated file that says
 *     nothing about truncating;
 *   - **takeover** — a URL bar and tab strip on the SAME browser the agent
 *     holds, so a login the agent cannot do (it has no credentials and should
 *     not have them) can be done by the person watching.
 */
export function BrowserPane({ sid, onFocusChange }: {
  sid: string;
  /** The pane holds a real Chromium. A "stop" that leaves it running has to
   *  be visible somewhere, and this is the surface that knows. */
  onFocusChange?: (running: boolean) => void;
}) {
  const [status, setStatus] = useState<StudioBrowserStatus | null>(null);
  const [view] = useState<'live' | 'console' | 'network' | 'shots'>('live');
  const [frame, setFrame] = useState<string>('');
  const [, setFrameUrl] = useState('');
  const [streamErr, setStreamErr] = useState('');
  const [, setCasting] = useState(false);
  const [events, setEvents] = useState<StudioBrowserEvent[]>([]);
  const [dropped, setDropped] = useState(0);
  const [shots, setShots] = useState<StudioBrowserShot[]>([]);
  const [more, setMore] = useState(0);
  const [urlBar, setUrlBar] = useState('');
  const [busy, setBusy] = useState(false);
  const [, setNote] = useState('');
  const [pick, setPick] = useState<StudioBrowserShot | null>(null);
  const abort = useRef<AbortController | null>(null);

  const refreshStatus = useCallback(async () => {
    try {
      const s = await api.studioBrowserStatus(sid);
      setStatus(s);
      onFocusChange?.(!!s.running);
      if (s.running && s.url && !urlBar) setUrlBar(s.url);
      return s;
    } catch (e) {
      setStatus({ running: false, error: e instanceof Error ? e.message : String(e) });
      return null;
    }
  }, [sid, urlBar, onFocusChange]);

  useEffect(() => { void refreshStatus(); }, [refreshStatus]);

  /* The live view owns its own connection, and the server ends the window
     when the page stops repainting. Reopening it here — rather than holding a
     socket forever — is what keeps an idle pane from being background load. */
  const startStream = useCallback(async () => {
    abort.current?.abort();
    const ctrl = new AbortController();
    abort.current = ctrl;
    setStreamErr('');
    try {
      for await (const f of api.studioBrowserView(sid, 50, ctrl.signal)) {
        if (ctrl.signal.aborted) return;
        const kind = String(f.type || '');
        if (kind === 'cast') setCasting(true);
        else if (kind === 'frame' && f.image) {
          setFrame(`data:image/jpeg;base64,${f.image}`);
          setFrameUrl(f.url || '');
        } else if (kind === 'unsupported') {
          setCasting(false);
          setStreamErr(status?.running
            ? 'This browser cannot be viewed live here. The agent can still file screenshots.'
            : 'No browser is open in this session yet. Type an address above to start one, '
              + 'or ask the agent to open a page — it is the same browser either way.');
        } else if (kind === 'disabled') {
          setCasting(false);
          setStreamErr('browser.tools is off, so there is no browser to show');
        } else if (kind === 'idle') {
          setCasting(false);
        } else if (kind === 'closed') {
          setCasting(false);
        }
      }
    } catch (e) {
      if (!ctrl.signal.aborted) {
        setStreamErr(e instanceof Error ? e.message : 'the live view stopped');
      }
    }
  }, [sid]);

  useEffect(() => {
    // Wait for the first status: opening the stream before it answers means
    // dialling `/view` for a browser the flag says does not exist.
    if (!status || status.disabled) return undefined;
    if (view !== 'live') { abort.current?.abort(); setCasting(false); return undefined; }
    void startStream();
    return () => abort.current?.abort();
  }, [view, startStream, status]);

  const loadEvents = useCallback(async (kind: 'console' | 'network') => {
    try {
      const r = await api.studioBrowserEvents(sid, kind, true);
      setEvents(r.events || []);
      setDropped(r.dropped || 0);
      if (r.error) setNote(r.error);
    } catch (e) {
      setEvents([]);
      setNote(e instanceof Error ? e.message : 'the page log failed');
    }
  }, [sid]);

  const loadShots = useCallback(async () => {
    try {
      const r = await api.studioBrowserShots(sid, 24);
      setShots(r.shots || []);
      setMore(r.more || 0);
    } catch { setShots([]); }
  }, [sid]);

  useEffect(() => {
    if (view === 'console') void loadEvents('console');
    if (view === 'network') void loadEvents('network');
    if (view === 'shots') void loadShots();
  }, [view, loadEvents, loadShots]);

  const navigate = useCallback(async () => {
    const url = urlBar.trim();
    if (!url) return;
    setBusy(true);
    setNote('');
    try {
      const r = await api.studioBrowserNavigate(sid, url);
      if (r.error) setNote(r.error);
      else {
        setNote(`opened ${r.url || url}${r.status ? ` · ${r.status}` : ''}`);
        await refreshStatus();
        if (view === 'shots') void loadShots();
      }
    } catch (e) {
      setNote(e instanceof Error ? e.message : 'navigation failed');
    } finally {
      setBusy(false);
    }
  }, [urlBar, sid, refreshStatus, view, loadShots]);

  const tabOp = useCallback(async (action: string, index?: number) => {
    setBusy(true);
    try {
      const r = await api.studioBrowserTab(sid, action, index);
      if (r?.error) setNote(String(r.error));
      await refreshStatus();
    } finally {
      setBusy(false);
    }
  }, [sid, refreshStatus]);

  const s = status || {};
  const tabs = (s.tabs || 0);

  /* The screencast is a picture; this is the mouse and keyboard behind it.
     Coordinates are mapped from the displayed image back to page space. */
  const sendInput = useCallback((ev: { kind: string; x?: number; y?: number; dx?: number; dy?: number; text?: string; key?: string }) => {
    void api.studioBrowserInput(sid, ev).catch(() => {});
  }, [sid]);

  const toPage = (e: React.MouseEvent<HTMLImageElement> | React.WheelEvent<HTMLImageElement>) => {
    const img = e.currentTarget;
    const rect = img.getBoundingClientRect();
    const nw = img.naturalWidth || rect.width || 1;
    const nh = img.naturalHeight || rect.height || 1;
    const scale = Math.min(rect.width / nw, rect.height / nh) || 1;
  const ox = rect.left + (rect.width - nw * scale) / 2;
    const oy = rect.top + (rect.height - nh * scale) / 2;
    const vp = (status?.viewport || {}) as { width?: number; height?: number };
    const vx = (vp.width || nw) / nw;
    const vy = (vp.height || nh) / nh;
    return { x: (e.clientX - ox) / scale * vx, y: (e.clientY - oy) / scale * vy };
  };

  /* One sentence when the feature is off. Before this the pane said it three
     times — in the live-view box, in the footer strip and above it as a
     paragraph — while still drawing a URL bar, a tab strip and a +/close pair
     that all answer `disabled`. A kill switch that leaves residue is worse than
     no kill switch, so nothing that needs a browser is rendered at all. */
  if (s.disabled) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center p-6">
        <p className="max-w-[46ch] text-center text-[12px] leading-relaxed text-zinc-muted">
          <span className="text-zinc-200">Browser tools are off.</span> The
          agent has no browser verbs, so there is nothing to watch, take over or
          prove. Switch{' '}
          <span className="font-mono text-[11px] text-zinc-300">browser.tools</span>{' '}
          on and restart the agent to give this session a Chromium.
        </p>
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col text-[12px]">
      {/* The URL bar is the takeover. It drives the same browser the agent is
          holding, which is the only reason "take over" means anything here. */}
      <div className="flex flex-none items-center gap-1 px-2 py-1.5">
        <button type="button" onClick={() => void tabOp('back')} disabled={busy}
                aria-label="Back" title="Back"
                className="flex h-6 w-6 flex-none items-center justify-center rounded-md text-zinc-400 hover:bg-white/5 hover:text-zinc-100 disabled:opacity-30">
          <ArrowLeft size={13} />
        </button>
        <button type="button" onClick={() => void tabOp('forward')} disabled={busy}
                aria-label="Forward" title="Forward"
                className="flex h-6 w-6 flex-none items-center justify-center rounded-md text-zinc-400 hover:bg-white/5 hover:text-zinc-100 disabled:opacity-30">
          <ArrowRight size={13} />
        </button>
        <button type="button" onClick={() => void tabOp('reload')} disabled={busy}
                aria-label="Reload" title="Reload"
                className="flex h-6 w-6 flex-none items-center justify-center rounded-md text-zinc-400 hover:bg-white/5 hover:text-zinc-100 disabled:opacity-30">
          <RefreshCw size={13} />
        </button>
        <input value={urlBar}
               onChange={(e) => setUrlBar(e.target.value)}
               onKeyDown={(e) => { if (e.key === 'Enter') void navigate(); }}
               placeholder="Search or enter a URL"
               aria-label="Browser address"
               className="min-w-0 flex-1 rounded-md border border-glass-border bg-surface-1 px-2 py-1 text-[12px] outline-none" />
      </div>

      {tabs > 1 && (
        <div className="flex flex-none items-center gap-1 overflow-x-auto border-b border-glass-border px-2 py-1">
          {Array.from({ length: tabs }).map((_x, i) => (
            <button key={i} type="button"
                    onClick={() => void tabOp('select', i)}
                    aria-pressed={i === (s.active || 0)}
                    className={'flex flex-none items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] '
                      + (i === (s.active || 0)
                        ? 'border-white/30 bg-white/[0.06] text-zinc-100'
                        : 'border-glass-border text-zinc-muted hover:bg-white/5')}>
              tab {i + 1}
              <span role="button" aria-label={`Close tab ${i + 1}`}
                    onClick={(e) => { e.stopPropagation(); void tabOp('close', i); }}
                    className="flex h-4 w-4 items-center justify-center rounded hover:bg-white/10 hover:text-danger"><X size={11} /></span>
            </button>
          ))}
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-auto">
        {view === 'live' && (
          <div className="relative min-h-0 flex-1 p-1">
            {frame ? (
              <img src={frame} alt="The page the session's browser is showing"
                   className="h-full w-full cursor-crosshair object-contain"
                   tabIndex={0}
                   onClick={(e) => { const p = toPage(e); sendInput({ kind: 'click', x: p.x, y: p.y }); }}
                   onWheel={(e) => { sendInput({ kind: 'wheel', dx: e.deltaX, dy: e.deltaY }); }}
                   onKeyDown={(e) => {
                     if (e.key.length === 1) sendInput({ kind: 'type', text: e.key });
                     else sendInput({ kind: 'key', key: e.key });
                   }}
                   title="Click to interact · type to send keys" />
            ) : (
              /* One empty state, not three. The box used to sit under a
                 paragraph and above a "not streaming" line, all saying that
                 nothing is open. */
              <div className="flex min-h-[220px] w-full flex-col items-center justify-center gap-2 rounded-md border border-dashed border-glass-border px-6 text-center text-[12px] leading-relaxed text-zinc-muted">
                {streamErr || (s.running
                  ? 'waiting for the page to paint…'
                  : 'No page is open. Type an address above, or ask the agent to check one — it is the same browser either way.')}
              </div>
            )}
          </div>
        )}

        {view === 'console' && (
          <ul className="divide-y divide-glass-border">
            {events.map((e, i) => (
              <li key={i} className="flex gap-2 px-2 py-1">
                <span className={'flex-none text-[11px] '
                  + (e.level === 'error' ? 'text-danger'
                    : e.level === 'warning' ? 'text-warn' : 'text-zinc-muted')}>
                  {e.type === 'pageerror' ? 'uncaught' : (e.level || 'log')}
                </span>
                <span className="min-w-0 flex-1 break-words text-zinc-300">{e.text}</span>
              </li>
            ))}
            {!events.length && (
              <li className="px-2 py-2 text-zinc-muted">
                Nothing logged yet — an empty log only means "no errors" once a
                page has actually loaded.
              </li>
            )}
            {!!dropped && (
              <li className="px-2 py-1 text-[11px] text-warn">
                {dropped} earlier message(s) were pushed out of the ring
              </li>
            )}
          </ul>
        )}

        {view === 'network' && (
          <ul className="divide-y divide-glass-border">
            {events.map((e, i) => (
              <li key={i} className="flex gap-2 px-2 py-1">
                <span className={'flex-none font-mono text-[11px] '
                  + ((e.status || 0) >= 400 ? 'text-danger'
                    : e.status ? 'text-zinc-muted' : 'text-zinc-muted')}>
                  {e.status ?? '…'}
                </span>
                <span className="flex-none text-[11px] text-zinc-muted">{e.method}</span>
                <span className="min-w-0 flex-1 truncate text-zinc-300" title={e.url}>
                  {e.url}
                </span>
              </li>
            ))}
            {!events.length && (
              <li className="px-2 py-2 text-zinc-muted">this page has made no requests.</li>
            )}
          </ul>
        )}

        {view === 'shots' && (
          <div className="p-2">
            {pick && (
              <div className="mb-2">
                <img src={pick.url} alt={`Screenshot ${pick.name}`}
                     className="max-h-[420px] w-auto max-w-full rounded-md border border-glass-border" />
                <p className="text-[11px] text-zinc-muted">
                  {pick.name} · {Math.round(pick.bytes / 1024)} KB ·
                  served from the session artifact dir
                  <button type="button" onClick={() => setPick(null)}
                          className="ml-2 rounded-md border border-glass-border px-1.5 py-0.5 hover:bg-white/5">
                    back
                  </button>
                </p>
              </div>
            )}
            {!pick && (
              <div className="grid grid-cols-3 gap-2">
                {shots.map((sh) => (
                  <button key={sh.name} type="button" onClick={() => setPick(sh)}
                          className="overflow-hidden rounded-md border border-glass-border hover:border-white/30">
                    <img src={sh.url} alt="" loading="lazy"
                         className="h-[70px] w-full object-cover object-top" />
                    <span className="block truncate px-1 py-0.5 text-[11px] text-zinc-muted">
                      {new Date(sh.at * 1000).toLocaleTimeString()}
                    </span>
                  </button>
                ))}
                {!shots.length && (
                  <p className="col-span-3 text-[12px] text-zinc-muted">
                    no screenshots filed yet. Every browser action can leave
                    one, and the agent is told to prove what it claims.
                  </p>
                )}
              </div>
            )}
            {!!more && (
              <p className="mt-1 text-[11px] text-zinc-muted">
                {more} older screenshot(s) not listed here
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
