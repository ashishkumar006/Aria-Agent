/* Integrated terminal for the Code view.

   A real shell (a PTY on the agent, see terminal.py) rendered with xterm.js,
   docked at the bottom of the editor column the way VS Code docks its panel.
   The session is created lazily on first open and kept alive when collapsed —
   collapsing a terminal and losing the scrollback is not what a terminal is
   for — so the xterm host is hidden with CSS rather than unmounted.

   The handshake carries the per-launch token in the query string: a browser
   cannot set a custom header on a WebSocket, and the agent's HTTP token
   middleware does not run for a WebSocket handshake, so the socket checks the
   token itself (see agent_server.py). */
import { useCallback, useEffect, useRef, useState } from 'react';
import { Terminal as Xterm } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import { WebglAddon } from '@xterm/addon-webgl';
import '@xterm/xterm/css/xterm.css';
import { ChevronDown, RotateCw, Terminal as TerminalIcon } from 'lucide-react';
import { launchToken } from '../api';

type Status = 'connecting' | 'open' | 'closed' | 'error';
export type TerminalStatus = Status;

/* Shares the editor surface (#10131c), so the terminal reads as part of the
   same window rather than a black well under it. */
const THEME = {
  background: '#10131c',
  foreground: '#cccccc',
  cursor: '#3794ff',
  cursorAccent: '#10131c',
  /* black stays the surface colour so apps that paint "black" don't punch a
     lighter hole in the terminal. */
  black: '#10131c', red: '#f14c4c', green: '#89d185', yellow: '#cca700',
  blue: '#3794ff', magenta: '#d670d6', cyan: '#29b8db', white: '#cccccc',
  brightBlack: '#6e7681', brightRed: '#ff7b72', brightGreen: '#a5e6a0',
  brightYellow: '#e2c08d', brightBlue: '#6cb6ff', brightMagenta: '#e2a8e2',
  brightCyan: '#56d4e8', brightWhite: '#e6e6e6',
};

const FONT = '"JetBrains Mono", "Fira Code", ui-monospace, SFMono-Regular, Menlo, monospace';

function TerminalSession({ visible, nonce, onStatus }: {
  visible: boolean; nonce: number; onStatus: (s: Status) => void;
}) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const termRef = useRef<Xterm | null>(null);
  const fitRef = useRef<FitAddon | null>(null);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const term = new Xterm({
      fontFamily: FONT,
      fontSize: 12.5,
      lineHeight: 1.2,
      cursorBlink: true,
      scrollback: 5000,
      theme: THEME,
      macOptionIsMeta: true,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host);
    termRef.current = term;
    fitRef.current = fit;
    const fitNow = () => {
      if (host.clientWidth < 20 || host.clientHeight < 20) return;
      try { fit.fit(); } catch { /* a mid-layout host has no size yet */ }
    };
    fitNow();
    try {
      const webgl = new WebglAddon();
      webgl.onContextLoss(() => webgl.dispose());
      term.loadAddon(webgl);
    } catch { /* no WebGL (common on VMs): xterm's default renderer is fine */ }

    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const qs = new URLSearchParams({
      token: launchToken(), cols: String(term.cols), rows: String(term.rows),
    });
    onStatus('connecting');
    const ws = new WebSocket(`${proto}://${location.host}/api/terminal?${qs}`);
    ws.binaryType = 'arraybuffer';

    const send = (obj: unknown) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
    };
    const resendSize = () => send({ t: 'resize', cols: term.cols, rows: term.rows });

    ws.onopen = () => { onStatus('open'); resendSize(); term.focus(); };
    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        try {
          const m = JSON.parse(ev.data) as { t?: string; error?: string };
          if (m.t === 'exit') {
            onStatus('closed');
            term.write(m.error
              ? `\r\n\x1b[31m${m.error}\x1b[0m\r\n`
              : '\r\n\x1b[90m[process exited — press restart to open a new shell]\x1b[0m\r\n');
          }
        } catch { /* not our control frame */ }
        return;
      }
      term.write(new Uint8Array(ev.data as ArrayBuffer));
    };
    ws.onerror = () => onStatus('error');
    ws.onclose = () => onStatus('closed');

    const dataSub = term.onData((d) => send({ t: 'in', d }));

    const ro = new ResizeObserver(() => { fitNow(); resendSize(); });
    ro.observe(host);

    return () => {
      ro.disconnect();
      dataSub.dispose();
      try { ws.close(); } catch { /* already gone */ }
      term.dispose();
      termRef.current = null;
      fitRef.current = null;
    };
    // nonce re-creates the session (restart); the rest is one-time setup.
  }, [nonce, onStatus]);

  useEffect(() => {
    if (!visible) return;
    const host = hostRef.current;
    if (host && host.clientWidth >= 20 && host.clientHeight >= 20) {
      try { fitRef.current?.fit(); } catch { /* see fitNow */ }
    }
    termRef.current?.focus();
  }, [visible]);

  return <div ref={hostRef} className="h-full w-full overflow-hidden bg-surface-1 pl-3 pt-1.5 pr-1" />;
}

