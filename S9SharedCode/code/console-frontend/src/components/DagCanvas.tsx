import { useCallback, useEffect, useMemo, useRef } from 'react';
import {
  ReactFlow,
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  type Edge,
  type NodeChange,
  type ReactFlowInstance,
} from '@xyflow/react';
import AgentNode, { type AgentNodeType } from './AgentNode';
import { classifyStatus } from './markdown';
import type { GraphPayload } from '../api';

/* React Flow's stylesheet belongs to the DAG, not the app shell. Importing
   it here puts it in this chunk's CSS so the 8 routes that never render a
   graph stop downloading ~18KB of it. */
import '@xyflow/react/dist/style.css';

const nodeTypes = { agent: AgentNode };

const NW = 200;
const GX = 90;
const ROW_H = 110;
const WRAP_GAP = 40;
/** Max nodes stacked in one column before wrapping into a sub-column, so a
 *  wide fan-out stays a balanced grid instead of a super-tall sliver that
 *  fitView has to shrink to a dot. */
const MAX_ROWS = 8;

/** Shared keyword classifier, adapted to the node visuals. Skipped and
    cancelled nodes get their own amber visual: lumping them into `idle`
    made them indistinguishable from pending, so a stopped run looked like
    a run that had never started. */
function statusOf(st?: string): 'run' | 'ok' | 'err' | 'idle' | 'skip' {
  if (/skip|cancel/i.test(st || '')) return 'skip';
  const c = classifyStatus(st);
  if (c === 'err') return 'err';
  if (c === 'info') return 'run';
  if (c === 'ok') return 'ok';
  return 'idle';
}

function toFlow(g: GraphPayload, running: boolean, selectedId: string | null) {
  // layered layout by topological depth (same algorithm as the classic UI)
  const byId = new Map(g.nodes.map((n) => [n.id, n]));
  const children = new Map<string, string[]>();
  const indeg = new Map<string, number>();
  g.nodes.forEach((n) => indeg.set(n.id, 0));
  g.edges.forEach((e) => {
    if (!byId.has(e.from) || !byId.has(e.to)) return;
    if (!children.has(e.from)) children.set(e.from, []);
    children.get(e.from)!.push(e.to);
    indeg.set(e.to, (indeg.get(e.to) || 0) + 1);
  });
  const depth = new Map<string, number>();
  const queue: string[] = [];
  g.nodes.forEach((n) => {
    if (!indeg.get(n.id)) {
      depth.set(n.id, 0);
      queue.push(n.id);
    }
  });
  g.nodes.forEach((n) => {
    if (!depth.has(n.id)) {
      depth.set(n.id, 0);
      queue.push(n.id);
    }
  });
  while (queue.length) {
    const id = queue.shift()!;
    for (const ch of children.get(id) || []) {
      if ((depth.get(ch) || 0) < (depth.get(id) || 0) + 1) depth.set(ch, (depth.get(id) || 0) + 1);
      indeg.set(ch, (indeg.get(ch) || 1) - 1);
      if (!indeg.get(ch)) queue.push(ch);
    }
  }
  const cols = new Map<number, typeof g.nodes>();
  g.nodes.forEach((n) => {
    const d = depth.get(n.id) || 0;
    if (!cols.has(d)) cols.set(d, []);
    cols.get(d)!.push(n);
  });

  const nodes: AgentNodeType[] = [];
  const depths = [...cols.keys()].sort((a, b) => a - b);
  // Each depth column reserves width for its wrapped sub-columns so a
  // wrapped fan-out never bleeds into the next depth column.
  const colX = new Map<number, number>();
  {
    let xCursor = 0;
    for (const d of depths) {
      colX.set(d, xCursor);
      const subCols = Math.max(1, Math.ceil(cols.get(d)!.length / MAX_ROWS));
      xCursor += (subCols - 1) * (NW + WRAP_GAP) + (NW + GX);
    }
  }
  depths.forEach((d) => {
    cols.get(d)!.forEach((n, ri) => {
      const sk = String(n.skill || n.id);
      const sub = Math.floor(ri / MAX_ROWS);
      const row = ri % MAX_ROWS;
      nodes.push({
        id: n.id,
        type: 'agent',
        // React Flow nodes are focusable by default, but without an explicit
        // label a screen reader announced only the inner text with no skill or
        // status context. Name each node so tabbing through a 40-node run is
        // actually navigable.
        ariaLabel: `${sk.replace(/agent$/i, '')} — ${n.status || 'pending'}`,
        position: { x: (colX.get(d) || 0) + sub * (NW + WRAP_GAP), y: row * ROW_H },
        data: {
          cap: (sk.replace(/agent$/i, '').toUpperCase().slice(0, 20) || 'STEP') as string,
          sub: ((sk.match(/agent$/i) ? sk : sk + 'Agent').slice(0, 26)) as string,
          status: statusOf(n.status),
          state: (n.status || '').slice(0, 12),
          selected: n.id === selectedId,
        },
      });
    });
  });
  const edges: Edge[] = g.edges
    .filter((e) => byId.has(e.from) && byId.has(e.to))
    .map((e, i) => ({
      id: `${e.from}->${e.to}#${i}`,
      source: e.from,
      target: e.to,
      type: 'smoothstep',
      animated: running,
      style: { stroke: running ? '#8b7cf6' : '#3a3a46', strokeWidth: 1.5 },
    }));
  return { nodes, edges };
}

