import { useCallback, useEffect, useState } from 'react';
import { ShieldCheck } from 'lucide-react';
import { Rail, TopBar, Skel, Pill, SkipLink } from '../components/ui';
import { api, CONF, gatewayUrl, getToken, setToken } from '../api';

export default function Settings() {
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

  // Filtered view of the config dump: matching lines full-bright, the
  // rest dimmed (context preserved, no content hidden).
  const cfgLines = cfg.split('\n');
  const cfgQ = cfgFilter.trim().toLowerCase();
  const cfgHits = cfgQ ? cfgLines.filter((l) => l.toLowerCase().includes(cfgQ)).length : 0;

  const load = useCallback(async () => {
    const [h, tools, sess, sch, tpl, c] = await Promise.all([
      api.safe(api.health()),
      api.safe(api.tools()),
      api.safe(api.sessions(200)),
      api.safe(api.scheduleList()),
      api.safe(api.templates()),
      api.safe(api.config()),
    ]);
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
    setLoaded(true);
    // The per-row pills degrade gracefully on partial failure, but when
    // health itself is unreachable every row just reads "down/unreadable"
    // with no statement of cause — so say it once, up top.
    setApiDown(!h);
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, CONF.pollSlowMs);
    return () => clearInterval(t);
  }, [load]);

  useEffect(() => { setTokenField(getToken()); }, []);

  const saveToken = () => {
    setToken(token.trim());
    setTokenSaved(true);
    window.setTimeout(() => setTokenSaved(false), 2500);
    load();
  };

  return (
    <div className="flex h-full flex-col lg:flex-row">
      <SkipLink />
      <Rail />
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
      <main id="main" tabIndex={-1} className="flex min-h-0 min-w-0 flex-1 flex-col outline-none">
        <TopBar crumb="Settings" />
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
          <div className="mb-1 flex items-center gap-2 text-[10.5px] font-bold tracking-[0.14em] text-zinc-muted">
            <ShieldCheck size={13} /> READ-ONLY CONFIG
            <span className="flex-1" />
            <input
              value={cfgFilter} onChange={(e) => setCfgFilter(e.target.value)}
              placeholder="filter config…"
              aria-label="Filter config text"
              className="rounded-md border border-white/10 bg-black/40 px-2.5 py-1 text-[11px] font-sans font-normal normal-case tracking-normal outline-none focus:border-violet-400"
            />
            {cfgQ && <span className="font-sans font-semibold normal-case tracking-normal text-violet-300">{cfgHits} hit{cfgHits === 1 ? '' : 's'}</span>}
          </div>
          <div className="overflow-auto whitespace-pre-wrap rounded-[10px] border border-white/10 bg-[#0a0a0d] p-3.5 font-mono text-[11.5px] leading-relaxed">
            {!cfg && 'loading…'}
            {cfgLines.map((l, i) => (
              <div key={i} className={cfgQ && !l.toLowerCase().includes(cfgQ) ? 'opacity-25' : undefined}>{l || ' '}</div>
            ))}
          </div>
          <div className="mt-3 text-[11px] text-zinc-muted">
            gateway deep pages: <a href={gatewayUrl('/')} className="text-zinc-400 hover:text-violet-300">overview</a> ·{' '}
            <a href={gatewayUrl('/static/system.html')} className="text-zinc-400 hover:text-violet-300">system</a>
          </div>
        </div>
      </main>
    </div>
  );
}