/* Controlled mode (`bare`) is what the Code view's shared bottom panel uses:
   the panel owns the tab strip, so the terminal renders only its body and the
   session stays mounted (hidden) when another tab is selected. Collapsing the
   panel must not lose the scrollback. */
export default function TerminalDock({ open: openProp, onToggle, bare,
  nonce: nonceProp, onStatus }: {
  open?: boolean;
  onToggle?: () => void;
  bare?: boolean;
  nonce?: number;
  onStatus?: (s: TerminalStatus) => void;
} = {}) {
  const [ownOpen, setOwnOpen] = useState(false);
  const [everOpened, setEverOpened] = useState(false);
  /* A restart counter, not the nonce itself: the caller may pin a nonce, and a
     manual restart still has to recreate the session in either mode. */
  const [bump, setBump] = useState(0);
  const [status, setStatus] = useState<Status>('connecting');
  const open = openProp ?? ownOpen;
  const nonce = (nonceProp ?? 0) + bump;

  const report = useCallback((s: Status) => {
    setStatus(s);
    onStatus?.(s);
  }, [onStatus]);

  useEffect(() => {
    if (open) setEverOpened(true);
  }, [open]);

  const toggle = () => {
    if (onToggle) { onToggle(); return; }
    setOwnOpen((o) => !o);
  };

  if (bare) {
    return (
      <div className={open ? 'relative block h-full' : 'relative hidden h-full'}>
        {everOpened && (
          <TerminalSession visible={open} nonce={nonce} onStatus={report} />
        )}
        {/* The docked panel has a Restart button in its header; bare mode
            (Code's shared bottom panel) had no control at all, so an exited
            shell or an agent restart left a dead terminal (H12). */}
        {open && everOpened && status !== 'open' && status !== 'connecting' && (
          <button
            type="button"
            onClick={() => setBump((n) => n + 1)}
            className="absolute right-2 top-2 z-10 flex items-center gap-1 rounded border border-glass-border bg-surface-1/90 px-2 py-1 text-[10.5px] text-zinc-300 hover:text-white"
          >
            <RotateCw size={11} /> Restart shell
          </button>
        )}
      </div>
    );
  }

  const dot = status === 'open' ? 'bg-emerald-400'
    : status === 'connecting' ? 'bg-amber-400'
    : status === 'error' ? 'bg-rose-400' : 'bg-zinc-500';
  const label = status === 'open' ? 'live' : status === 'connecting' ? 'starting…'
    : status === 'error' ? 'error' : 'exited';

  return (
    <div className="flex flex-none flex-col border-t border-glass-border bg-surface-0">
      <div className="flex flex-none items-center gap-2 px-3 py-1.5">
        <button type="button" onClick={toggle}
          aria-expanded={open}
          className="group flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-[0.14em] text-zinc-400 hover:text-zinc-200">
          <ChevronDown size={12}
            className={`transition-transform ${open ? '' : '-rotate-90'}`} />
          <TerminalIcon size={12} />
          Terminal
        </button>
        {everOpened && (
          <span className="flex items-center gap-1.5 text-[10.5px] text-zinc-muted">
            <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
            {label}
          </span>
        )}
        <span className="flex-1" />
        {open && everOpened && (
          <button type="button" onClick={() => setBump((n) => n + 1)}
            title="Start a new shell"
            className="flex items-center gap-1 rounded border border-glass-border px-1.5 py-0.5 text-[10.5px] text-zinc-400 hover:text-zinc-200">
            <RotateCw size={11} /> Restart
          </button>
        )}
      </div>
      <div className={open ? 'block' : 'hidden'} style={{ height: 240 }}>
        {everOpened && (
          <TerminalSession visible={open} nonce={nonce} onStatus={setStatus} />
        )}
      </div>
    </div>
  );
}
