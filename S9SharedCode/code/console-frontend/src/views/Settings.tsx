import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Flag, ShieldCheck } from 'lucide-react';
import { Rail, TopBar, Skel, Pill, SkipLink } from '../components/ui';
import { useMainId, useActivePoll } from '../components/KeepAlive';
import { api, CONF, gatewayUrl, getToken, setToken, type FlagRow, type GatewayUserStatus } from '../api';
import { Pane } from '../lib/panes';
import { yamlHighlighter, yamlLine } from '../lib/yaml';

export default function Settings() {
  const mainId = useMainId();
  const [rows, setRows] = useState<[string, boolean, string][]>([]);
  const [loaded, setLoaded] = useState(false);
  const [cfg, setCfg] = useState('');
  const [cfgFilter, setCfgFilter] = useState('');
  const [apiDown, setApiDown] = useState(false);
  /* Bearer token for the ARIA_API_TOKEN gate. Without this the whole
     console 401s with no recovery path; the api.ts comment always claimed
     this field existed, but it never did — threads were equally
     undeletable in UI despite a working DELETE endpoint. */
  const [token, setTokenField] = useState('');
  const [tokenSaved, setTokenSaved] = useState(false);
  /* Feature flags and config moved here from Apps. One backend evaluator,
     one place to change it; Apps is now just the app list and app runtime. */
  const [flags, setFlags] = useState<FlagRow[]>([]);
  const [prefab, setPrefab] = useState({ configured: false, live: false });
  const [flagErr, setFlagErr] = useState('');
  const [gwUser, setGwUser] = useState<GatewayUserStatus | null>(null);
  const [gwTok, setGwTok] = useState('');
  const [gwMsg, setGwMsg] = useState('');
  const [gwBusy, setGwBusy] = useState(false);
  const flagBusy = useRef(new Set<string>());

  // Filtered view of the config dump: matching lines full-bright, the
  // rest dimmed (context preserved, no content hidden).
  const cfgLines = cfg.split('\n');
  const cfgQ = cfgFilter.trim().toLowerCase();
  const cfgHits = cfgQ ? cfgLines.filter((l) => l.toLowerCase().includes(cfgQ)).length : 0;
  /* Highlighting is stateful: it has to know whether a line is inside a `|`
     block scalar, and that depends on the lines before it. So the spans are
     computed in ONE ordered pass over the document, memoised on `cfg` - not
     per line during render, which would walk the wrong state and mis-colour
     every prompt body. */
  const cfgSpans = useMemo(() => {
    const h = yamlHighlighter();
    return cfgLines.map((l) => h.line(l));
  }, [cfg]);

  const load = useCallback(async () => {
    const [h, tools, sess, sch, tpl, c, fl, gwAcct] = await Promise.all([
      api.safe(api.health()),
      api.safe(api.tools()),
      api.safe(api.sessions(200)),
      api.safe(api.scheduleList()),
      api.safe(api.templates()),
      api.safe(api.config()),
      api.safe(api.flags()),
      api.safe(api.gatewayUser()),
    ]);
    setGwUser(gwAcct);
    const gw = !!h && h.gateway_up !== false;
    /* The agent row used to be hardcoded `true` ("this server :8500"),
       so with the API unreachable the page contradicted itself: the banner
       said "agent unreachable" next to a row asserting it was up. The
       health call that backs the banner is the same one that proves the
       agent answered, so use it. */
    const agentUp = !!h;
    const list: [string, boolean, string][] = [
      ['agent', agentUp, agentUp ? `this server :${CONF.agentPort}` : `:${CONF.agentPort} unreachable`],
      ['gateway', gw, gw ? `:${CONF.gatewayPort} reachable` : `:${CONF.gatewayPort} down`],
      ['mcp tools', !!tools, tools ? `${(tools.tools || []).length} registered` : 'unreadable'],
      ['sessions', !!sess, sess ? `${(sess.sessions || []).length} in first 200` : 'unreadable'],
      ['schedules', !!sch, sch ? `${Array.isArray(sch.schedules) ? sch.schedules.length : Object.keys(sch.schedules || {}).length} jobs` : 'unreadable'],
      ['templates', !!tpl, tpl ? `${(tpl.templates || []).length} saved` : 'unreadable'],
    ];
    setRows(list);
    setCfg((c && (c.raw || c.error)) || '(empty)');
    if (fl && !flagBusy.current.size) {
      setFlags(fl.flags || []);
      setPrefab(fl.prefab || { configured: false, live: false });
    }
    setLoaded(true);
    // The per-row pills degrade gracefully on partial failure, but when
    // health itself is unreachable every row just reads "down/unreadable"
    // with no statement of cause — so say it once, up top.
    setApiDown(!h);
  }, []);

  useActivePoll(load, CONF.pollSlowMs);

  useEffect(() => { setTokenField(getToken()); }, []);

  const saveToken = () => {
    setToken(token.trim());
    setTokenSaved(true);
    window.setTimeout(() => setTokenSaved(false), 2500);
    load();
  };

  const flipFlag = async (f: FlagRow) => {
    if (f.type !== 'bool' || flagBusy.current.has(f.name)) return;
    const next = !(f.value === true);
    const before = flags;
    flagBusy.current.add(f.name);
    setFlags((prev) => prev.map((x) => (
      x.name === f.name ? { ...x, value: next } : x)));
    try {
      const r = await api.setFlag(f.name, next);
      setFlags(r.flags || before);
      setFlagErr('');
    } catch (e) {
      setFlags(before);
      setFlagErr(`${f.name}: ${(e as Error)?.message || e}`);
    } finally { flagBusy.current.delete(f.name); }
  };

  const revertFlag = async (f: FlagRow) => {
    if (flagBusy.current.has(f.name)) return;
    const before = flags;
    flagBusy.current.add(f.name);
    try {
      const r = await api.setFlag(f.name, null);
      if (r.flags) setFlags(r.flags);
    } catch {
      setFlags(before);
    } finally { flagBusy.current.delete(f.name); }
  };

  return (
    <div className="flex h-full flex-col lg:flex-row">
      <SkipLink />
      <Rail />
      <Pane id="settings.env">
      <div className="flex w-full max-h-[34vh] flex-none flex-col border-b border-white/10 bg-[#0b0b0e] lg:max-h-none lg:w-[248px] lg:border-b-0 lg:border-r">
        <div className="px-3.5 pb-2 pt-3.5 text-xs font-bold tracking-wide">Environment</div>
        <div className="min-h-0 flex-1 space-y-2 overflow-y-auto px-2.5 pb-3">
          {!loaded && <Skel />}
          {rows.map(([name, ok, note]) => (
            <div key={name} className="rounded-[10px] border border-white/10 bg-[#0e0e12] p-2.5">
              <div className="flex items-center gap-2">
                <span className="min-w-0 flex-1 truncate text-[12.5px] font-semibold">{name}</span>
                <Pill tone={ok ? 'ok' : 'err'}>{ok ? 'up' : 'down'}</Pill>
              </div>
              <div className="mt-1 text-[11px] text-zinc-muted">{note}</div>
            </div>
          ))}
        </div>
      </div>
      </Pane>
      <main id={mainId} tabIndex={-1} className="flex min-h-0 min-w-0 flex-1 flex-col outline-none">
        <TopBar crumb="Settings" panes="settings" />
        <div className="min-h-0 flex-1 overflow-y-auto p-3.5">
          {loaded && apiDown && (
            <div className="mb-3 rounded-lg border border-red-400/30 bg-red-400/5 p-3 text-center text-xs text-red-200">
              agent unreachable — is it running on :{CONF.agentPort}? <button onClick={load} className="underline">retry</button>
            </div>
          )}
          <div className="mb-3 rounded-[10px] border border-white/10 bg-[#0e0e12] p-3.5">
            <div className="mb-1 text-[10.5px] font-bold tracking-[0.14em] text-zinc-muted">API TOKEN</div>
            <p className="mb-2 text-[11.5px] leading-relaxed text-zinc-muted">
              Only needed when the agent runs with <span className="font-mono text-zinc-300">ARIA_API_TOKEN</span> set —
              without it every page 401s. Stored in this browser only.
            </p>
            <div className="flex gap-2">
              <input
                value={token}
                onChange={(e) => { setTokenField(e.target.value); setTokenSaved(false); }}
                placeholder="paste bearer token…"
                aria-label="API bearer token"
                autoComplete="off"
                spellCheck={false}
                type="password"
                className="min-w-0 flex-1 rounded-md border border-white/10 bg-black/40 px-2.5 py-1.5 font-mono text-xs outline-none focus:border-violet-400"
              />
              <button
                onClick={saveToken}
                className="flex-none rounded-md bg-violet-400 px-3 py-1.5 text-xs font-bold text-[#0b0b0e] hover:brightness-110"
              >
                Save
              </button>
              {!!token && (
                <button
                  onClick={() => { setToken(''); setTokenField(''); load(); }}
                  className="flex-none rounded-md border border-white/15 bg-white/5 px-3 py-1.5 text-xs text-zinc-300 hover:border-red-400 hover:text-red-200"
                >
                  Clear
                </button>
              )}
            </div>
            {tokenSaved && <div className="mt-1.5 text-[11px] text-emerald-300" role="status">saved — reloading status…</div>}
          </div>
          <div className="mb-3 rounded-[10px] border border-white/10 bg-[#0e0e12] p-3.5">
            <div className="mb-1 flex items-center gap-2 text-[10.5px] font-bold tracking-[0.14em] text-zinc-muted">
              GATEWAY ACCOUNT
              <span className="flex-1" />
              <a href={gatewayUrl('/static/users.html')} target="_blank" rel="noreferrer"
                 className="font-normal normal-case tracking-normal text-[10.5px] text-zinc-500 underline hover:text-violet-300">
                users &amp; keys
              </a>
            </div>
            <p className="mb-2 text-[11.5px] leading-relaxed text-zinc-muted">
              Sign in as a gateway user so model calls run on your own saved provider keys
              instead of the shared operator pool. Create the token in the gateway's Users page.
            </p>
            {gwUser?.configured ? (
              <div className="mb-2 rounded-md border border-white/10 bg-black/25 px-2.5 py-2 text-[11.5px]">
                {gwUser.valid ? (
                  <span className="text-emerald-300">
                    {gwUser.kind === 'user'
                      ? `${gwUser.user?.name || 'user'} · ${gwUser.user?.keys ?? 0} key${(gwUser.user?.keys ?? 0) === 1 ? '' : 's'} saved`
                      : 'operator account'}
                    <span className="ml-2 font-mono text-zinc-500">{gwUser.token_hint}</span>
                  </span>
                ) : (
                  <span className="text-amber-300">token {gwUser.token_hint} was rejected: {gwUser.error || 'invalid'}</span>
                )}
              </div>
            ) : (
              <div className="mb-2 text-[11px] text-zinc-500">
                No user token — calls use the operator pool.
              </div>
            )}
            <div className="flex gap-2">
              <input
                value={gwTok}
                onChange={(e) => { setGwTok(e.target.value); setGwMsg(''); }}
                placeholder="paste gateway user token (aru_…)…"
                aria-label="Gateway user token"
                autoComplete="off"
                spellCheck={false}
                type="password"
                className="min-w-0 flex-1 rounded-md border border-white/10 bg-black/40 px-2.5 py-1.5 font-mono text-xs outline-none focus:border-violet-400"
              />
              <button
                disabled={gwBusy}
                onClick={async () => {
                  const tok = gwTok.trim();
                  if (!tok) { setGwMsg('paste a token first'); return; }
                  setGwBusy(true);
                  try {
                    const r = await api.gatewayUserSet(tok);
                    setGwUser(r);
                    setGwTok('');
                    setGwMsg(r.valid ? 'saved — new calls use your keys' : (r.error || 'saved'));
                  } catch (e) {
                    setGwMsg(String((e as Error)?.message || e));
                  } finally { setGwBusy(false); }
                }}
                className="flex-none rounded-md bg-violet-400 px-3 py-1.5 text-xs font-bold text-[#0b0b0e] hover:brightness-110 disabled:opacity-60"
              >
                {gwBusy ? 'Saving…' : 'Save'}
              </button>
              {gwUser?.configured && (
                <button
                  onClick={async () => {
                    setGwBusy(true);
                    try {
                      const r = await api.gatewayUserClear();
                      setGwUser(r);
                      setGwMsg('forgotten — back to the operator pool');
                    } catch (e) { setGwMsg(String((e as Error)?.message || e)); }
                    finally { setGwBusy(false); }
                  }}
                  className="flex-none rounded-md border border-white/15 bg-white/5 px-3 py-1.5 text-xs text-zinc-300 hover:border-red-400 hover:text-red-200"
                >
                  Forget
                </button>
              )}
            </div>
            {!!gwMsg && <div className="mt-1.5 text-[11px] text-emerald-300" role="status">{gwMsg}</div>}
          </div>
          <details className="group mb-3 rounded-[10px] border border-white/10 bg-[#0e0e12]">
            <summary className="flex cursor-pointer list-none items-center gap-2 px-3.5 py-2.5 text-[10.5px] font-bold tracking-[0.14em] text-zinc-muted">
              <Flag size={13} /> FLAGS &amp; CONFIG
              <span className="font-normal normal-case tracking-normal text-zinc-500">
                {flags.length} flag{flags.length === 1 ? '' : 's'} · {prefab.live
                  ? 'prefab cloud live'
                  : prefab.configured ? 'prefab configured (offline)' : 'local only'}
              </span>
              <span className="ml-auto text-zinc-600 group-open:hidden">show</span>
              <span className="ml-auto hidden text-zinc-600 group-open:inline">hide</span>
            </summary>
            <div className="border-t border-white/10 px-3.5 py-1">
              {flagErr && (
                <p role="alert" className="my-2 rounded border border-rose-400/30 bg-rose-400/5 px-2 py-1.5 text-[11px] text-rose-200">
                  {flagErr}
                </p>
              )}
              {!flags.length && <p className="py-3 text-[11.5px] text-zinc-500">No flags defined.</p>}
              {flags.map((f) => (
                <div key={f.name} className="flex items-center gap-3 border-b border-white/5 py-2 last:border-0">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <span className="truncate font-mono text-[11.5px] text-zinc-200">{f.name}</span>
                      <Pill tone={f.source === 'prefab' ? 'info' : f.source === 'override' ? 'warn' : 'muted'}>
                        {f.source}
                      </Pill>
                    </div>
                    {!!f.description && (
                      <p className="mt-0.5 break-words text-[10.5px] leading-snug text-zinc-500">
                        {f.description}
                      </p>
                    )}
                  </div>
                  {f.type === 'bool' ? (
                    <button
                      type="button"
                      onClick={() => void flipFlag(f)}
                      role="switch"
                      aria-checked={f.value === true}
                      aria-label={`Toggle ${f.name}`}
                      className={`relative h-5 w-9 flex-none rounded-full transition-colors ${
                        f.value === true ? 'bg-violet-400' : 'bg-white/15'
                      } ${flagBusy.current.has(f.name) ? 'opacity-50' : ''}`}>
                      <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all ${
                        f.value === true ? 'left-[18px]' : 'left-0.5'
                      }`} />
                    </button>
                  ) : (
                    <span className="flex-none font-mono text-[11px] text-zinc-300">{String(f.value)}</span>
                  )}
                  {f.source === 'override' && (
                    <button type="button" onClick={() => void revertFlag(f)}
                      className="flex-none text-[10.5px] text-zinc-500 underline hover:text-zinc-300">
                      revert
                    </button>
                  )}
                </div>
              ))}
            </div>
          </details>
          <div className="mb-1 flex items-center gap-2 text-[10.5px] font-bold tracking-[0.14em] text-zinc-muted">
            <ShieldCheck size={13} /> READ-ONLY CONFIG
            <span className="flex-1" />
            <Pane id="settings.cfgfilter">
            <input
              value={cfgFilter} onChange={(e) => setCfgFilter(e.target.value)}
              placeholder="filter config…"
              aria-label="Filter config text"
              className="rounded-md border border-white/10 bg-black/40 px-2.5 py-1 text-[11px] font-sans font-normal normal-case tracking-normal outline-none focus:border-violet-400"
            />
            </Pane>
            {cfgQ && <span className="font-sans font-semibold normal-case tracking-normal text-violet-300">{cfgHits} hit{cfgHits === 1 ? '' : 's'}</span>}
          </div>
          <Pane id="settings.dump">
          {/* FIXED height, not intrinsic. This block went from the single word
              "loading…" to several hundred lines of YAML the moment the config
              arrived, and everything below it was pushed down with it -
              cumulative layout shift 0.70 on tablet and 0.64 on mobile, the
              worst on the site by a factor of two. A bounded, scrollable region
              is also the right shape for a config dump: an unbounded `<pre>`
              lets one long file make the whole page taller than the viewport. */}
          <div className="h-[52vh] min-h-[300px] overflow-auto whitespace-pre-wrap rounded-[10px] border border-white/10 bg-[#0a0a0d] p-3.5 font-mono text-[11.5px] leading-relaxed">
            {!cfg && <span className="text-zinc-500">loading…</span>}
            {cfgLines.map((l, i) => (
              /* Highlighting returns nodes, not an HTML string: this file
                 contains prompts and prose, and rendering it through
                 dangerouslySetInnerHTML would parse its own text as markup. */
              <div key={i}>{yamlLine(cfgSpans[i], !!cfgQ && !l.toLowerCase().includes(cfgQ))}</div>
            ))}
          </div>
          </Pane>
          <Pane id="settings.links">
          <div className="mt-3 text-[11px] text-zinc-muted">
            gateway deep pages: <a href={gatewayUrl('/')} className="text-zinc-400 hover:text-violet-300">overview</a> ·{' '}
            <a href={gatewayUrl('/static/system.html')} className="text-zinc-400 hover:text-violet-300">system</a>
          </div>
          </Pane>
        </div>
      </main>
    </div>
  );
}
