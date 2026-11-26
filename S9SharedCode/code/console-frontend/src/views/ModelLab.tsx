import { useEffect, useState } from 'react';
import { Rail } from '../components/ui';
import { api } from '../api';
import {
  Cpu, Layers, ExternalLink, Play, AlertTriangle, CheckCircle2,
  ArrowRight, Terminal, Code2, Sparkles, Gauge,
} from 'lucide-react';

/** Model Lab — a showcase for the from-scratch 124M instruction-tuned model.
 *
 *  Two halves: a portfolio-grade overview (metrics, pipeline, results) and a
 *  live playground that embeds the local Model Lab app (port 7861) when it is
  *  running, with the captured screenshots as a graceful fallback. */

const METRICS = [
  { label: 'Parameters', value: '124M', sub: '114.2M unique' },
  { label: 'Vocab', value: '50,304', sub: 'GPT-2 BPE' },
  { label: 'Context', value: '2,048', sub: 'tokens' },
  { label: 'Pre-training', value: '4B', sub: 'tokens' },
  { label: 'Instruction sets', value: '7,500', sub: '3 epochs' },
  { label: 'Val loss', value: '2.078', sub: 'epoch 3' },
];

const PIPELINE = [
  { step: '01', title: 'Pre-train', detail: '4B tokens · FineWeb-Edu + technical · bf16 · cosine decay', tone: 'text-sky-300' },
  { step: '02', title: 'Instruction tune', detail: '7,500 [INST] pairs · 3 epochs · train 1.980 / val 2.078', tone: 'text-violet-300' },
  { step: '03', title: 'Evaluate', detail: 'code 85%+ · instruction adherence strong · hallucination 0.133', tone: 'text-emerald-300' },
];

const STRENGTHS = [
  'Code generation — 85%+ accuracy',
  'Instruction following / format adherence',
  'Structured responses (JSON, lists)',
  'Creative writing — 80%+ quality',
];
const LIMITS = [
  'Factual QA — 70–80%',
  'Math / logic — 60–70%',
  '2048-token context',
  'Can hallucinate on niche/recent topics',
];
const FUTURE = [
  '10B-token pre-training (in progress)',
  '50K instruction examples',
  'Code / math specialised variants',
  '4,096-token context',
];

const SHOTS = [
  { src: '/assets/model-lab/_shot_playground.png', label: 'Playground' },
  { src: '/assets/model-lab/_shot_compare.png', label: 'Base vs tuned' },
  { src: '/assets/model-lab/_shot_architecture.png', label: 'Architecture' },
  { src: '/assets/model-lab/_shot_results.png', label: 'Results' },
  { src: '/assets/model-lab/_final_lab.png', label: 'Lab' },
];

const HF_SPACE = 'https://huggingface.co/spaces/aishwarya/llm-124m-instruction-tuned';
const HF_MODEL = 'https://huggingface.co/aishwarya/llm-124m-instruction-tuned';

/* Architecture, straight from llm_124m/config.py. */
const CONFIG: [string, string][] = [
  ['hidden_dim', '768'], ['layers', '12'], ['heads', '12'], ['kv_heads', '4 (GQA)'],
  ['ffn', 'SwiGLU ×4'], ['vocab', '50,304'], ['ctx', '2,048'], ['positions', 'RoPE'],
  ['batch', '524,288 tok'], ['peak_lr', '6e-4'], ['warmup', '2,000 steps'], ['tokens', '4B'],
];

/* [batch, train, val] — sampled from POSTTRAINING_RESULT/validation_log_continued.txt */
const LOSS: [number, number, number][] = [
  [50, 1.865, 2.0827], [100, 1.923, 2.0823], [150, 1.951, 2.0819],
  [200, 1.970, 2.0815], [250, 1.977, 2.0812], [300, 1.977, 2.0810],
  [350, 1.988, 2.0808], [400, 1.993, 2.0807], [450, 1.991, 2.0804],
  [1350, 1.982, 2.0778], [1550, 1.981, 2.0778], [1750, 1.978, 2.0778],
];

