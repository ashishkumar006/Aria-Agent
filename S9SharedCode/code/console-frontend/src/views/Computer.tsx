import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Bot, Check, CircleSlash, History, MonitorSmartphone, Play, ShieldCheck, X,
} from 'lucide-react';
import { Rail, TopBar, Empty, Stat, Pill, SkipLink } from '../components/ui';
import { useMainId, useSectionActive } from '../components/KeepAlive';
import { api, type ComputerApproval } from '../api';

/* The Computer view: the human half of the computer-use gate.
 *
 * The safety gates on the server are explicit that a sensitive action
 * "returns a pending token; the web UI resolves it (approve/reject) before
 * execution" (safety/gates.py). The agent server had the endpoints, but
 * the console had no page: nothing could approve anything, so a pending
 * action was a dead end - the agent asked, and the request sat in memory
 * until the process restarted.
 *
 * So this view is not a dashboard bolted on afterwards; it is the other
 * half of the gate. Without it, "gated" meant "stuck".
 *
 * What it shows, in order of what the user needs:
 *   1. Whether computer-use is enabled at all, and in which mode
 *      (dry-run describes the action; live executes it).
 *   2. The pending approvals, each with the exact action and its arguments
 *      - the decision is "this exact thing, yes or no", not "trust the agent".
 *   3. The audit trail (redacted by default) and the recorded trajectories.
 */

type Tab = 'approvals' | 'audit' | 'runs';

