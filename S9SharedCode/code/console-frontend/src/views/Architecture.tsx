import { Fragment, useState } from 'react';
import { TopBar } from '../components/ui';

/** One architecture node — a box the reader can click to see its role. */
type Node = {
  id: string;
  label: string;
  hint: string;       // one line under the label in the box
  role: string;       // the detail panel paragraph
  files: string[];    // where it lives
  invariant?: string; // the design rule it enforces
};

type Layer = { id: string; title: string; blurb: string; nodes: Node[] };

const LAYERS: Layer[] = [
  {
    id: 'surfaces',
    title: 'Surfaces',
    blurb: 'Where the user talks to the system.',
    nodes: [
      {
        id: 'console', label: 'Console', hint: 'React + Vite',
        role: 'The single-page console. Every section (Research, Runs, Authoring, Studio…) is a view over the same agent API. One React app, no second backend.',
        files: ['console-frontend/src/App.tsx', 'views/*.tsx'],
        invariant: 'A section is a view, never a second server.',
      },
      {
        id: 'desktop', label: 'Desktop shell', hint: 'Electron',
        role: 'A portable Electron wrapper that starts the agent + gateway and shows the same :8500 console in a window. It is a launcher, not a backend.',
        files: ['agent-desktop/main.cjs', 'build_desktop.ps1'],
        invariant: 'The shell drives :8500; it is not a second backend.',
      },
    ],
  },
  {
    id: 'agent',
    title: 'Agent server  :8500',
    blurb: 'Decides WHAT to do: plans, runs skills, calls tools.',
    nodes: [
      {
        id: 'chat', label: 'Chat', hint: 'one tool loop',
        role: 'A single run_with_tools loop with a read-only toolset, grounded by document retrieval and memory BEFORE the model sees anything.',
        files: ['agent_server.py  chat_simple_stream', 'mcp_runner.py'],
        invariant: 'Grounding happens on the request path, not as a tool.',
      },
      {
        id: 'runs', label: 'Runs — DAG Executor', hint: 'plan → skills → recover',
        role: 'The planner emits a JSON graph of skill nodes; the Executor runs them in waves, inserting critic gates and a recovery planner around failures.',
        files: ['flow.py', 'skills.py', 'recovery.py'],
        invariant: 'The planner decides the shape; the executor enforces it.',
      },
      {
        id: 'skills', label: 'Skill registry', hint: 'prompt + tools per skill',
        role: 'Each skill = a prompt (.md) + the exact tool list it may call + an output contract. The author skill is one skill with per-format fragments.',
        files: ['skills.py', 'agent_config.yaml', 'prompts/*.md'],
        invariant: 'A capability must be in the tool payload, not just the prompt.',
      },
      {
        id: 'authoring', label: 'Authoring', hint: 'research → author → render',
        role: 'The planner sets format/engine in node metadata; the author gets ONLY that format’s fragment and the render is forced to it, so it renders the required type every time.',
        files: ['skills.py  _author_format_fragment', 'prompts/author/*.md'],
        invariant: 'Planner decides, render forced, instructions match.',
      },
      {
        id: 'tools', label: 'MCP tools', hint: 'confined fs + shell',
        role: 'The tool surface the model actually calls: file ops, shell, git, browser, document render. Confinement lives in exactly two places, not scattered checks.',
        files: ['mcp_server.py', 'mcp_runner.py'],
        invariant: 'Confinement lives in two places — a third is a bug.',
      },
      {
        id: 'persistence', label: 'Persistence', hint: 'sessions · artifacts',
        role: 'Sessions, graphs, node states and artifacts on disk, so a run survives a reload and can reattach after the browser closes.',
        files: ['persistence.py', 'artifacts.py', 'studio_runs.py'],
        invariant: 'A run is an addressable object, not a stream you must not close.',
      },
    ],
  },
  {
    id: 'gateway',
    title: 'Gateway  :8109',
    blurb: 'Decides WHETHER and WHAT IT COSTS: one place for every model call.',
    nodes: [
      {
        id: 'router', label: 'Router', hint: 'TINY / LARGE / HUGE',
        role: 'Classifies a request by size (tokens) into a tier, then picks the worker failover order for that tier. A lightweight fixer (JSON repair) prefers kilo, falling back to gemini.',
        files: ['main.py  _classify_tier', 'router.py'],
        invariant: 'One router chooses the model so cost and failover are central.',
      },
      {
        id: 'policy', label: 'Policy', hint: 'egress · creds · budget',
        role: 'A first-match allow-list for egress, credential selection and a budget gate — the only place a dollar or a secret is decided.',
        files: ['policy/engine.py', 'policy/policy.yaml'],
        invariant: 'Never trust a client-supplied price; read it from the ledger.',
      },
      {
        id: 'providers', label: 'Providers', hint: '16 adaptors',
        role: 'One adaptor per vendor (gemini, kilo, anthropic, openai, ollama…). Each translates the shared request to the vendor’s API and back.',
        files: ['providers.py', 'adaptors/'],
        invariant: 'Failover is cross-vendor, so one vendor outage is survivable.',
      },
      {
        id: 'docgen', label: 'Docgen', hint: 'PDF · PPTX · DOCX · Typst',
        role: 'Renders the block model to real files: native PDF (reportlab), LaTeX (tectonic), Typst, PPTX/DOCX/XLSX, plus md/html/csv/json/epub/odt.',
        files: ['docgen.py', 'docgen_latex.py', 'docgen_typst.py'],
        invariant: 'A claim without a rendered artifact fails the run.',
      },
      {
        id: 'ledger', label: 'Ledger', hint: 'the one cost truth',
        role: 'SQLite record of every turn’s provider, model and USD. The only honest source of spend — the console reads it, never a guessed price.',
        files: ['ledger.py', 'pricing.py'],
        invariant: 'Cost is read back from the gateway, never assumed.',
      },
    ],
  },
  {
    id: 'data',
    title: 'Data planes',
    blurb: 'Shared state both halves read and write.',
    nodes: [
      {
        id: 'memory', label: 'Memory', hint: 'FAISS + keyword',
        role: 'Working/episode/fact/playbook/policy drawers behind a hybrid vector + keyword store, so a skill is grounded in what the user already told it.',
        files: ['memory.py', 'llm_gatewayV9/memory/'],
        invariant: 'Memory is retrieved on the path, not searched by the model.',
      },
      {
        id: 'docs', label: 'Documents', hint: 'parse · chunk · embed',
        role: 'Uploaded files are parsed, chunked and embedded so chat and the researcher can ground on them.',
        files: ['documents/', 'docgen_api.py'],
        invariant: 'A document is enabled only once it is fully indexed.',
      },
      {
        id: 'scheduler', label: 'Scheduler', hint: 'nudges · wakes',
        role: 'One-shots, recurring jobs and self-wakes. A wake runs a model-free predicate and only spends a turn when it matches.',
        files: ['scheduler.py'],
        invariant: 'A timer that matches nothing costs nothing.',
      },
    ],
  },
];