export default function DagCanvas({
  graph,
  running,
  selectedId,
  onSelect,
}: {
  graph: GraphPayload | null;
  running: boolean;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const [nodes, setNodes, onNodesChange] = useNodesState<AgentNodeType>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const lastSig = useRef('');
  const rfRef = useRef<ReactFlowInstance<AgentNodeType, Edge> | null>(null);
  /** Dragged positions, so status-only updates never snap nodes back. */
  const posRef = useRef(new Map<string, { x: number; y: number }>());
  /** Structure already fitted, so fitView runs on shape changes — not on
   *  every status flip (which would yank the viewport mid-inspection). */
  const fittedStruct = useRef('');

  const sig = useMemo(() => {
    if (!graph) return '';
    return (
      graph.nodes.length + ':' + graph.nodes.map((n) => n.id + (n.status || '')).join(',') +
      `|${running ? 1 : 0}|${selectedId || ''}`
    );
  }, [graph, running, selectedId]);

  const structSig = useMemo(() => {
    if (!graph) return '';
    const ids = graph.nodes.map((n) => n.id).sort().join(',');
    const es = graph.edges.map((e) => `${e.from}>${e.to}`).sort().join(',');
    return `${ids}|${es}`;
  }, [graph]);

  useEffect(() => {
    if (!graph || sig === lastSig.current) return; // signature guard — no churn
    lastSig.current = sig;
    const f = toFlow(graph, running, selectedId);
    // Preserve user-dragged positions for nodes we already placed.
    for (const n of f.nodes) {
      const p = posRef.current.get(n.id);
      if (p) n.position = p;
    }
    setNodes(f.nodes);
    setEdges(f.edges);
    if (structSig !== fittedStruct.current) {
      fittedStruct.current = structSig;
      // Fit AFTER ReactFlow commits the new nodes, on the next frame.
      requestAnimationFrame(() => {
        rfRef.current?.fitView({ padding: 0.2, maxZoom: 1 });
      });
    }
  }, [sig, structSig, graph, running, selectedId, setNodes, setEdges]);

  const handleNodesChange = useCallback(
    (changes: NodeChange<AgentNodeType>[]) => {
      onNodesChange(changes);
      for (const ch of changes) {
        if (ch.type === 'position' && ch.position) {
          posRef.current.set(ch.id, { x: ch.position.x, y: ch.position.y });
        }
      }
    },
    [onNodesChange],
  );

  /* Opening the inspector steals 400px from this pane, but the graph keeps
     the zoom it was fitted to — nodes laid out for the wide pane end up
     hidden underneath the panel (the third node of a 3-node run was
     unreadable). Re-fit when the WIDTH changes materially: that covers the
     inspector opening/closing and window resizes, while deliberately not
     re-fitting on every poll, which would yank the viewport out from under
     a user who has panned or zoomed. */
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const lastW = useRef(0);
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const w = Math.round(entries[0].contentRect.width);
      if (!w) return;
      const prev = lastW.current;
      lastW.current = w;
      if (prev && Math.abs(w - prev) > 40) {
        requestAnimationFrame(() => {
          rfRef.current?.fitView({ padding: 0.2, maxZoom: 1 });
        });
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [graph ? 1 : 0]);

  const onNodeClick = useCallback(
    (_: unknown, node: { id: string }) => onSelect(node.id),
    [onSelect],
  );

  const setRfInstance = useCallback((inst: ReactFlowInstance<AgentNodeType, Edge> | null) => {
    rfRef.current = inst;
  }, []);

  if (!graph || !graph.nodes.length) return null;
  return (
    <div className="h-full w-full" ref={wrapRef}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={handleNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={onNodeClick}
        onInit={setRfInstance}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.2, maxZoom: 1 }}
        minZoom={0.15}
        maxZoom={1.75}
        proOptions={{ hideAttribution: false }}
        colorMode="dark"
      >
        <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="#1d1d25" />
        <Controls showInteractive={false} />
        <MiniMap pannable zoomable className="!bg-[#0e0e12]" />
      </ReactFlow>
    </div>
  );
}
