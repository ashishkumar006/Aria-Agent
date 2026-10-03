/* Typed client over the agent's existing REST + SSE surface. */

/** Single source of truth for environment knobs (ports, poll cadence).
    Views must import these instead of hardcoding numbers/URLs. */
export const CONF = {
  agentPort: 8500,
  gatewayPort: 8109,
  gatewayHost: '127.0.0.1',
  pollSlowMs: 15000, // list refreshes (runs, topics, ledger…)
  pollFastMs: 2000, // live graph polling during a run
  pollLiveMs: 3000, // console event feed
  pollCatchUpMs: 1000, // post-stop graph catch-up ticks
  clockTickMs: 1000, // elapsed-time badges
  healthMs: 15000, // status-dot heartbeat
} as const;

/** Base URL for opening the gateway's own pages in a new tab. */
export function gatewayUrl(path = '/'): string {
  return `http://${CONF.gatewayHost}:${CONF.gatewayPort}${path}`;
}

export interface GraphNode {
  id: string;
  skill?: string;
  status?: string;
  /** Wall-clock timing, stamped by the executor into the graph rollup on
      every mark() — the 2s poll carries per-node durations for free, which
      is what powers the Timeline strip (and what finally answers "why did
      this run take 310s" without opening any node file). */
  started_at?: number | null;
  completed_at?: number | null;
  elapsed_s?: number | null;
}

export interface GraphEdge {
  from: string;
  to: string;
}

export interface GraphPayload {
  nodes: GraphNode[];
  edges: GraphEdge[];
  query?: string;
}

/** A planner-emitted successor spec. The planner returns objects, not bare
    strings — `{skill, inputs, metadata}` — so this union is load-bearing:
    rendering it as `string[]` produced React error #31 and blanked the
    whole console the moment any node was inspected. */
export interface SuccessorSpec {
  skill?: string;
  inputs?: string[];
  metadata?: { label?: string };
}

export interface NodeDetail {
  node_id?: string;
  skill?: string;
  status?: string;
  inputs?: string[];
  /** NodeState names this `prompt_sent`; `prompt` never exists on the wire,
    so reading it made the AGENT GOAL panel permanently blank. */
  prompt_sent?: string;
  started_at?: number;
  completed_at?: number;
  retries?: number;
  result?: {
    output?: unknown;
    successors?: (string | SuccessorSpec)[];
    agent_name?: string;
    cost?: number | string;
    provider?: string;
    /** Failed nodes carry the reason here (see Inspector's ERROR panel). */
    error?: string;
    elapsed_s?: number;
  };
  error?: string;
}

export interface SessionSummary {
  session_id: string;
  query?: string;
  /** The user's own question, extracted server-side from the skill prompt
      that wraps it. `query` is still that whole prompt, so titles used to
      read "You are a research agent..."; prefer this. */
  topic?: string;
  nodes?: number;
  skills?: string[];
  updated_ago?: number;
  conversation_id?: string | null;
  status_counts?: Record<string, number>;
  usd?: number;
  updated?: number;
}

export interface McpStats {
  cache?: {
    enabled: boolean | null;
    hits: number;
    misses: number;
    hit_rate: number;
    entries: number;
    ttl_days: number;
  };
  tool_outcomes?: { queued: number; written: number; dropped: number; errors: number } | null;
  mcp_breaker?: {
    open: boolean;
    consecutive_failures: number;
    trips: number;
    threshold: number;
    cooldown_s: number;
    last_error: string;
  } | null;
}

export interface RunRow extends SessionSummary {}

export interface FeedbackRow {
  name: string;
  up: number;
  down: number;
  total: number;
  /** (up - down) / total, in [-1, 1]. 0 when there are no votes. */
  net: number;
  /** "low" means fewer than `min_votes` — do not read it as a rate. */
  confidence: 'ok' | 'low';
}

export interface FeedbackRollup {
  window_days: number;
  totals: { up: number; down: number; total: number; net: number };
  by_skill: FeedbackRow[];
  by_node: FeedbackRow[];
  trend: { day: string; up: number; down: number }[];
  unlabelled?: number;
  note?: string;
}