/** The request lifecycle, as a presentation track. `node` points at the box in
 *  LAYERS whose detail should be shown while this step is active. */
type Stage = {
  label: string;
  where: string;
  say: string;
  node: string;
};

const FLOW: Stage[] = [
  {
    label: 'You ask', where: 'Console',
    say: 'Every surface — chat, runs, authoring, studio — is a view over one agent API. The console holds no business logic; it renders state and posts intents.',
    node: 'console',
  },
  {
    label: 'Planner', where: 'agent :8500',
    say: 'The planner turns the request into a JSON graph of skill nodes. It decides the SHAPE of the work, not the prose; a node gets only its own slice plus the overall request as context.',
    node: 'runs',
  },
  {
    label: 'Executor', where: 'agent :8500',
    say: 'The executor runs the graph in waves. It inserts critic gates, and when a node fails it re-plans around the failure instead of retrying the same node blindly.',
    node: 'skills',
  },
  {
    label: 'Skills + tools', where: 'agent :8500',
    say: 'Each skill is a prompt plus the exact tool list it may call. That payload is the contract: if a tool is not in it, the skill cannot use it, no matter what the prompt says.',
    node: 'tools',
  },
  {
    label: 'Gateway', where: ':8109',
    say: 'Every model call leaves through the gateway: router picks the tier, policy decides egress and budget, a vendor adaptor makes the call, and the ledger records the cost. The agent never holds a provider key.',
    node: 'router',
  },
  {
    label: 'Artifact', where: 'docgen',
    say: 'The result must be a real artifact — a rendered PDF, deck or answer. A claim with no file behind it fails the run, so "done" always means something the user can open.',
    node: 'docgen',
  },
];