function LossChart() {
  const W = 560, H = 150, P = 10;
  const xs = LOSS.map((p) => p[0]);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const all = LOSS.flatMap((p) => [p[1], p[2]]);
  const minY = Math.min(...all), maxY = Math.max(...all);
  const x = (v: number) => P + ((v - minX) / (maxX - minX)) * (W - 2 * P);
  const y = (v: number) => H - P - ((v - minY) / (maxY - minY)) * (H - 2 * P);
  const train = LOSS.map((p) => `${x(p[0])},${y(p[1])}`).join(' ');
  const val = LOSS.map((p) => `${x(p[0])},${y(p[2])}`).join(' ');
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="Training and validation loss">
      <line x1={P} y1={H - P} x2={W - P} y2={H - P} stroke="rgba(255,255,255,0.08)" />
      <line x1={P} y1={P} x2={P} y2={H - P} stroke="rgba(255,255,255,0.08)" />
      <polyline points={train} fill="none" stroke="#38bdf8" strokeWidth="1.6" />
      <polyline points={val} fill="none" stroke="#a78bfa" strokeWidth="1.6" strokeDasharray="3 3" />
    </svg>
  );
}

function Card({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return (
    <div className={'rounded-xl border border-white/10 bg-white/[0.03] ' + className}>
      {children}
    </div>
  );
}

function SectionTitle({ children, icon }: { children: React.ReactNode; icon?: React.ReactNode }) {
  return (
    <h2 className="mb-3 flex items-center gap-2 text-[13px] font-semibold uppercase tracking-[0.14em] text-zinc-400">
      {icon}{children}
    </h2>
  );
}

export default function ModelLab() {
  const [tab, setTab] = useState<'overview' | 'playground'>('overview');
  // Is the local Model Lab app (port 7861) actually running? A blank iframe is
  // indistinguishable from a broken page, so ask the backend and show a helpful
  // placeholder when it is down.
  const [labUp, setLabUp] = useState<boolean | null>(null);
  useEffect(() => {
    let alive = true;
    api.modelLabStatus()
      .then((d) => { if (alive) setLabUp(!!d.up); })
      .catch(() => { if (alive) setLabUp(false); });
    return () => { alive = false; };
  }, []);

  return (
    <div className="flex h-full min-h-0 flex-col bg-void text-zinc-200">
      <Rail />
      <div className="min-h-0 flex-1 overflow-auto">
        <div className="mx-auto max-w-5xl px-6 py-6">
          {/* Hero */}
          <div className="mb-6 flex flex-col gap-3">
            <div className="flex items-center gap-2">
              <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-accent/15 text-accent"><Cpu size={18} /></span>
              <div>
                <h1 className="text-[22px] font-semibold leading-tight text-zinc-50">LLM Lab</h1>
                <p className="text-[13px] text-zinc-400">A 124M language model, trained from scratch — pre-training, instruction tuning, and the inference engineering around it.</p>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2 text-[11.5px]">
              <span className="rounded-full bg-accent/15 px-2 py-0.5 font-semibold text-accent">124M params</span>
              <span className="rounded-full bg-white/[0.06] px-2 py-0.5 text-zinc-300">4B tokens</span>
              <span className="rounded-full bg-white/[0.06] px-2 py-0.5 text-zinc-300">[INST] format</span>
              <span className="rounded-full bg-white/[0.06] px-2 py-0.5 text-zinc-300">GPT-2 style</span>
              <span className="flex-none" />
              <a href={HF_MODEL} target="_blank" rel="noreferrer" className="flex items-center gap-1 rounded-md border border-white/10 px-2 py-1 text-zinc-300 hover:bg-white/5 hover:text-zinc-100"><Code2 size={12} /> Model card</a>
              <a href={HF_SPACE} target="_blank" rel="noreferrer" className="flex items-center gap-1 rounded-md border border-white/10 px-2 py-1 text-zinc-300 hover:bg-white/5 hover:text-zinc-100"><ExternalLink size={12} /> HF Space</a>
            </div>
          </div>

          {/* Tabs */}
          <div className="mb-5 flex items-center gap-1 rounded-lg border border-white/10 bg-white/[0.02] p-0.5 text-[12.5px]">
            <button onClick={() => setTab('overview')} className={'rounded-md px-3 py-1.5 ' + (tab === 'overview' ? 'bg-white/10 text-zinc-100' : 'text-zinc-400 hover:text-zinc-200')}>Overview</button>
            <button onClick={() => setTab('playground')} className={'rounded-md px-3 py-1.5 ' + (tab === 'playground' ? 'bg-white/10 text-zinc-100' : 'text-zinc-400 hover:text-zinc-200')}>Playground</button>
          </div>

          {tab === 'overview' ? (
            <div className="flex flex-col gap-6">
              {/* Metrics */}
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
                {METRICS.map((m) => (
                  <Card key={m.label} className="px-3 py-2.5">
                    <p className="text-[10px] font-medium uppercase tracking-[0.14em] text-zinc-500">{m.label}</p>
                    <p className="mt-0.5 text-[18px] font-semibold tabular-nums text-zinc-50">{m.value}</p>
                    <p className="text-[10.5px] text-zinc-500">{m.sub}</p>
                  </Card>
                ))}
              </div>

              {/* Pipeline */}
              <div>
                <SectionTitle icon={<Layers size={13} />}>Training pipeline</SectionTitle>
                <div className="grid gap-3 sm:grid-cols-3">
                  {PIPELINE.map((p, i) => (
                    <Card key={p.step} className="relative px-3 py-3">
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-[11px] text-zinc-600">{p.step}</span>
                        <span className={'text-[13px] font-semibold ' + p.tone}>{p.title}</span>
                      </div>
                      <p className="mt-1 text-[11.5px] leading-relaxed text-zinc-400">{p.detail}</p>
                      {i < PIPELINE.length - 1 && <ArrowRight size={13} className="absolute -right-2 top-1/2 hidden -translate-y-1/2 text-zinc-700 sm:block" />}
                    </Card>
                  ))}
                </div>
              </div>

              {/* Architecture + training log */}
              <div className="grid gap-3 lg:grid-cols-2">
                <Card className="px-3 py-3">
                  <SectionTitle icon={<Cpu size={13} />}>Architecture</SectionTitle>
                  <div className="mb-2 flex flex-wrap items-center gap-1 text-[10.5px] text-zinc-500">
                    <span className="rounded bg-white/[0.06] px-1.5 py-0.5">tokens</span>
                    <ArrowRight size={10} />
                    <span className="rounded bg-white/[0.06] px-1.5 py-0.5">embed</span>
                    <ArrowRight size={10} />
                    <span className="rounded bg-accent/15 px-1.5 py-0.5 text-accent">12× block</span>
                    <ArrowRight size={10} />
                    <span className="rounded bg-white/[0.06] px-1.5 py-0.5">LM head</span>
                  </div>
                  <div className="grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-3">
                    {CONFIG.map(([k, v]) => (
                      <div key={k} className="flex items-baseline justify-between gap-2 border-b border-white/5 py-0.5">
                        <span className="font-mono text-[10.5px] text-zinc-500">{k}</span>
                        <span className="font-mono text-[11px] text-zinc-200">{v}</span>
                      </div>
                    ))}
                  </div>
                </Card>
                <Card className="px-3 py-3">
                  <SectionTitle icon={<Gauge size={13} />}>Training log</SectionTitle>
                  <div className="mb-1 flex items-center gap-3 text-[10.5px] text-zinc-500">
                    <span className="flex items-center gap-1"><span className="h-0.5 w-3 bg-sky-400" />train</span>
                    <span className="flex items-center gap-1"><span className="h-0.5 w-3 border-t border-dashed border-violet-400" />val</span>
                    <span className="ml-auto">epoch 2–3 · batch 50–1750</span>
                  </div>
                  <LossChart />
                </Card>
              </div>

              {/* Strengths / limits / future */}
              <div className="grid gap-3 sm:grid-cols-3">
                <Card className="px-3 py-3">
                  <SectionTitle icon={<CheckCircle2 size={13} />}>Strengths</SectionTitle>
                  <ul className="space-y-1.5">
                    {STRENGTHS.map((s) => <li key={s} className="flex gap-2 text-[12px] text-zinc-300"><CheckCircle2 size={12} className="mt-0.5 flex-none text-emerald-400" />{s}</li>)}
                  </ul>
                </Card>
                <Card className="px-3 py-3">
                  <SectionTitle icon={<AlertTriangle size={13} />}>Limits</SectionTitle>
                  <ul className="space-y-1.5">
                    {LIMITS.map((s) => <li key={s} className="flex gap-2 text-[12px] text-zinc-300"><AlertTriangle size={12} className="mt-0.5 flex-none text-amber-400" />{s}</li>)}
                  </ul>
                </Card>
                <Card className="px-3 py-3">
                  <SectionTitle icon={<Sparkles size={13} />}>Next</SectionTitle>
                  <ul className="space-y-1.5">
                    {FUTURE.map((s) => <li key={s} className="flex gap-2 text-[12px] text-zinc-300"><ArrowRight size={12} className="mt-0.5 flex-none text-sky-400" />{s}</li>)}
                  </ul>
                </Card>
              </div>

              {/* Screenshots */}
              <div>
                <SectionTitle icon={<Gauge size={13} />}>The lab</SectionTitle>
                <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                  {SHOTS.map((s) => (
                    <a key={s.src} href={s.src} target="_blank" rel="noreferrer" className="group overflow-hidden rounded-xl border border-white/10 bg-white/[0.02]">
                      <img src={s.src} alt={s.label} className="aspect-[16/10] w-full object-cover object-top transition group-hover:opacity-90" />
                      <p className="px-3 py-1.5 text-[11.5px] text-zinc-400 group-hover:text-zinc-200">{s.label}</p>
                    </a>
                  ))}
                </div>
              </div>
            </div>
          ) : (
            <div className="flex flex-col gap-3">
              <Card className="overflow-hidden">
                <div className="flex items-center justify-between border-b border-white/10 px-3 py-2">
                  <span className="flex items-center gap-2 text-[12.5px] text-zinc-300"><Play size={13} className="text-accent" /> Live playground</span>
                  <span className="flex items-center gap-2 text-[11px]">
                    <span className={'mr-2 inline-block h-1.5 w-1.5 rounded-full ' + (labUp ? 'bg-emerald-400' : labUp === false ? 'bg-amber-400' : 'bg-zinc-500')} />
                    <span className="text-zinc-500">model_lab · :7861</span>
                  </span>
                </div>
                {labUp === true ? (
                  <iframe
                    src="http://127.0.0.1:7861"
                    title="Model Lab playground"
                    sandbox="allow-scripts allow-forms allow-popups allow-downloads"
                    className="h-[560px] w-full bg-[#111113]"
                  />
                ) : (
                  <div className="flex h-[300px] flex-col items-center justify-center gap-3 px-6 text-center">
                    <span className="flex h-11 w-11 items-center justify-center rounded-xl bg-amber-400/10 text-amber-300"><Terminal size={20} /></span>
                    <p className="text-[14px] font-medium text-zinc-200">
                      {labUp === false ? 'The Model Lab app is not running' : 'Checking the Model Lab app…'}
                    </p>
                    <p className="max-w-md text-[12px] leading-relaxed text-zinc-500">
                      Start it locally with
                      <code className="mx-1 rounded bg-white/[0.06] px-1.5 py-0.5 font-mono text-[11px] text-zinc-300">python run_model_lab.py</code>
                      in the training folder, then switch back here. The Overview tab has captured screenshots of the lab in the meantime.
                    </p>
                  </div>
                )}
              </Card>
              <p className="flex items-center gap-2 text-[11.5px] text-zinc-500">
                <Terminal size={12} />
                Start it locally with
                <code className="rounded bg-white/[0.06] px-1.5 py-0.5 font-mono text-[11px] text-zinc-300">python run_model_lab.py</code>
                in the training folder. If it isn't running, the Overview tab has the captured screenshots.
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}