export interface ScheduleJob {
  id: string;
  query?: string;
  when?: string;
  recurring?: string;
  next_fire?: number;
  enabled?: boolean;
  last_fire?: number;
  last_error?: string;
  created?: number;
}

export interface ToolSpec {
  name: string;
  kind?: string;
  description?: string;
  params?: { properties?: Record<string, unknown> };
}

export interface MemItem {
  id?: string;
  kind?: string;
  drawer?: string;
  descriptor?: string;
  keywords?: string[];
  source?: string;
  superseded_by?: string;
}

export interface DocItem {
  id: string;
  filename: string;
  doc_type: string;
  size_bytes: number;
  /** pending | parsing | chunking | embedding | ready | blocked | failed */
  status: string;
  enabled: boolean;
  chunk_count: number;
  embedded_count: number;
  /** 0..1, only meaningful while indexing. */
  progress?: number;
  warnings?: string[];
  error?: string;
  embed_model?: string;
  embed_dim?: number;
  /** Present on the detail response. */
  chunks?: DocChunk[];
  chunk_error?: string;
}

export interface DocChunk {
  index: number;
  kind: string;
  heading_path?: string[];
  page?: number | null;
  words: number;
  embedded: boolean;
  preview: string;
}

export interface DocHit {
  id: string;
  doc_id: string;
  chunk_index: number;
  page?: number | null;
  descriptor?: string;
  chunk: string;
  embed_model?: string | null;
}

/** One entry from `/api/code/tree`. */
export interface CodeEntry {
  name: string;
  dir: boolean;
  size?: number;
}

/** One file from `/api/code/file`. Served read-only by the agent. */
export interface CodeFile {
  path: string;
  name: string;
  text: string;
  bytes: number;
  lines: number;
  truncated: boolean;
  language: string;
  /** Cheap freshness token (mtime+size) so a reload can detect drift. */
  version: string;
}

/** One diagnostic from `/api/code/check`. Lines and columns are 1-based,
    matching Python and every editor's convention. */
export interface CodeProblem {
  line: number;
  col: number;
  message: string;
  severity: 'error' | 'warning' | 'info';
}

export interface CodeCheck {
  /** `ast` = real parser, `balance` = delimiter heuristic, `none` = no parser. */
  mode: 'ast' | 'balance' | 'none';
  language: string;
  checked: boolean;
  problems: CodeProblem[];
  note?: string;
  source?: { line: number; text: string };
}

/** One hit from `/api/code/search`. */
export interface CodeMatch {
  path: string;
  line: number;
  col: number;
  text: string;
}

export interface SkillRow {
  skill: string;
  calls?: number;
  in_tokens?: number;
  out_tokens?: number;
  dollars?: number;
}

export interface TurnRow {
  ts?: number;
  query?: string;
  calls?: number;
  usd?: number;
}

export interface FeedEvent {
  t: number;
  iso: string;
  level: string;
  src: string;
  msg: string;
}

export type ChatEvent =
  | { type: 'started'; session_id?: string; conversation_id?: string }
  | { type: 'log'; text?: string }
  | { type: 'done'; answer?: string; session_id?: string }
  | { type: 'error'; text?: string }
  | { type: string; [k: string]: unknown };

export type SimpleStreamEvent =
  | { type: 'started'; conversation_id?: string }
  | { type: 'status'; text?: string }
  | { type: 'delta'; text?: string }
  | { type: 'done'; answer?: string; conversation_id?: string }
  | { type: 'error'; text?: string }
  | { type: string; [k: string]: unknown };

/** Optional bearer token. The agent gates every `/api/*` route when
    ARIA_API_TOKEN is set; without this the whole console 401s with no way
    to recover, so the token is read once from localStorage (set via the
    Settings page or a `?token=` bootstrap link) and sent from one place. */
export const TOKEN_KEY = 'aria_token';
export function getToken(): string {
  try { return localStorage.getItem(TOKEN_KEY) || ''; } catch { return ''; }
}
export function setToken(t: string): void {
  try {
    if (t) localStorage.setItem(TOKEN_KEY, t);
    else localStorage.removeItem(TOKEN_KEY);
  } catch { /* private mode: token simply won't persist */ }
}