/** Likely interview questions, with answers grounded in the code above. */
const QA: { q: string; a: string }[] = [
  {
    q: 'Why split the agent (:8500) from the gateway (:8109)?',
    a: 'The agent decides WHAT to do; the gateway decides WHETHER a model call may happen and what it costs. Centralising every model call gives one place for failover, policy, credential handling and the ledger — a caller cannot route around it.',
  },
  {
    q: 'How do you keep cost honest?',
    a: 'Every model call is logged in the gateway’s SQLite ledger with provider, model, token counts and USD. The console reads spend from that ledger; a client-supplied price is never trusted.',
  },
  {
    q: 'What happens when a provider is down or rate-limited?',
    a: 'The router tracks per-provider RPM/TPM/RPD and walks a failover ladder. Load is spread by picking the least-recently-loaded usable provider, and a failed attempt is recorded so the next hop starts from the outage, not from a blind retry.',
  },
  {
    q: 'How do you stop a long document from being truncated?',
    a: 'Length comes from splitting the work, not from raising max_tokens: the author writes one section per model call and the orchestrator renders once at the end. Measured live, that is how a 27,000-word request actually lands.',
  },
  {
    q: 'How do images get into a generated PDF?',
    a: 'The author can search CC-licensed images and file one into Documents. For a long document a Python pre-pass fetches the most relevant image per section, vision-checks it, shows it to the section writer and hands it an exact handle to place; the renderer embeds it at a measured width with a caption.',
  },
  {
    q: 'How do you stop the model claiming a side effect that never happened?',
    a: 'Every skill has a tool payload, and a capability must exist in the payload — the prompt alone is a lie. The executor verifies the produced artifact, and a run that renders no file fails rather than reporting success.',
  },
];

const PRINCIPLES = [
  'Grounding happens before the model, not as a tool it may forget to call.',
  'A capability must exist in the tool payload — the prompt alone is a lie.',
  'One path vocabulary and confinement in exactly two places.',
  'Cost is read from the ledger; a client-supplied price is never trusted.',
  'Never claim a side effect that did not happen.',
  'Stop is honoured inside a node, not only between batches.',
];

/** The request path as a real diagram: boxes and arrows, not a list.
 *  Clicking a box selects the matching node in the detail panel below. */