export default function Computer() {
  const mainId = useMainId();
  const active = useSectionActive();
  const [tab, setTab] = useState<Tab>('approvals');
  const [approvals, setApprovals] = useState<ComputerApproval[]>([]);
  const [mode, setMode] = useState('dry-run');
  const [enabled, setEnabled] = useState(false);
  const [caps, setCaps] = useState<{
    binary_present?: boolean; daemon_running?: boolean; ax_ok?: boolean;
    screenshot_ok?: boolean; elevated?: boolean; platform?: string;
    apps?: { name: string; running: boolean }[]; note?: string;
  }>({});
  const [audit, setAudit] = useState<string[]>([]);
  const [auditTotal, setAuditTotal] = useState(0);
  const [runs, setRuns] = useState<{ id: string; turns?: number; path?: string }[]>([]);
  const [openRun, setOpenRun] = useState('');
  const [runTurns, setRunTurns] = useState<{ turn: string; calls: unknown[] }[]>([]);
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState('');
  const [flash, setFlash] = useState('');
  const reqRef = useRef(0);

  const loadStatus = useCallback(async () => {
    const req = ++reqRef.current;
    try {
      const d = await api.computerStatus();
      if (req !== reqRef.current) return;
      setApprovals(d.approvals || []);
      setMode(d.mode || 'dry-run');
      setEnabled(d.enabled !== false);
      setCaps(d.capabilities || {});
      setErr('');
    } catch (e) {
      if (req !== reqRef.current) return;
      setErr(String((e as Error)?.message || e));
    }
  }, []);

  const loadAudit = useCallback(async () => {
    try {
      const d = await api.computerAudit(true, 400);
      setAudit(d.lines || []);
      setAuditTotal(d.total ?? (d.lines || []).length);
    } catch (e) {
      setErr(String((e as Error)?.message || e));
    }
  }, []);

  const loadRuns = useCallback(async () => {
    try {
      const d = await api.computerRuns();
      setRuns(d.runs || []);
    } catch (e) {
      setErr(String((e as Error)?.message || e));
    }
  }, []);

  /* Approvals are the thing that goes stale: a pending request is a
   * request the user has not answered yet, so it is polled on a short
   * interval and refreshed the moment the tab is opened. */
  useEffect(() => {
    if (!active) return;
    void loadStatus();
    const t = setInterval(() => { void loadStatus(); }, 4000);
    return () => clearInterval(t);
  }, [active, loadStatus]);

  useEffect(() => {
    if (!active) return;
    if (tab === 'audit') void loadAudit();
    if (tab === 'runs') void loadRuns();
  }, [active, tab, loadAudit, loadRuns]);

  const decide = async (a: ComputerApproval, approve: boolean) => {
    setBusy(a.id);
    try {
      const r = await api.computerDecide(a.id, approve);
      setFlash(approve
        ? `Approved: ${a.action}. ${r.status || 'approved'}.`
        : `Rejected: ${a.action}. The action did not run.`);
      setErr('');
    } catch (e) {
      setErr(String((e as Error)?.message || e));
    } finally {
      setBusy('');
      void loadStatus();
    }
  };

  /* A recording is only useful if it can be read: the row said "N turns"
   * and the calls were unreachable, so the tab could only replay a
   * trajectory nobody could inspect. */
  const toggleRun = async (id: string) => {
    if (openRun === id) { setOpenRun(''); return; }
    setOpenRun(id);
    setRunTurns([]);
    try {
      const d = await api.computerRun(id);
      setRunTurns(d.turns || []);
    } catch (e) {
      setErr(String((e as Error)?.message || e));
    }
  };

  const replay = async (id: string) => {
    setBusy(id);
    try {
      const r = await api.computerReplay(id);
      setFlash(`Replay ${id}: ${r.status || 'done'}.`);
      setErr('');
    } catch (e) {
      setErr(String((e as Error)?.message || e));
    } finally {
      setBusy('');
    }
  };

  const pending = approvals.filter((a) => (a.status || 'pending') === 'pending');
  const apps = caps.apps || [];
  const running = apps.filter((a) => a.running).length;

  return (
    <div className="flex h-full flex-col lg:flex-row">
      <SkipLink />
      <Rail />
      <div className="flex w-full max-h-[34vh] flex-none flex-col border-b border-white/10 bg-[#0b0b0e] lg:max-h-none lg:w-[248px] lg:border-b-0 lg:border-r">
        <div className="flex items-center justify-between px-3.5 pb-2 pt-3.5 text-xs font-bold tracking-wide">
          Control
          <span className="rounded-full border border-white/10 bg-white/5 px-2.5 py-0.5 text-[10.5px] font-bold text-zinc-400">
            {pending.length} pending
          </span>
        </div>
        <div className="space-y-1.5 px-2.5">
          {([['approvals', 'Approvals'], ['audit', 'Audit trail'], ['runs', 'Recordings']] as const).map(
            ([id, label]) => (
              <button
                key={id}
                onClick={() => setTab(id)}
                className={`block w-full rounded-md px-2.5 py-1.5 text-left text-xs ${tab === id ? 'bg-violet-400/15 font-bold text-zinc-100' : 'text-zinc-400 hover:bg-white/5'}`}
              >
                {label}
                {id === 'approvals' && pending.length > 0 && (
                  <span className="ml-1.5 rounded-full bg-amber-400/20 px-1.5 py-0.5 text-[10px] font-bold text-amber-200">
                    {pending.length}
                  </span>
                )}
              </button>
            ))}
        </div>
        <div className="mt-auto hidden border-t border-white/10 px-3.5 py-2.5 text-[11px] text-zinc-muted lg:block">
          {apps.length} apps · {running} running
        </div>
      </div>

      <main id={mainId} tabIndex={-1} className="flex min-h-0 min-w-0 flex-1 flex-col outline-none">
        <TopBar crumb="Computer">
          <Pill tone={!enabled ? 'muted' : mode === 'live' ? 'err' : 'muted'}>
            {!enabled ? '○ disabled' : mode === 'live' ? '● live mode' : '○ dry-run mode'}
          </Pill>
          <span
            role="status"
            className={`rounded-full border px-2.5 py-1 text-[10.5px] font-bold ${
              err
                ? 'border-red-400/50 bg-red-400/10 text-red-200'
                : 'border-white/10 bg-white/5 text-zinc-400'}`}
          >
            {err ? '● unreachable' : `${pending.length} awaiting you`}
          </span>
          <button
            onClick={() => { void loadStatus(); void loadAudit(); void loadRuns(); }}
            className="flex items-center gap-1.5 rounded-md border border-white/15 bg-white/5 px-3 py-1.5 text-xs hover:border-violet-400"
          >
            <Play size={13} /> refresh
          </button>
        </TopBar>

        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="flex flex-wrap gap-2.5 p-3.5 pb-0">
            <Stat k="PENDING" v={String(pending.length)} s="need a decision" tone={pending.length ? 'bad' : 'ok'} />
            <Stat k="MODE" v={!enabled ? 'off' : mode} s={!enabled ? 'gates are off' : mode === 'live' ? 'actions execute' : 'nothing executes'} tone={!enabled ? 'warn' : mode === 'live' ? 'bad' : 'ok'} />
            <Stat k="APPS" v={String(apps.length)} s={`${running} running`} />
            <Stat
              k="DRIVER"
              v={caps.daemon_running ? 'up' : 'down'}
              s={caps.daemon_running
                ? `AX ${caps.ax_ok ? 'ok' : 'no tree'} · shots ${caps.screenshot_ok ? 'ok' : 'none'}`
                : (caps.note || 'cua-driver not running')}
              tone={caps.daemon_running ? 'ok' : 'bad'}
            />
          </div>

          {err && (
            <p role="alert" className="mx-3.5 mb-3.5 mt-3 rounded-[10px] border border-red-400/40 bg-red-400/5 px-3.5 py-2.5 text-[12.5px] text-red-200">
              Cannot read the computer-use state: {err.slice(0, 180)}
            </p>
          )}
          {flash && (
            <p role="status" className="mx-3.5 mb-3.5 mt-3 rounded-[10px] border border-emerald-400/40 bg-emerald-400/5 px-3.5 py-2.5 text-[12.5px] text-emerald-200">
              {flash}
            </p>
          )}
          {!enabled && (
            <p className="mx-3.5 mb-3.5 mt-3 rounded-[10px] border border-amber-400/40 bg-amber-400/10 px-3.5 py-2.5 text-[12.5px] text-amber-100">
              <ShieldCheck size={13} className="mr-1.5 inline" />
              Computer-use is switched OFF on this host
              (<span className="font-mono">COMPUTER_USE_ENABLED</span>). Every
              request is refused, so an empty queue here means the feature is
              off - not that nothing was asked. Restart the agent with the
              flag set to turn it on.
            </p>
          )}
          {enabled && mode !== 'live' && (
            <p className="mx-3.5 mb-3.5 mt-3 rounded-[10px] border border-amber-400/40 bg-amber-400/10 px-3.5 py-2.5 text-[12.5px] text-amber-100">
              <ShieldCheck size={13} className="mr-1.5 inline" />
              Computer-use is in DRY-RUN mode: the agent describes what it would
              do and nothing executes. Live mode is a host setting
              (<span className="font-mono">COMPUTER_USE_MODE=live</span>), not a
              switch here - so this page can never be the thing that turns a
              machine into a target.
            </p>
          )}

          {tab === 'approvals' && (
            <section className="mx-3.5 mb-3.5 mt-3 overflow-hidden rounded-[10px] border border-white/10 bg-[#0e0e12]">
              <div className="border-b border-white/10 px-3.5 py-2.5 text-xs font-bold tracking-wider">
                PENDING APPROVALS
              </div>
              {!pending.length && (
                <Empty icon={<Bot size={30} />} title="Nothing is waiting for you"
                  sub="A sensitive action the agent wants to take shows up here, with its exact arguments, before it runs." />
              )}
              {pending.map((a) => (
                <div key={a.id} className="border-b border-white/5 px-3.5 py-3 last:border-b-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="rounded-md bg-amber-400/15 px-2 py-0.5 font-mono text-[11px] font-bold text-amber-200">
                      {a.action}
                    </span>
                    {/* The agent's answer quotes this id ("Approval ID:
                        cu-..."), so the row has to show it or the user
                        cannot match the message to the request. */}
                    <span className="font-mono text-[10.5px] text-zinc-muted">{a.id}</span>
                    <span className="text-[11px] text-zinc-muted">
                      {a.created ? new Date(a.created * 1000).toLocaleString() : ''}
                    </span>
                    <span className="ml-auto flex gap-2">
                      <button
                        onClick={() => void decide(a, true)}
                        disabled={busy === a.id}
                        className="flex items-center gap-1.5 rounded-md border border-emerald-400/50 bg-emerald-400/10 px-2.5 py-1 text-[11.5px] font-bold text-emerald-200 hover:bg-emerald-400/20 disabled:opacity-50"
                      >
                        <Check size={13} /> approve
                      </button>
                      <button
                        onClick={() => void decide(a, false)}
                        disabled={busy === a.id}
                        className="flex items-center gap-1.5 rounded-md border border-red-400/50 bg-red-400/10 px-2.5 py-1 text-[11.5px] font-bold text-red-200 hover:bg-red-400/20 disabled:opacity-50"
                      >
                        <X size={13} /> reject
                      </button>
                    </span>
                  </div>
                  <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-md bg-black/40 p-2.5 font-mono text-[11px] text-zinc-300">
                    {JSON.stringify(a.params ?? {}, null, 2)}
                  </pre>
                </div>
              ))}
            </section>
          )}

          {tab === 'audit' && (
            <section className="mx-3.5 mb-3.5 mt-3 overflow-hidden rounded-[10px] border border-white/10 bg-[#0e0e12]">
              <div className="border-b border-white/10 px-3.5 py-2.5 text-xs font-bold tracking-wider">
                AUDIT TRAIL <span className="font-normal text-zinc-muted">(secrets redacted)</span>
                {auditTotal > audit.length && (
                  <span className="ml-2 font-normal text-zinc-muted">
                    newest {audit.length} of {auditTotal}
                  </span>
                )}
              </div>
              <div className="max-h-[calc(100vh-320px)] min-h-[200px] overflow-y-auto py-2 font-mono text-xs leading-relaxed">
                {!audit.length && <Empty icon={<History size={30} />} title="No gated actions yet" />}
                {audit.map((l, i) => (
                  <div key={i} className="border-l-2 border-transparent px-3 py-[3px] break-words text-zinc-400 hover:bg-white/[0.03]">
                    {l}
                  </div>
                ))}
              </div>
            </section>
          )}

          {tab === 'runs' && (
            <section className="mx-3.5 mb-3.5 mt-3 overflow-hidden rounded-[10px] border border-white/10 bg-[#0e0e12]">
              <div className="border-b border-white/10 px-3.5 py-2.5 text-xs font-bold tracking-wider">
                RECORDED TRAJECTORIES
              </div>
              {!runs.length && (
                <Empty icon={<MonitorSmartphone size={30} />} title="No recordings yet"
                  sub="A trajectory is recorded when the agent drives the desktop with recording on." />
              )}
              {runs.map((r) => (
                <div key={r.id} className="border-b border-white/5 last:border-b-0">
                  <div className="flex items-center gap-3 px-3.5 py-2.5">
                    <button
                      onClick={() => void toggleRun(r.id)}
                      className="font-mono text-[12px] text-zinc-200 hover:text-violet-300"
                      title="Show the recorded tool calls"
                    >
                      {openRun === r.id ? '▾' : '▸'} {r.id}
                    </button>
                    <span className="text-[11px] text-zinc-muted">{r.turns ?? 0} turns</span>
                    <button
                      onClick={() => void replay(r.id)}
                      disabled={busy === r.id || !enabled || mode !== 'live'}
                      title={!enabled
                        ? 'Computer-use is off on this host'
                        : mode !== 'live'
                          ? 'Replay re-runs real clicks and keystrokes, so it needs live mode'
                          : 'Re-run this recorded trajectory'}
                      className="ml-auto flex items-center gap-1.5 rounded-md border border-white/15 bg-white/5 px-2.5 py-1 text-[11.5px] hover:border-violet-400 disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      <CircleSlash size={13} /> replay
                    </button>
                  </div>
                  {openRun === r.id && (
                    <div className="px-3.5 pb-3">
                      {!runTurns.length && (
                        <p className="text-[11.5px] text-zinc-muted">
                          This recording holds no tool calls.
                        </p>
                      )}
                      {runTurns.map((t) => (
                        <div key={t.turn} className="mb-2">
                          <div className="mb-1 font-mono text-[11px] text-zinc-400">{t.turn}</div>
                          <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-md bg-black/40 p-2.5 font-mono text-[11px] text-zinc-300">
                            {JSON.stringify(t.calls, null, 2)}
                          </pre>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </section>
          )}
        </div>
      </main>
    </div>
  );
}