/* The per-launch token the agent injects into this document as
   `<meta name="aria-token">`. Read once, at module load.

   Why a meta tag rather than a fetch: the agent serves this page, so it is
   same-origin, and a cross-origin page cannot read the response body. That is
   exactly the property that closes CSRF - a custom header cannot be set
   cross-origin without a preflight the agent never answers - while doing
   nothing against another process running as the same OS user.

   It is deliberately NOT persisted. Persisting a per-launch secret to
   localStorage would leave a live credential behind after a restart, which is
   the opposite of "per-launch". */
const LAUNCH_TOKEN: string = (() => {
  try {
    return document.querySelector('meta[name="aria-token"]')?.getAttribute('content') || '';
  } catch { return ''; }
})();

export function hasLaunchToken(): boolean { return LAUNCH_TOKEN !== ''; }

function headers(extra?: Record<string, string>): Record<string, string> {
  const h: Record<string, string> = { ...(extra || {}) };
  const t = getToken();
  if (t) h.Authorization = `Bearer ${t}`;
  // The per-launch token travels as its own header, not as the bearer. It is
  // required on every /api/* call when the agent has one, and a cross-origin
  // page cannot set it.
  if (LAUNCH_TOKEN) h['X-Aria-Token'] = LAUNCH_TOKEN;
  return h;
}

/** Pull a human-readable reason out of a failed response. The server uses
    `{"error": …}` for its own 4xx/5xx and FastAPI's `{"detail": …}` for
    framework-raised ones, and the gateway's own errors arrive as `detail`
    too — so check all three before falling back to the status line. */
async function reason(r: Response): Promise<string> {
  let body: unknown = null;
  try { body = await r.json(); } catch { /* not json: use the status */ }
  const b = body as { error?: unknown; detail?: unknown; message?: unknown } | null;
  const pick = [b?.error, b?.detail, b?.message]
    .map((v) => (typeof v === 'string' ? v : Array.isArray(v) ? JSON.stringify(v) : ''))
    .find((v) => v.trim().length > 0);
  return pick ? `${r.status} ${pick}` : `HTTP ${r.status} ${r.url}`;
}

async function j<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, { ...init, headers: headers(init?.headers as Record<string, string>) });
  if (!r.ok) throw new Error(await reason(r));
  return (await r.json()) as T;
}

/** POST a JSON body through the same pipeline as `j` (auth header + server
    error text). Every POST in this client must go through here: hand-rolled
    `fetch` calls drifted — some dropped the Authorization header, some
    stringified differently — and each drift was a latent auth or
    contract bug. */
function pj<T>(url: string, body: unknown, init?: RequestInit): Promise<T> {
  return j<T>(url, {
    ...init,
    method: 'POST',
    headers: headers({ 'Content-Type': 'application/json', ...((init?.headers || {}) as Record<string, string>) }),
    body: JSON.stringify(body),
  });
}

async function safe<T>(p: Promise<T>): Promise<T | null> {
  try {
    return await p;
  } catch {
    return null;
  }
}

export interface ChatThread {
  conversation_id: string;
  title?: string;
  updated?: number;
  turns?: number;
}

export interface FlagRow {
  name: string;
  type: string;
  value: unknown;
  default: unknown;
  source: 'prefab' | 'override' | 'default';
  description?: string;
}

export interface TrackerApp {
  id: string;
  name: string;
  kind: string;
  schedule: string;
  next_refresh: number | null;
  refreshed_at: number | null;
  count: number;
  added: number;
  removed: number;
  error: string | null;
}

export interface TrackerAppDetail extends TrackerApp {
  spec: Record<string, unknown>;
  items: Record<string, unknown>[];
  added_items: Record<string, unknown>[];
  removed_ids: string[];
  history: { ts: number; count: number; added: string[]; removed: string[] }[];
}

/** SSE frame reader shared by every streaming endpoint.
    Handles the three things a naive `indexOf('\n\n')` loop gets wrong:
      1. `\r\n\r\n` frame separators (legal SSE; a CRLF-normalising proxy
         emits them, and `'\r\n\r\n'` does not contain `'\n\n'`, so every
         frame would be silently dropped and the UI would show an empty
         answer with no error);
      2. a final frame truncated by a dropped connection — the residual
         buffer is flushed rather than discarded, otherwise a missing
         `done` reads as a successful, complete answer;
      3. frames split across or coalesced within TCP chunks.
    Yields the parsed JSON of each `data:` payload. */