function Diagram({ step, onSelect }: {
  step: number; onSelect: (id: string) => void;
}) {
  const box = (id: string, x: number, y: number, w: number, h: number,
               label: string, sub: string, active: boolean) => (
    <g key={`${id}-${label}`} className="cursor-pointer" onClick={() => onSelect(id)}>
      <rect x={x} y={y} width={w} height={h} rx={9}
            className={active
              ? 'fill-indigo-500/15 stroke-indigo-400'
              : 'fill-white/[0.03] stroke-white/10 hover:stroke-white/25'} />
      <text x={x + 11} y={y + 21}
            className={active
              ? 'fill-indigo-100 text-[12px] font-semibold'
              : 'fill-zinc-200 text-[12px] font-semibold'}>{label}</text>
      <text x={x + 11} y={y + 37} className="fill-zinc-500 text-[10px]">{sub}</text>
    </g>
  );
  const arrow = (x1: number, y1: number, x2: number, y2: number, dashed = false) => (
    <line x1={x1} y1={y1} x2={x2} y2={y2}
          className={dashed ? 'stroke-zinc-700' : 'stroke-zinc-600'}
          strokeWidth={1.5} strokeDasharray={dashed ? '4 3' : undefined}
          markerEnd="url(#arw)" />
  );
  const col = (x: number, y: number, label: string) => (
    <text x={x} y={y}
          className="fill-zinc-600 text-[9.5px] font-bold uppercase tracking-widest">
      {label}
    </text>
  );
  return (
    <svg viewBox="0 0 1000 300" className="mb-4 w-full" role="img"
         aria-label="Request path: surface to agent to gateway to artifact">
      <defs>
        <marker id="arw" viewBox="0 0 10 10" refX="8" refY="5"
                markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M 0 0 L 10 5 L 0 10 z" className="fill-zinc-600" />
        </marker>
      </defs>

      {col(20, 46, 'Surface')}
      {col(240, 14, 'Agent :8500')}
      {col(560, 14, 'Gateway :8109')}
      {col(860, 72, 'Out')}

      {box('console', 20, 56, 175, 74, 'Console / Desktop', 'React · Electron', step === 0)}
      {box('runs', 240, 22, 250, 50, 'Planner', 'emits the DAG', step === 1)}
      {box('runs', 240, 82, 250, 50, 'Executor', 'waves · critic · recovery', step === 2)}
      {box('tools', 240, 142, 250, 50, 'Skills + MCP tools', 'prompt + tool payload', step === 3)}
      {box('router', 560, 22, 250, 50, 'Router', 'tier → failover order', step === 4)}
      {box('policy', 560, 82, 250, 50, 'Policy', 'egress · creds · budget', step === 4)}
      {box('ledger', 560, 142, 250, 50, 'Providers → Ledger', 'vendor call · cost', step === 4)}
      {box('docgen', 860, 82, 120, 50, 'Artifact', 'PDF · PPTX', step === 5)}

      {arrow(195, 93, 236, 93)}
      {arrow(490, 93, 556, 93)}
      {arrow(810, 107, 856, 107)}
      {arrow(365, 72, 365, 80)}
      {arrow(365, 132, 365, 140)}
      {arrow(685, 72, 685, 80)}
      {arrow(685, 132, 685, 140)}

      {col(240, 230, 'Data planes')}
      {box('memory', 240, 240, 120, 44, 'Memory', 'FAISS + keyword', false)}
      {box('docs', 370, 240, 120, 44, 'Documents', 'parse · embed', false)}
      {box('scheduler', 500, 240, 120, 44, 'Scheduler', 'wakes', false)}
      {arrow(300, 240, 300, 196, true)}
      {arrow(430, 240, 430, 196, true)}
      {arrow(560, 240, 560, 196, true)}
    </svg>
  );
}

