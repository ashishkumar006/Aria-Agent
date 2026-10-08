import { memo } from 'react';
import { Handle, Position, type Node, type NodeProps } from '@xyflow/react';
import { Bot } from 'lucide-react';

export type AgentNodeData = {
  cap: string;
  sub: string;
  status: 'run' | 'ok' | 'err' | 'idle' | 'skip';
  /** Raw node status (running / complete / skipped / ...) shown as a chip. */
  state?: string;
  selected?: boolean;
  /** Opens this node's inspector. Passed in as data because React Flow renders
      the node component itself and does not hand it the canvas callbacks. */
  onSelect?: () => void;
  [k: string]: unknown;
};

export type AgentNodeType = Node<AgentNodeData, 'agent'>;

const RING: Record<AgentNodeData['status'], string> = {
  run: 'border-violet-400 shadow-[0_0_16px_rgba(139,124,246,0.45)]',
  ok: 'border-white/15',
  err: 'border-red-400/70',
  idle: 'border-white/10 opacity-70',
  skip: 'border-dashed border-amber-400/50 opacity-80',
};

const SUB: Record<AgentNodeData['status'], string> = {
  run: 'text-violet-300',
  ok: 'text-emerald-300',
  err: 'text-red-300',
  idle: 'text-zinc-muted',
  skip: 'text-amber-200',
};

function AgentNode({ data }: NodeProps<AgentNodeType>) {
  /* React Flow makes its nodes focusable, so a keyboard user can Tab onto a
     node - but selection was wired only to onNodeClick, so Enter and Space did
     nothing at all (measured: a mouse click opened the Inspector, Enter and
     Space did not). A focusable thing that cannot be operated is worse than
     one that is skipped, so the key handling belongs here, on the element that
     actually receives the event. */
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar') return;
    e.preventDefault();
    e.stopPropagation();
    data.onSelect?.();
  };
  return (
    <div
      role="button"
      aria-pressed={!!data.selected}
      onKeyDown={onKeyDown}
      className={`w-[200px] rounded-[10px] border bg-[#101016] px-3 py-2.5 shadow-[0_8px_24px_rgba(0,0,0,0.45)] transition-shadow ${RING[data.status]} ${
        data.selected ? 'ring-2 ring-violet-400' : ''
      }`}
    >
      <Handle type="target" position={Position.Left} className="!bg-zinc-600 !border-zinc-500" />
      <div className="flex items-center gap-2">
        <span className="flex h-7 w-7 flex-none items-center justify-center rounded-md border border-violet-400/30 bg-violet-400/10">
          <Bot size={15} className="text-violet-300" />
        </span>
        <div className="min-w-0">
          <div className="truncate text-[11px] font-bold tracking-wide text-zinc-200">{data.cap}</div>
          <div className={`truncate text-[11px] font-semibold ${SUB[data.status]}`}>{data.sub}</div>
        </div>
        {data.state ? (
          <span
            className={`ml-auto max-w-[86px] truncate rounded-full border border-white/10 bg-white/5 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide ${SUB[data.status]}`}
          >
            {data.state}
          </span>
        ) : null}
      </div>
      <Handle type="source" position={Position.Right} className="!bg-zinc-600 !border-zinc-500" />
    </div>
  );
}

export default memo(AgentNode);