async function* sseFrames(r: Response, signal?: AbortSignal): AsyncGenerator<Record<string, unknown>> {
  if (!r.body) throw new Error('empty response body');
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  const parse = (chunk: string): Record<string, unknown> | null => {
    // A frame is a blank-line-delimited block. Skip comments (`:`), and
    // `event:`/`id:`/`retry:` fields; join multi-line `data:` per the spec.
    const data = chunk.split('\n')
      .filter((l) => l.startsWith('data:'))
      .map((l) => l.slice(5).replace(/^ /, ''))
      .join('\n');
    if (!data) return null;
    try { return JSON.parse(data) as Record<string, unknown>; } catch { return null; }
  };
  try {
    for (;;) {
      if (signal?.aborted) { try { await reader.cancel(); } catch { /* already closed */ } break; }
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true }).replace(/\r\n/g, '\n');
      let idx: number;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const frame = parse(buf.slice(0, idx));
        buf = buf.slice(idx + 2);
        if (frame) yield frame;
      }
    }
    const tail = parse(buf); // a frame the server never terminated
    if (tail) yield tail;
  } finally {
    try { reader.releaseLock(); } catch { /* already released */ }
  }
}

export const api = {
  health: () => j<{ agent: string; gateway_up: boolean }>('/api/health'),
  chatThreads: (limit = 50) =>
    j<{ threads: ChatThread[] }>(`/api/chat/threads?limit=${limit}`),
  chatThread: (cid: string) =>
    j<{ conversation_id: string; title: string; messages: { role: string; content: string }[] }>(
      `/api/chat/threads/${encodeURIComponent(cid)}`,
    ),
  chatThreadDelete: (cid: string) =>
    j<{ status: string }>(`/api/chat/threads/${encodeURIComponent(cid)}`, { method: 'DELETE' }),
  chatSimple: (query: string, conversationId?: string) =>
    pj<{ answer: string; conversation_id: string; error?: string }>(`/api/chat/simple`, {
      query, conversation_id: conversationId || undefined,
    }),
  sessions: (limit = 50) =>
    j<{ sessions: SessionSummary[] }>(`/api/sessions?limit=${limit}`),
  runsSummary: (limit = 200) =>
    j<{ runs: RunRow[]; totals?: { runs?: number; nodes?: number; dollars?: number } }>(
      `/api/runs/summary?limit=${limit}`,
    ),
  /** The graph is always requested `light`: `light=false` makes the server
      serialise every NodeState (untrimmed, "some nodes are megabytes") into
      a `node_states` array this client never reads. Node detail is fetched
      on demand via `api.node()`, which *is* trimmed. */
  graph: (sid: string) =>
    j<GraphPayload>(`/api/sessions/${encodeURIComponent(sid)}/graph?light=true`),
  node: (sid: string, nid: string) =>
    j<NodeDetail>(`/api/sessions/${encodeURIComponent(sid)}/nodes/${encodeURIComponent(nid)}`),
  cost: (conversationId?: string) =>
    j<{ rows?: { calls?: number; dollars?: number }[] }>(
      `/api/cost${conversationId ? `?conversation_id=${encodeURIComponent(conversationId)}` : ''}`,
    ),
  bySkill: (conversationId?: string) =>
    j<{ rows?: SkillRow[]; turns?: TurnRow[]; totals?: { dollars?: number; calls?: number; skills?: number } }>(
      `/api/cost/by_skill${conversationId ? `?conversation_id=${encodeURIComponent(conversationId)}` : ''}`,
    ),
  scheduleList: () => j<{ schedules?: ScheduleJob[] | Record<string, ScheduleJob> }>('/api/schedule'),
  scheduleCreate: (query: string, when: string) =>
    pj<{ status: string; id?: string; message?: string }>('/api/schedule', { query, when }),
  scheduleCancel: (id: string) =>
    j<{ status: string }>(`/api/schedule/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  scheduleDelete: (id: string) =>
    j<{ status: string }>(`/api/schedule/${encodeURIComponent(id)}?hard=true`, { method: 'DELETE' }),
  tools: () => j<{ tools: ToolSpec[] }>('/api/tools'),
  toolsGuard: () => j<{ disabled: string[] }>('/api/config/tools'),
  setTool: (tool: string, enabled: boolean) =>
    pj<{ disabled?: string[]; error?: string }>('/api/config/tools', { tool, enabled }),
  flags: () =>
    j<{ flags: FlagRow[]; prefab: { configured: boolean; live: boolean } }>('/api/apps/flags'),
  setFlag: (name: string, value: unknown) =>
    pj<{ name?: string; value?: unknown; cleared?: boolean; flags?: FlagRow[]; error?: string }>(
      '/api/apps/flags', { name, value },
    ),
  trackerApps: () => j<{ apps: TrackerApp[] }>('/api/apps'),
  trackerApp: (id: string) =>
    j<TrackerAppDetail>(`/api/apps/${encodeURIComponent(id)}`),
  trackerCreate: (spec: Record<string, unknown>) =>
    pj<{ spec?: Record<string, unknown>; apps?: TrackerApp[]; error?: string }>('/api/apps', spec),
  trackerRefresh: (id: string) =>
    j<{ count?: number; added?: number; removed?: number; error?: string | null; refreshed_at?: number }>(
      `/api/apps/${encodeURIComponent(id)}/refresh`,
      { method: 'POST' },
    ),
  trackerDelete: (id: string) =>
    j<{ deleted?: string; apps?: TrackerApp[] }>(`/api/apps/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }),
  /** Prefab render pre-check. The iframe itself cannot send the bearer
      token or report failures, so this probe (same URL, authed headers)
      decides whether to mount it — and surfaces the server's error text
      instead of a blank white box. A sibling hand-rolled fetch here once
      drifted off the auth path; keep it on `headers()`. */
  prefabCheck: async (id: string): Promise<{ ok: boolean; status: number; text: string }> => {
    const r = await fetch(`/api/apps/${encodeURIComponent(id)}/prefab`, {
      headers: headers({ Accept: 'text/html' }),
    });
    const text = r.ok ? '' : (await r.text()).slice(0, 200);
    return { ok: r.ok, status: r.status, text };
  },
  memory: (params: URLSearchParams, signal?: AbortSignal) =>
    j<{ items?: MemItem[]; error?: string }>(`/api/memory?${params}`, { signal }),
  remember: (body: Record<string, unknown>) =>
    pj<{ status: string; id?: string }>('/api/memory/remember', body),
  /* `confirm=wipe` is required server-side: an unscoped DELETE /api/memory
     deletes every drawer, so the intent has to be explicit. */
  wipeMemory: (sessionId: string) =>
    j<Record<string, unknown>>(
      `/api/memory?session_id=${encodeURIComponent(sessionId)}&confirm=wipe`,
      { method: 'DELETE' }),
  /* Delete exactly one memory row. Distinct from wipeMemory, which clears every
     drawer for a session — that is far too destructive for fixing one
     mis-captured preference. */
  /* Per-conversation document access. Defaults to on; a conversation that
     turns it off gets no document chunks in its prompt. */
  chatThreadPrefs: (id: string) =>
    j<{ prefs?: { use_documents: boolean } }>(
      `/api/chat/threads/${encodeURIComponent(id)}/prefs`),
  setChatThreadPrefs: (id: string, useDocuments: boolean) =>
    pj<{ prefs?: { use_documents: boolean } }>(
      `/api/chat/threads/${encodeURIComponent(id)}/prefs`,
      { use_documents: useDocuments }),
  deleteMemory: (id: string) =>
    j<Record<string, unknown>>(`/api/memory/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  events: (limit = 200) => j<{ events?: FeedEvent[]; count?: number }>(`/api/events?limit=${limit}`),
  config: () => j<{ raw?: string; error?: string }>('/api/config'),
  templates: () => j<{ templates?: unknown[] }>('/api/templates'),
  /* Documents. Upload returns as soon as the bytes are stored; indexing
     continues in the background, so the caller polls the list. */
  documents: (signal?: AbortSignal) =>
    j<{ documents?: DocItem[]; error?: string }>(
      '/api/documents', signal ? { signal } : undefined),
  documentDetail: (id: string) =>
    j<{ document?: DocItem; error?: string }>(
      `/api/documents/${encodeURIComponent(id)}`),
  uploadDocument: (file: File) => {
    const fd = new FormData();
    fd.append('file', file, file.name);
    // NOT pj: it forces Content-Type: application/json, which would override
    // the multipart boundary the browser generates and the server could not
    // parse the file at all.
    return j<{ document: DocItem; warnings?: string[]; error?: string }>(
      '/api/documents', { method: 'POST', body: fd });
  },
  setDocumentEnabled: (id: string, enabled: boolean) =>
    pj<{ document?: DocItem; error?: string }>(
      `/api/documents/${encodeURIComponent(id)}/enabled`, { enabled }),
  reindexDocument: (id: string) =>
    pj<{ document?: DocItem; error?: string }>(
      `/api/documents/${encodeURIComponent(id)}/reindex`, {}),
  deleteDocument: (id: string) =>
    j<{ status?: string; error?: string }>(
      `/api/documents/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  /** Retrieval only - verifies indexing without spending an LLM call. */
  searchDocuments: (query: string, topK = 8) =>
    pj<{ hits?: DocHit[]; error?: string }>('/api/documents/search',
      { query, top_k: topK }),

  /* Code workspace — READ ONLY. The agent serves a fixed pair of source trees
     through a path-confined endpoint; there is no write, move or delete verb.
     Edits made in the console stay in the browser (see views/Code.tsx). */
  codeRoots: () =>
    j<{ root?: string; roots?: { name: string; exists: boolean }[]; max_bytes?: number }>(
      '/api/code/roots'),
  codeTree: (path: string, signal?: AbortSignal) =>
    j<{ path?: string; parent?: string; entries?: CodeEntry[]; error?: string }>(
      `/api/code/tree?path=${encodeURIComponent(path)}`,
      signal ? { signal } : undefined),
  codeFiles: (signal?: AbortSignal) =>
    j<{ files?: string[]; count?: number; truncated?: boolean }>(
      '/api/code/files', signal ? { signal } : undefined),
  /** Syntax-check a DRAFT buffer. Takes text, never a path — the server judges
      what is on screen and cannot be turned into a second file reader. */
  codeCheck: (text: string, language: string, signal?: AbortSignal) =>
    pj<CodeCheck>('/api/code/check', { text, language },
      signal ? { signal } : undefined),
  codeSearch: (q: string, limit = 200, signal?: AbortSignal) =>
    j<{ matches?: CodeMatch[]; count?: number; truncated?: boolean }>(
      `/api/code/search?q=${encodeURIComponent(q)}&limit=${limit}`,
      signal ? { signal } : undefined),
  codeFile: (path: string, signal?: AbortSignal) =>
    safe(j<CodeFile>(`/api/code/file?path=${encodeURIComponent(path)}`,
      signal ? { signal } : undefined)),
  adopt: (sessionId: string) =>
    pj<{ conversation_id?: string; error?: string }>('/api/conversations/adopt', {
      session_id: sessionId,
    }),
  feedbackGet: (nodeId: string) =>
    j<{ vote: number }>(`/api/feedback?node_id=${encodeURIComponent(nodeId)}`),
  /** `skill` is sent so votes can be rolled up per skill. Without it the
      server can only group by node id, and a rollup over pruned runs loses
      the attribution entirely. */
  feedbackPost: (sessionId: string, nodeId: string, vote: 1 | -1, skill?: string) =>
    pj<{ status: string }>('/api/feedback', { session_id: sessionId, node_id: nodeId, vote, skill }),
  /** Aggregated votes: the only honest way to tell whether a prompt edit
      helped. `min_votes` marks low-sample rows rather than presenting them
      as a rate. */
  feedbackRollup: (days = 30, minVotes = 1) =>
    j<FeedbackRollup>(`/api/feedback/rollup?days=${days}&min_votes=${minVotes}`),
  /** Runtime counters the UI cannot otherwise see: fetch-cache hit rate (the
      thing behind research latency), how many tool outcomes reached memory,
      and whether the MCP tool subprocess breaker is open. */
  mcpStats: () => j<McpStats>('/api/mcp/stats'),
  /** Ask the agent to actually STOP a live run (server-side; the run ends
      at its next node boundary and remaining nodes are saved as `skipped`).
      `found:false` simply means nothing was running for that id. */
  cancelRun: (ids: { conversation_id?: string; session_id?: string }) =>
    pj<{ status: string; found: boolean; cancelled: number }>('/api/chat/cancel', ids),
  safe,
  /** POST /api/chat as an async generator of SSE events.
      Pass an AbortSignal to cancel mid-run (the generator closes the
      reader and stops — callers must also stop their graph polling). */
  async *chat(query: string, conversationId?: string, signal?: AbortSignal,
              opts?: { research?: boolean; idempotencyKey?: string }): AsyncGenerator<ChatEvent> {
    const r = await fetch('/api/chat', {
      method: 'POST',
      headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({
        query,
        conversation_id: conversationId || undefined,
        // Research submits are deduplicated server-side: a double-click (or a
        // retry after a slow response) joins the run already in flight
        // instead of paying for a second identical one.
        research: opts?.research || undefined,
        idempotency_key: opts?.idempotencyKey,
      }),
      signal,
    });
    if (!r.ok) throw new Error(await reason(r));
    if (!r.body) throw new Error('empty response body');
    let sawTerminal = false;
    for await (const f of sseFrames(r, signal)) {
      if (f.type === 'done' || f.type === 'error') sawTerminal = true;
      yield f as ChatEvent;
    }
    if (!sawTerminal && !signal?.aborted) {
      throw new Error('connection closed before the run finished — the answer may be incomplete');
    }
  },
  /** POST /api/chat/simple/stream as an async generator of SSE events
      (started/status/delta/done/error). Same abort contract as chat(). */
  async *chatSimpleStream(query: string, conversationId?: string, signal?: AbortSignal): AsyncGenerator<SimpleStreamEvent> {
    const r = await fetch('/api/chat/simple/stream', {
      method: 'POST',
      headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ query, conversation_id: conversationId || undefined }),
      signal,
    });
    if (!r.ok) throw new Error(await reason(r));
    if (!r.body) throw new Error('empty response body');
    let sawTerminal = false;
    for await (const f of sseFrames(r, signal)) {
      if (f.type === 'done' || f.type === 'error') sawTerminal = true;
      yield f as SimpleStreamEvent;
    }
    if (!sawTerminal && !signal?.aborted) {
      throw new Error('connection closed before the answer finished');
    }
  },
};