export default function Architecture() {
  const allNodes = LAYERS.flatMap((l) => l.nodes);
  const [step, setStep] = useState(0);
  const [selected, setSelected] = useState<Node | null>(
    allNodes.find((n) => n.id === FLOW[0].node) || allNodes[0] || null,
  );
  const [copied, setCopied] = useState(false);

  const go = (i: number) => {
    const next = Math.max(0, Math.min(FLOW.length - 1, i));
    setStep(next);
    const n = allNodes.find((x) => x.id === FLOW[next].node);
    if (n) setSelected(n);
  };

  const selectNode = (id: string) => {
    const n = allNodes.find((x) => x.id === id);
    if (n) setSelected(n);
  };

  const talkTrack = FLOW.map(
    (s, i) => `${i + 1}. ${s.label} (${s.where}) — ${s.say}`,
  ).join('\n\n');

  const copyTalk = async () => {
    try {
      await navigator.clipboard.writeText(talkTrack);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <TopBar crumb="Architecture" />
      <div className="min-h-0 flex-1 overflow-auto px-4 py-4">
        <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
          <p className="max-w-3xl text-[13px] leading-relaxed text-zinc-muted">
            Aria is one agent server (<span className="text-zinc-300">:8500</span>) that decides
            <em> what to do</em>, and one gateway (<span className="text-zinc-300">:8109</span>) that decides
            <em> whether it may and what it costs</em>. Click any box to see its role, where it lives, and the
            rule it enforces.
          </p>
          <button
            onClick={() => void copyTalk()}
            className="shrink-0 rounded-lg border border-white/10 bg-white/[0.03] px-3 py-1.5 text-[11.5px] text-zinc-300 transition hover:border-white/25"
          >
            {copied ? 'Talk track copied ✓' : 'Copy talk track'}
          </button>
        </div>

        {/* ── Diagram ──────────────────────────────────────────────────── */}
        <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-500">
          Request path — click a box
        </div>
        <Diagram step={step} onSelect={selectNode} />

        {/* ── Request flow ─────────────────────────────────────────────── */}
        <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-500">
          Request lifecycle — click a step
        </div>
        <div className="mb-3 flex flex-wrap items-stretch gap-1.5">
          {FLOW.map((s, i) => {
            const on = i === step;
            return (
              <Fragment key={s.label}>
                <button
                  onClick={() => go(i)}
                  className={
                    'min-w-[92px] flex-1 rounded-xl border px-2.5 py-2 text-left transition ' +
                    (on
                      ? 'border-indigo-400 bg-indigo-500/15'
                      : 'border-white/10 bg-white/[0.03] hover:border-white/25')
                  }
                >
                  <div className={'text-[10px] font-semibold ' + (on ? 'text-indigo-300' : 'text-zinc-500')}>
                    {i + 1}
                  </div>
                  <div className={'text-[12.5px] font-medium ' + (on ? 'text-indigo-100' : 'text-zinc-200')}>
                    {s.label}
                  </div>
                  <div className="text-[10px] text-zinc-500">{s.where}</div>
                </button>
                {i < FLOW.length - 1 && (
                  <div className="self-center px-0.5 text-[15px] text-zinc-600">→</div>
                )}
              </Fragment>
            );
          })}
        </div>

        {/* Talk track for the active step */}
        <div className="mb-5 rounded-xl border border-indigo-400/30 bg-indigo-500/[0.06] p-3.5">
          <div className="flex items-center justify-between gap-3">
            <div className="flex items-baseline gap-2">
              <span className="rounded-md bg-indigo-500/20 px-1.5 py-0.5 text-[11px] font-semibold text-indigo-200">
                {step + 1} / {FLOW.length}
              </span>
              <span className="text-[13px] font-semibold text-indigo-100">{FLOW[step].label}</span>
              <span className="text-[11px] text-zinc-500">{FLOW[step].where}</span>
            </div>
            <div className="flex items-center gap-1.5">
              <button
                onClick={() => go(step - 1)}
                disabled={step === 0}
                className="rounded-md border border-white/10 px-2 py-1 text-[11px] text-zinc-300 transition enabled:hover:border-white/25 disabled:opacity-30"
              >
                ← Prev
              </button>
              <button
                onClick={() => go(step + 1)}
                disabled={step === FLOW.length - 1}
                className="rounded-md border border-white/10 px-2 py-1 text-[11px] text-zinc-300 transition enabled:hover:border-white/25 disabled:opacity-30"
              >
                Next →
              </button>
            </div>
          </div>
          <p className="mt-2 text-[12.5px] leading-relaxed text-zinc-300">{FLOW[step].say}</p>
          <div className="mt-2.5 h-1 overflow-hidden rounded-full bg-white/[0.06]">
            <div
              className="h-full rounded-full bg-indigo-400/70 transition-all"
              style={{ width: `${((step + 1) / FLOW.length) * 100}%` }}
            />
          </div>
        </div>

        {/* ── Layer diagram ────────────────────────────────────────────── */}
        <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-500">
          The system, layer by layer
        </div>
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-4">
          {LAYERS.map((layer) => (
            <div key={layer.id} className="rounded-xl border border-white/10 bg-black/20 p-2.5">
              <div className="mb-2 px-1">
                <div className="text-[12px] font-semibold text-zinc-200">{layer.title}</div>
                <div className="text-[10.5px] leading-snug text-zinc-500">{layer.blurb}</div>
              </div>
              <div className="flex flex-col gap-1.5">
                {layer.nodes.map((n) => {
                  const on = selected?.id === n.id;
                  return (
                    <button
                      key={n.id}
                      onClick={() => setSelected(n)}
                      className={
                        'rounded-lg border px-2.5 py-2 text-left transition ' +
                        (on
                          ? 'border-indigo-400 bg-indigo-500/15'
                          : 'border-white/10 bg-white/[0.03] hover:border-white/25')
                      }
                    >
                      <div className={'text-[12px] font-medium ' + (on ? 'text-indigo-200' : 'text-zinc-200')}>
                        {n.label}
                      </div>
                      <div className="text-[10.5px] text-zinc-500">{n.hint}</div>
                    </button>
                  );
                })}
              </div>
            </div>
          ))}
        </div>

        {/* Detail panel */}
        {selected && (
          <div className="mt-4 rounded-xl border border-indigo-400/30 bg-indigo-500/[0.06] p-4">
            <div className="flex items-baseline gap-2">
              <h3 className="text-[14px] font-semibold text-indigo-200">{selected.label}</h3>
              <span className="text-[11px] text-zinc-500">{selected.hint}</span>
            </div>
            <p className="mt-1.5 text-[12.5px] leading-relaxed text-zinc-300">{selected.role}</p>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {selected.files.map((f) => (
                <code key={f} className="rounded bg-black/40 px-1.5 py-0.5 text-[10.5px] text-zinc-400">
                  {f}
                </code>
              ))}
            </div>
            {selected.invariant && (
              <p className="mt-2 text-[11.5px] text-amber-300/90">
                <span className="font-semibold">Invariant:</span> {selected.invariant}
              </p>
            )}
          </div>
        )}

        {/* ── Interview Q&A ────────────────────────────────────────────── */}
        <div className="mt-6">
          <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-500">
            Likely questions
          </div>
          <div className="grid grid-cols-1 gap-2 lg:grid-cols-2">
            {QA.map((item) => (
              <details
                key={item.q}
                className="group rounded-xl border border-white/10 bg-white/[0.02] px-3.5 py-2.5"
              >
                <summary className="cursor-pointer list-none text-[12.5px] font-medium text-zinc-200">
                  <span className="mr-1.5 text-indigo-400 transition group-open:rotate-90">▸</span>
                  {item.q}
                </summary>
                <p className="mt-2 pl-4 text-[12px] leading-relaxed text-zinc-400">{item.a}</p>
              </details>
            ))}
          </div>
        </div>

        {/* Principles */}
        <div className="mt-6 rounded-xl border border-white/10 bg-white/[0.02] p-4">
          <h3 className="mb-2 text-[12px] font-semibold uppercase tracking-wide text-zinc-400">
            Design principles
          </h3>
          <ul className="grid grid-cols-1 gap-1.5 text-[12px] text-zinc-300 sm:grid-cols-2">
            {PRINCIPLES.map((p) => (
              <li key={p} className="flex gap-2">
                <span className="text-indigo-400">▸</span>
                <span>{p}</span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}