export function briefFor(topic: string, depth: string): string {
  const shape =
    depth === 'quick'
      ? 'Keep it tight: a short summary with the key points and main sources. One pass only.'
      : depth === 'deep'
        ? 'Be thorough: Executive Summary, Key Findings (with evidence), Competing Views, Risks & Unknowns, and a Sources section. Retrieve broadly before concluding.'
        : 'A structured report: Executive Summary, Key Findings, and Sources. Retrieve before concluding.';
  return `You are a research agent. Research this topic and deliver a final report as Markdown.\n\nTopic: ${topic}\n\n${shape}\n\nUse web search, memory and any files available. The final message must be the complete report.`;
}

export function displayTopic(s: SessionSummary): string {
  if (s.topic) return s.topic.trim();
  const m = String(s.query || '').match(/Topic:\s*([\s\S]{1,140})/i);
  return (m ? m[1].split('\n')[0] : s.query || '(untitled)').trim();
}

export function ago(updatedAgo?: number): string {
  const s = Math.max(0, updatedAgo || 0);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export function fmtFire(ts?: number): string {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const diff = ts * 1000 - Date.now();
  const when = d.toLocaleString();
  if (diff <= 0) return `${when} (due)`;
  const m = Math.round(diff / 60000);
  const rel = m < 60 ? `in ${m}m` : m < 1440 ? `in ${Math.round(m / 60)}h` : `in ${Math.round(m / 1440)}d`;
  return `${when} (${rel})`;
}
