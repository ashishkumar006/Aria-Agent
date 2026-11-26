/* Typed client over the agent's existing REST + SSE surface. */

import { cachedGet, invalidate, DEFAULT_TTL_MS } from './lib/getCache';

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
    /** Per-node token spend/produce, shown in the Inspector stats tab.
     *  For a tool-using node this is the loop's accumulated total across all
     *  its turns, not just the final reply's usage. */
    input_tokens?: number;
    output_tokens?: number;
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
     /** 'authoring' = this run rendered a document; 'research' = it did not.
         Both kinds live in the same session store, so without this the
         Authoring run appeared in the Research sidebar and neither view could
         tell its own history from the other's.
         Named `run_kind`, not `kind`: in the Authoring sidebar `kind` already
         means the FILE FORMAT (pdf/pptx/docx/xlsx). */
     run_kind?: 'authoring' | 'research' | 'chat' | 'parts' | 'app';
     /* Did this run actually write a file? Named `has_files` because the
        authoring sidebar already uses `files` for the file LIST. */
     has_files?: boolean;
  nodes?: number;
  skills?: string[];
  /** Seconds since the run last changed, computed server-side. Preferred over
      deriving it from `updated`, because `updated: 0` is falsy and used to
      collapse into a confident "0s ago" for runs over 100 hours old. */
  updated_ago?: number;
  /** False = there is no graph for this run. Distinct from a run that is
      queued: "nothing to show" is not "waiting to start". */
  has_graph?: boolean;
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

/** What a produced document actually contains, read back from its bytes.
 *  `warnings` names anything the user would otherwise only find out after
 *  downloading — an empty workbook, a deck of blank slides, a 57-word PDF. */
export interface DocPreview {
  kind: 'pdf' | 'pptx' | 'docx' | 'xlsx' | 'unknown';
  title?: string;
  subtitle?: string;
  size_bytes?: number;
  warnings?: string[];
  slides?: { n: number; title: string; bullets: string[]; notes: string; empty: boolean }[];
  pages?: { n: number; text: string; chars: number }[];
  sheets?: { name: string; rows: string[][]; empty: boolean }[];
  paragraphs?: string[];
  tables?: string[][][];
}

export interface RunRow extends SessionSummary {}

/** A gated computer-use action waiting for a human decision. */
export interface ComputerApproval {
  id: string;
  action: string;
  params?: Record<string, unknown>;
  created?: number;
  status?: string;
  reason?: string;
}

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

export interface FeedbackLearningEvent {
  feedback_id: string;
  status: 'applied' | 'skipped' | 'duplicate' | 'error' | 'failed';
  at: number;
  kind?: string;
  statement?: string;
  note_excerpt?: string;
  confidence?: number;
  reason?: string;
  memory_id?: string;
  attempts?: number;
  vote?: number;
  session_id?: string;
  node_id?: string;
  msg_id?: string;
  skill?: string;
}

export interface FeedbackLearningStatus {
  enabled: boolean;
  persona_autosave: boolean;
  pending: number;
  last_run: number | null;
  counts: Record<string, number>;
  processed: FeedbackLearningEvent[];
}

export interface FeedbackSyncResult {
  dry_run: boolean;
  pending_before: number;
  pending: number;
  selected: number;
  proposed: {
    action?: string;
    text?: string;
    kind?: string;
    confidence?: number;
    reason?: string;
    feedback_id?: string;
    context_source?: string;
    context_matched?: boolean;
  }[];
  applied: { feedback_id?: string; kind?: string; text?: string; memory_id?: string }[];
  skipped: { action?: string; text?: string; reason?: string; feedback_id?: string }[];
  errors: { feedback_id?: string; error?: string }[];
  last_run?: number | null;
  persona?: {
    ok?: boolean; error?: string; version?: number; markdown?: string;
    documents?: { ok?: boolean; error?: string; current?: string; evolution?: string };
  };
}

export interface VoiceOption {
  id: string;
  label: string;
}

export interface VoiceStatus {
  tts: {
    available: boolean;
    loaded?: boolean;
    dependencies?: boolean;
    model?: string;
    error?: string;
  };
  stt: {
    available: boolean;
    loaded?: boolean;
    model?: string;
    error?: string;
  };
  voices: VoiceOption[];
}

export interface PersonaMeta {
  version?: number;
  updated_iso?: string;
  reason?: string;
  turns_since_update?: number;
  total_turns?: number;
  based_on?: Record<string, number>;
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
  /* Self-wake. A wake row carries a reason and no query, so every field here
   * exists to keep the two readable apart in the list — without `kind`, a watch
   * the agent armed for itself is indistinguishable from a reminder you asked
   * for, in the one place you come to see what is going to happen. */
  kind?: string;
  reason?: string;
  every?: string;
  step_s?: number;
  wakes_left?: number;
  wakes_fired?: number;
  armed_by?: string;
  parent?: string;
  watch_until?: number;
  watch?: { surface?: string; match?: Record<string, string> };
  /* The words the user actually receives. Without it the Schedules page could
   * only show `query`, so a row whose query was the imperative "remind me to
   * stretch" looked identical to a healthy reminder while it re-scheduled
   * itself every two hours. */
  notify_policy?: {
    channel?: string;
    message?: string;
    lead_message?: string;
    lead_fire?: number;
    wait_for_ack?: boolean;
  };
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
  /** How well-supported the record is. Only observations write a real
      value; everything else was written before the field had a writer and
      defaults to 1.0. */
  confidence?: number;
}

export interface MemStats {
  /** Live records per kind, for the kinds the Memory page browses. */
  kinds: Record<string, number>;
  total: number;
  /** Raw per-drawer shapes, kept for a future drawer view. */
  drawers?: Record<string, { live?: number; live_by_kind?: Record<string, number> }>;
  excluded_drawers?: string[];
}

/** A logical document folder. parent_id="" is a top-level folder. */
export interface DocFolder {
  id: string;
  name: string;
  parent_id: string;
  created_at: string;
}

export interface DocItem {
  id: string;
  filename: string;
  doc_type: string;
  size_bytes: number;
  /** Folder id, or "" for unfiled documents in the root bucket. */
  folder_id?: string;
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
  /** Where the file came from: `upload`, `authoring`, `deck`, `research`,
      `web`, `chat`… Set at registration with the bytes, so a document in the
      library says which section of Aria made it. */
  origin?: string;
  /** Its source pointer — the URL for a fetched page, the artifact handle for
      a generated file. */
  origin_ref?: string;
  /** The run that produced it, when there was one. */
  session_id?: string;
  /** False only when the user switched it off deliberately; a document that
      has never been touched is enabled as soon as it finishes indexing. */
  auto_enable?: boolean;
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

/** The active Code workspace: the base path the explorer browses. Defaults
 *  to the project's two service trees; "Open Folder" points it elsewhere. */
export interface CodeWorkspace {
  root: string;
  name: string;
  roots?: string[];
  is_default?: boolean;
}

/** One folder a studio session holds. `scoped` on the payload below is whether
 *  the request belongs to a session at all — NOT whether it holds folders, so a
 *  reset session reads as yours-and-empty rather than as the shared default. */
export interface StudioRoot { path: string; name: string }

/** One studio run as the registry lists it. `status` is the field that makes
 *  a list of agents readable: `running` and `waiting` look identical in a
 *  transcript and mean opposite things to someone deciding whether to stop. */
export interface StudioRun {
  id: string;
  sid: string;
  question: string;
  model?: string;
  provider?: string;
  mode?: string;
  status: 'running' | 'waiting' | 'stopping' | 'finished' | 'stopped' | 'error';
  started: number;
  ended?: number;
  frames?: number;
  input_tokens?: number;
  output_tokens?: number;
  tool_calls?: number;
  answer?: string;
  stopped?: string;
  error?: string;
  files?: string[];
  live?: boolean;
}

export interface StudioRunsResult {
  runs?: StudioRun[];
  live?: number;
  waiting?: string[];
  error?: string;
}

/** A replayed frame. `t` is the SSE kind the panel already knows how to
 *  render, so replay and live stream share one code path — which is the only
 *  reason they cannot disagree about what a run looked like. */
export interface StudioRunFrame {
  t: string;
  at?: number;
  [key: string]: unknown;
}

export interface StudioRunFrames {
  frames?: StudioRunFrame[];
  run?: string;
  live?: boolean;
  status?: string;
  unreadable?: number;
  error?: string;
}

/** Slice 6 — what `@<fragment>` can complete to. Paths come back in the
 *  explorer's vocabulary (`<root>/dir/file`), which is the form the server
 *  maps onto a tool path, so a completion and a hand-typed mention mean the
 *  same file. */
export interface StudioMention {
  path: string;
  name: string;
  dir: boolean;
}

export interface StudioMentionsResult {
  matches?: StudioMention[];
  count?: number;
  truncated?: boolean;
  /** True when the walk ran out of budget: the list is early results, not
   *  every match, and the popup has to say so. */
  stopped?: boolean;
  note?: string;
  error?: string;
}

export interface StudioCommand {
  name: string;
  args: string;
  kind: string;
  desc: string;
}

/** One run's money, read from the gateway ledger. `unknown` is the honest
 *  third state — the ledger was unreachable — and is never rendered as $0. */
export interface StudioRunCost {
  run?: string;
  unknown?: boolean;
  usd?: number | null;
  in_tok?: number;
  out_tok?: number;
  calls?: number;
  /** Tokens the provider answered from its cache (or minted for it), per run. */
  cache_reads?: number;
  cache_creates?: number;
  note?: string;
  error?: string;
}

export interface StudioCommitMsgResult {  ok?: boolean;
  subject?: string;
  body?: string;
  text?: string;
  /** Which diff the message was written from — the UI has to label it. */
  staged?: boolean;
  repo?: string;
  files?: string[];
  files_with_diffs?: string[];
  omitted?: string[];
  truncated?: boolean;
  chars_sent?: number;
  note?: string;
  error?: string;
}

/** Slice 8 — one file operation the server planned or performed. */
export interface StudioFileOp {
  op: 'rename' | 'move' | 'delete' | 'mkdir';
  from?: string;
  to?: string;
}

export interface StudioFileOpResult {
  op: string;
  ok?: boolean;
  from?: string;
  to?: string;
  path?: string;
  reason?: string;
}

export interface StudioFilesResult {
  ok?: boolean;
  applied?: number;
  refused_count?: number;
  results?: StudioFileOpResult[];
  /** The checkpoint the batch was captured into — the undo for a delete. */
  checkpoint?: string;
  note?: string;
  error?: string;
}

export interface StudioSearchMatch {
  path: string;
  line: number;
  col: number;
  text: string;
}

export interface StudioSearchResult {
  matches?: StudioSearchMatch[];
  count?: number;
  truncated?: boolean;
  scanned?: number;
  binary_skipped?: number;
  note?: string;
  error?: string;
}

export interface StudioReplacePreview {
  ok?: boolean;
  dry_run?: boolean;
  token?: string;
  files?: { path: string; count: number; bytes_before: number; bytes_after: number }[];
  count?: number;
  refused?: string[];
  note?: string;
  error?: string;
  expires_in?: number;
}

export interface StudioReplaceResult {
  ok?: boolean;
  applied?: number;
  written?: { path: string; count: number }[];
  stale?: string[];
  checkpoint?: string;
  note?: string;
  error?: string;
}

export interface StudioGitPath {
  path: string;
  index: string;
  worktree: string;
  staged: boolean;
  untracked: boolean;
  unmerged: boolean;
  deleted: boolean;
}

export interface StudioGitResult {
  ok?: boolean;
  action?: string;
  repo?: string;
  summary?: string;
  moves_head?: boolean;
  exit_code?: number | null;
  output?: string;
  stderr?: string;
  truncated?: boolean;
  paths?: StudioGitPath[];
  branches?: string[];
  log?: string[];
  error?: string;
  /** Present on the 428 answer: the diff to read before confirming. */
  confirm_required?: boolean;
  preview?: {
    diff?: { ok?: boolean; output?: string; error?: string };
    status?: { ok?: boolean; output?: string };
  };
}

export interface StudioJournalEntry {
  at: number;
  kind: string;
  sid?: string;
  [key: string]: unknown;
}

/** Slice 10 — the browser pane. `casting` distinguishes a live view from a
 *  filmstrip of screenshots, and the pane says which it is showing rather
 *  than letting an empty box be read as a blank page. */
export interface StudioBrowserStatus {
  /** The page viewport, so the viewer can map screencast pixels to page pixels. */
  viewport?: { width?: number; height?: number };
  running?: boolean;
  disabled?: boolean;
  sid?: string;
  headed?: boolean | null;
  tabs?: number;
  active?: number;
  url?: string;
  actions?: number;
  epoch?: number;
  console_errors?: number;
  requests?: number;
  casting?: boolean;
  cast_fps?: number;
  cast_frames?: number;
  cast_error?: string;
  idle_in_s?: number;
  alive_s?: number;
  need_restart?: boolean;
  fallback_note?: string;
  note?: string;
  error?: string;
}

export interface StudioBrowserEvent {
  at: number;
  type?: string;
  level?: string;
  text?: string;
  method?: string;
  url?: string;
  status?: number | null;
  resource?: string;
}

export interface StudioBrowserEvents {
  events?: StudioBrowserEvent[];
  count?: number;
  dropped?: number;
  showing?: number;
  kind?: string;
  url?: string;
  disabled?: boolean;
  error?: string;
}

export interface StudioBrowserShot {
  name: string;
  url: string;
  at: number;
  bytes: number;
}

export interface StudioBrowserShots {
  shots?: StudioBrowserShot[];
  count?: number;
  more?: number;
  dir?: string;
  disabled?: boolean;
  error?: string;
}

export interface StudioBrowserNav {
  ok?: boolean;
  url?: string;
  status?: number | null;
  title?: string;
  tabs?: number;
  error?: string;
}

export interface StudioBrowserFrame {
  type: string;
  image?: string;
  url?: string;
  at?: number;
  fps?: number;
  frames?: number;
  error?: string;
  note?: string;
  reason?: string;
}

/** One studio session as the left column lists it. `label` is the first task
 *  typed into it, which is what makes a list of sessions readable; `roots` and
 *  `paths` say where it works without opening it. */
export interface StudioSession {  sid: string;
  label: string;
  roots: number;
  paths: string[];
  archived: boolean;
  created: number;
  updated: number;
  current: boolean;
  /** Live runs right now (in-memory count, cheap). Drives the spinner. */
  live?: number;
  /** Status of the newest run in the index (`running`, `finished`, `stopped`,
   *  `error`, …), when one exists. Drives the "finished" dot. */
  last_status?: string;
  /** Unreviewed changes this session is holding — the same definition the
   *  Changes drawer renders. Drives the finished-but-needs-you dot. */
  pending_review?: number;
}

export interface StudioSessionsResult {
  sessions?: StudioSession[];
  current?: string;
  error?: string;
}

export interface StudioWorkspace {
  sid: string;
  scoped: boolean;
  base: string;
  roots: StudioRoot[];
  declared: StudioRoot[];
  count: number;
  max: number;
  is_default?: boolean;
  workspace?: string;
  warning?: string;
  created?: string;
  error?: string;
}

/** The folder picker's read: folder names only, drives at the top level.
 *  `selectable` is `validate_root` surfaced as data — clicking a blocked row
 *  navigates into it (you must be able to reach your project), the Select
 *  button greys it with the refusal text. */
export interface StudioFsDir {
  name: string;
  path: string;
  selectable: boolean;
  reason: string;
}
export interface StudioFsListResult {
  path: string;
  parent: string;
  dirs?: StudioFsDir[];
  truncated?: boolean;
  /** Whether the CURRENT folder may be picked — the same refusal text the add
   *  route would give, greyed into the button instead. */
  selectable?: boolean;
  reason?: string;
  /** The user's home directory, for the quick-access column. */
  home?: string;
  /** Desktop / Documents / Downloads / … that exist on this machine. */
  quick?: StudioFsDir[];
  error?: string;
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
  /** Exact mtime: what a save echoes back so the server can refuse a stale
   *  write instead of clobbering newer work. */
  mtime?: number;
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

/** One checker the project actually has, and what it said. */
export interface CodeCheckResult {
  tool: string;
  ok: boolean;
  exit_code: number | null;
  stdout?: string;
  stderr?: string;
  error?: string;
  available?: boolean;
}

/** `/api/code/checks` — the project's own lint/typecheck/tests, run for real. */
/** Result of one `/api/code/intel` op. Only the fields that op returns are
 *  populated, so callers must not assume all of them are present. */
export interface CodeIntelResult {
  ok?: boolean;
  error?: string;
  // symbols
  file?: string;
  language?: string;
  symbols?: Array<{
    name: string; kind: string; line: number; end_line: number;
    col: number; doc: string; parent: string;
  }>;
  // references / blast
  symbol?: string;
  definitions?: Array<{ file: string; line: number; col: number; text: string; kind: string }>;
  references?: Array<{ file: string; line: number; col: number; text: string; kind: string }>;
  definition_count?: number;
  reference_count?: number;
  files_touched?: number;
  blast?: Array<{ file: string; count: number; lines: number[] }>;
  risk?: 'low' | 'medium' | 'high';
  exact?: boolean;
  // preview / apply
  diff?: string;
  detail?: string;
  applied?: boolean;
  written?: boolean;
  path?: string;
  // definition / hover (go-to-definition and tooltips)
  found?: boolean;
  line?: number;
  kind?: string;
  parent?: string;
  local?: boolean;
  signature?: string;
  doc?: string;
  // rename
  sites?: Array<{ file: string; line: number; col: number; text: string;
                  kind: string }>;
  site_count?: number;
  new_name?: string;
  changed?: string[];
  files_changed?: number;
}

export interface CodeChecks {
  ok: boolean;
  checks_run: number;
  results: CodeCheckResult[];
  cwd?: string;
  error?: string;
  note?: string;
}

/** A frame from `/api/code/ask`. Unknown types are ignored, not fatal. */
export interface CodeAskFrame {
  type: string;
  text?: string;
  answer?: string;
  path?: string;
  provider?: string;
  model?: string;
  /** The model's reasoning. A separate frame type precisely so it is never
   *  concatenated into the answer. */
  thinking?: string;
  input_tokens?: number;
  output_tokens?: number;
  context_files?: string[];
}

export type CodePlanStatus = 'pending' | 'doing' | 'done' | 'blocked';

export interface CodePlanStep {
  id?: string;
  title: string;
  status: CodePlanStatus;
}

export interface CodeVerificationCheck {
  path?: string;
  kind?: string;
  ok?: boolean;
  skipped?: boolean;
  error?: string;
  note?: string;
}

export interface CodeVerification {
  ok: boolean;
  checked: number;
  changed?: string[];
  checks?: CodeVerificationCheck[];
  error?: string;
  duration_ms?: number;
  round?: number;
}

/** A frame from `/api/code/agent` — the tool-using agentic loop.
 *
 *  Superset of CodeAskFrame: it adds the live tool trace (`tool_call` /
 *  `tool_result`), which is the whole difference between a Q&A box and an
 *  agent that reads the repo. `delta` frames now arrive token by token from
 *  the model as it writes, so the answer appears while it is being generated. */
export interface CodeAgentFrame extends CodeAskFrame {
  /** Present on tool_call / tool_result: the tool's name. */
  name?: string;
  /** tool_call: the identifying argument. tool_result: a bounded result. */
  detail?: string;
  /** tool_result only: did the tool succeed? */
  ok?: boolean;
  /** Pairs a tool_result with the tool_call it belongs to, so the panel
   *  updates one activity row instead of appending a duplicate. */
  id?: string;
  /** done only: the reasoning was the answer repeated, so any live `thinking`
   *  frames must be withdrawn. */
  thinking_dropped?: boolean;
  /** done only: why the loop stopped early (hop cap, context budget, a stale
   *  repeat). Empty on a normal completion. */
  stopped?: string;
  tool_calls?: number;
  /** plan: the current task checklist. */
  steps?: CodePlanStep[];
  /** error / verify: the human-readable failure reason. */
  error?: string;
  /** verify only: the harness's post-edit syntax/type check. `retry` means
   *  the server is starting a fix round (the panel must clear the live answer). */
  checked?: number;
  changed?: string[];
  checks?: CodeVerificationCheck[];
  duration_ms?: number;
  round?: number;
  retry?: boolean;
  /** done only: final verification snapshot, if any. */
  verification?: CodeVerification | null;
  plan?: CodePlanStep[] | null;
  mode?: 'ask' | 'auto-edit' | 'auto-all' | 'read-only';
  /** permission only: the agent wants to write and is parked until this id is
   *  decided through `codePermission`. `diff` is the exact patch.
   *  write (auto) only: the same payload, but the write is already dispatched
   *  - no decision is expected; the user reviews afterwards through
   *  Keep / Revert (`codeKeep` / `codeRevert`). */
  tool?: string;
  summary?: string;
  diff?: string;
  args?: Record<string, unknown>;
  /** permission (command) only: the run carried a session id, so "always
   *  allow this command for this session" is offered alongside Accept. */
  rememberable?: boolean;
  /** exec only: this command ran under a standing approval the operator gave
   *  earlier in the session — reported, never silent. */
  standing?: boolean;
  /** permission / write: the CLIENT-visible path of the file the write touches
   *  ("<root>/dir/file"), safe to hand straight to `codeFile`/`open`. */
  path?: string;
  /** write only: the write was auto-approved and is being applied now. */
  auto?: boolean;
}

/** One unreviewed agent write, as listed by `codeChanges`. */
export interface CodeAgentChange {
  path: string;
  tool?: string;
  summary?: string;
  diff?: string;
  when?: number;
  /** The task this file was last written under — the change-set key. Files
   *  with no turn predate task tracking and group as "older work". */
  turn?: string;
  checkpoint?: string;
  /** true when the agent created the file (before-content: nothing). */
  created?: boolean;
  /** Present from slice 4 on both surfaces: the same hunks the studio renders,
   *  each with a content fingerprint. The legacy rail ignores them. */
  hunks?: StudioHunk[];
  added?: number;
  removed?: number;
}

/** One addressable region of an unreviewed change.
 *
 *  `id` is a fingerprint of the hunk's own lines plus its context — NOT of its
 *  line numbers, which move the moment another hunk is decided. Clicking a
 *  hunk whose text has since been edited away is therefore refused
 *  (`hunk-moved`) instead of applied to the wrong region. */
export interface StudioHunk {
  id: string;
  index: number;
  header: string;
  old_start: number;
  old_lines: number;
  new_start: number;
  new_lines: number;
  added: number;
  removed: number;
  /** context + removed / context + added, as the server sees them. */
  old: string[];
  new: string[];
}

export interface StudioChange extends CodeAgentChange {
  hunks: StudioHunk[];
  /** Only with `path=`: the state pending hunks are measured against. After a
   *  keep this is NOT the file as the agent found it — it is that plus what
   *  the user accepted, which is why the diff view needs it from the server. */
  baseline?: string;
  baseline_missing?: boolean;
}

export interface StudioChangesResult {
  changes?: StudioChange[];
  sid?: string;
  error?: string;
}

export interface StudioApplyResult {
  ok?: boolean;
  action?: string;
  path?: string;
  hunk?: string;
  index?: number;
  hunks_remaining?: number;
  deleted?: boolean;
  error?: string;
}

export interface StudioCheckpoint {
  id: string;
  name: string;
  turn: string;
  when: number;
  /** "" for a task's own "before" state, "redo" for a state a redo can replay,
   *  "undo" for the state a redo came from (which is what undo takes next). */
  capture?: string;
  files: number;
  bytes: number;
}

/** `/api/studio/checkpoints` also carries the walk cursor: ids of checkpoints
 *  the undo/redo history has already passed. Without it every `/undo` would
 *  pick the same entry, because checkpoints are kept, not popped. */
export interface StudioCheckpointList {
  checkpoints?: StudioCheckpoint[];
  acted?: string[];
  error?: string;
}

export interface StudioCheckpointRestoreResult {
  ok?: boolean;
  checkpoint?: string;
  name?: string;
  /** Paths put back to their checkpointed bytes. */
  restored?: string[];
  /** Paths a rename had moved, taken away again so undo is not a duplicate. */
  removed?: string[];
  failed?: { path: string; reason: string }[];
  skipped?: { path: string; reason: string }[];
  /** The walk cursor after this restore, so the caller can offer the next step
   *  without waiting for a reload. */
  acted?: string[];
  undo?: { id: string; turn: string; name: string };
  error?: string;
}

/** One entry of the gateway's Kilo catalogue, normalized upstream. */
export interface KiloModel {
  id: string;
  name: string;
  vendor: string;
  owned_by: string;
  context_length: number;
  max_completion_tokens: number;
  price_in: number;
  price_out: number;
  is_free: boolean;
  supports_tools: boolean;
  supports_vision: boolean;
  supports_reasoning: boolean;
  description: string;
}

/** Response of `/api/models` — the picker roster, or an explanation. */
export interface ModelRoster {
  models: KiloModel[];
  count: number;
  free_count: number;
  stale: boolean;
  /** The pool's current model; what the picker pre-selects. */
  default?: string;
  /** Whether a KILO_API_KEY is set (the roster is readable without one). */
  configured?: boolean;
  error?: string | null;
}

export interface SkillRow {
  skill: string;
  calls?: number;
  in_tokens?: number;
  out_tokens?: number;
  dollars?: number;
}

/** One skill's node reliability: how many nodes it ran, how many failed,
 *  and why. `fail_pct` is against THAT SKILL's node count, which is the number
 *  a reader can act on - "author failed 38% of its nodes" is a defect;
 *  "4 nodes failed overall" is not. */
export interface NodeHealthRow {
  skill: string;
  /** Every node seen for this skill, including ones never dispatched. */
  nodes: number;
  /** Nodes actually attempted (complete + failed). The fail_pct denominator. */
  attempted?: number;
  /** pending + interrupted: recorded, but never ran, so never failed. */
  never_run?: number;
  failed: number;
  skipped: number;
  fail_pct: number;
  reasons: { reason: string; count: number; pct_of_failures: number }[];
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
  | { type: 'done'; answer?: string; session_id?: string; surface?: A2uiSurface }
  | { type: 'error'; text?: string }
  | { type: string; [k: string]: unknown };

/** A file a run produced, as reported on a stream's `done` frame. The server
    resolves every handle against the artifact store before reporting it, so a
    listed file always downloads. */
export interface StreamFile {
  artifact: string;
  filename: string;
  format: string;
  skill?: string;
  bytes?: number;
}

export type SimpleStreamEvent =
  | { type: 'started'; conversation_id?: string }
  | { type: 'status'; text?: string }
  | { type: 'delta'; text?: string }
  /** `files` is what makes a rendered document (and any chart inside it)
      visible in Chat without the model having to paste its artifact handle
      into the prose. Verified against the artifact store server-side, so an
      entry here always downloads. */
  /* `elapsed_ms` is measured server-side from the moment the turn starts, not
     from when the client rendered: time spent opening the SSE connection is
     the user's wait too, and hiding it would flatter the answer. */
  /* `kind: "report"` + `title` mark an answer that IS a research report shown
     in full (delegated from chat). The console renders those as a preview
     card that opens in a dialog, instead of inlining several thousand words. */
   | { type: 'done'; answer?: string; conversation_id?: string; files?: StreamFile[]; elapsed_ms?: number; model?: string; provider?: string; kind?: string; title?: string; stopped?: boolean; surface?: A2uiSurface }
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

/** The per-launch token as a value, for the terminal's WebSocket handshake.
 *  A browser cannot set a custom header on a WebSocket, so the token has to
 *  travel in the URL — the same unforgeable-per-launch secret, just carried
 *  where the transport allows. */
export function launchToken(): string {
  // Re-read the shell's meta tag rather than pinning the module-load value:
  // a reconnect after the agent restarted must use the NEW per-launch token
  // if the shell refreshed it (H12).
  try {
    return document.querySelector('meta[name="aria-token"]')
      ?.getAttribute('content') || LAUNCH_TOKEN;
  } catch { return LAUNCH_TOKEN; }
}

/* ── AG-UI ────────────────────────────────────────────────────────────────
   The subset this client consumes. Deliberately not the full 31-event
   vocabulary: these are the frames the Apps refresh emits, and a type that
   enumerated everything would claim coverage the client does not have. */
export interface AguiEvent {
  type: string;
  timestamp?: number;
  threadId?: string;
  runId?: string;
  stepName?: string;
  messageId?: string;
  activityType?: string;
  content?: { text?: string };
  result?: { app_id?: string; count?: number; error?: string | null };
  message?: string;
}

/** Auth headers for a direct `fetch`. Exported because some downloads are
 *  blob fetches rather than JSON calls (a file route needs the token, and an
 *  <a href> navigation cannot send it). */
export function headers(extra?: Record<string, string>): Record<string, string> {
  const h: Record<string, string> = { ...(extra || {}) };
  const t = getToken();
  if (t) h.Authorization = `Bearer ${t}`;
  // The per-launch token travels as its own header, not as the bearer. It is
  // required on every /api/* call when the agent has one, and a cross-origin
  // page cannot set it.
  const launch = launchToken();
  if (launch) h['X-Aria-Token'] = launch;
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
  const msg = pick ? `${r.status} ${pick}` : `HTTP ${r.status} ${r.url}`;
  /* The agent regenerates its token on every launch and the console reads it
     once from a <meta> tag, so a tab left open across a restart holds a dead
     credential: every route 403s and there is nothing the user can do about
     it. Flag it so the shell can say "reload" instead of showing an error
     that reads like a bug. */
  if (r.status === 403 && /X-Aria-Token/i.test(msg)) {
    notifyStaleToken();
  }
  return msg;
}

/** Fired once when the console's per-launch token is rejected. A reload
 *  re-fetches the document, and the agent re-injects the current token into
 *  it — which is the only way to recover without persisting the secret. */
export const STALE_TOKEN_EVENT = 'aria:stale-token';
let staleNotified = false;

function notifyStaleToken(): void {
  if (staleNotified) return;
  staleNotified = true;
  try {
    window.dispatchEvent(new CustomEvent(STALE_TOKEN_EVENT));
  } catch { /* no DOM: nothing to show it on */ }
}

/** How long a list read may be reused for a section switch.
 *
 *  Lists were all `no-store`, so every hop back to Runs/Documents/Skills paid
 *  the full round-trip again even though the user had just looked at the same
 *  rows (measured: 224 ms for Runs, 673 ms for Documents). 10 s makes a quick
 *  hop instant while leaving every poll cadence (15 s) genuinely fresh; a
 *  write still invalidates the whole cache immediately. */
export const LIST_TTL_MS = 10_000;

/** Options a caller may pass to `j`. `ttlMs` only applies to GETs. */
type JInit = RequestInit & { ttlMs?: number };

async function j<T>(url: string, init?: JInit): Promise<T> {
  const method = (init?.method || 'GET').toUpperCase();
  /* Read-through cache on GET only. This is the single funnel every view
     goes through, so caching here covers all of them without touching a call
     site. A section switch was refetching everything the user had just
     looked at; the keep-alive host stops the component remount and this stops
     the network round-trip. Writes bypass it entirely. */
  const load = async (): Promise<T> => {
    const r = await fetch(url, { ...init, headers: headers(init?.headers as Record<string, string>) });
    if (!r.ok) throw new Error(await reason(r));
    return (await r.json()) as T;
  };
  if (method !== 'GET' || !url.startsWith('/api/')) {
    /* A write makes every cached read of the affected data stale — whatever its
       verb. This used to live only in `pj` (POST), so the DELETE routes
       (scheduling, chat threads, memory, documents) went through here with no
       invalidation at all: `scheduleDelete` reported "deleted", then the
       view's re-read was served from the 30s read-through cache and the row
       stayed on screen until the TTL expired or the page was reloaded.
       Invalidate before the request so a component that re-reads on the
       success path never sees the pre-write value. */
    if (method !== 'GET') invalidate();
    return load();
  }
  /* `cache: 'no-store'` opts a GET out of the read-through cache. Liveness
     probes need it: a 30s-cached /api/health made the gateway-down banner's
     Retry replay the very failure the user was trying to clear. */
  if (init?.cache === 'no-store') return load();
  return cachedGet<T>(url, load, init?.ttlMs ?? DEFAULT_TTL_MS);
}

/** POST a JSON body through the same pipeline as `j` (auth header + server
    error text). Every POST in this client must go through here: hand-rolled
    `fetch` calls drifted — some dropped the Authorization header, some
    stringified differently — and each drift was a latent auth or
    contract bug. */
function pj<T>(url: string, body: unknown, init?: RequestInit): Promise<T> {
  /* `j` invalidates the read-through cache for every non-GET verb, so a POST
     through here needs no invalidation of its own. PATCH/DELETE methods are
     honored when the caller supplies one. */
  return j<T>(url, {
    ...init,
    method: init?.method || 'POST',
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

/** POST whose 4xx answer is CONTENT, not a transport failure.
 *
 *  `/api/studio/git` answers a HEAD-moving verb with 428 + the diff to read,
 *  and the replace route answers a spent preview token with 409 + the reason.
 *  Those are the next step of the flow, so a caller that only ever sees a
 *  thrown `428 …` string cannot render the confirmation — the panel would
 *  show an error where a decision belongs. Genuine failures still throw. */
async function pjBody<T>(url: string, body: unknown,
                         accept: number[] = [403, 409, 428]): Promise<T> {
  const r = await fetch(url, {
    method: 'POST',
    headers: headers({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  });
  invalidate();
  if (!r.ok && !accept.includes(r.status)) throw new Error(await reason(r));
  return (await r.json()) as T;
}

export interface ChatThread {
  conversation_id: string;
  title?: string;
  updated?: number;
  turns?: number;
  /** Which engine owns this thread. The sidebar lists only the current mode's. */
  mode?: 'light' | 'dag';
  /** True while the server still owns a run for this thread, even when no
      browser is watching its stream. */
  active?: boolean;
}

export interface TtsStreamEvent {
  type: 'chunk' | 'done' | 'error';
  index?: number;
  total?: number;
  text?: string;
  sample_rate?: number;
  duration_s?: number;
  audio?: string;
  chunks?: number;
  seconds?: number;
  error?: string;
}

export interface GatewayUserStatus {
  configured: boolean;
  token_hint?: string;
  valid?: boolean;
  kind?: 'user' | 'operator';
  user?: { id?: string; name?: string; keys?: number };
  error?: string;
}

export interface FlagRow {
  name: string;
  type: string;
  value: unknown;
  default: unknown;
  source: 'prefab' | 'override' | 'default';
  description?: string;
}

export interface A2uiProps { [k: string]: unknown }

/** Handler for A2UI Action component clicks. */
export type A2uiActionHandler = (action: string, args: Record<string, unknown>) => void;

export interface A2uiComponent { id: string; component: string; props: A2uiProps; type?: string }
export interface A2uiSurface {
  surfaceId: string;
  catalogId: string;
  rootComponent: string;
  components: A2uiComponent[];
}

export interface PartMetrics {
  faces?: number;
  vertices?: number;
  volume_mm3?: number;
  surface_area_mm2?: number;
  dimensions_mm?: number[];
  bbox_min?: number[];
  bbox_max?: number[];
  min_edge_mm?: number;
  median_edge_mm?: number;
  overhang_faces?: number;
  overhang_area_mm2?: number;
  thin_regions?: number;
  thinnest_wall_mm?: number;
}

export interface PartIssue {
  code: string;
  severity: 'error' | 'warn' | 'info' | string;
  message: string;
}

export interface PartVerdict {
  status?: 'ok' | 'warn' | 'unusable' | string;
  errors?: number;
  warnings?: number;
  printable?: boolean;
}

export interface PartView {
  preset?: string;
  mode?: 'shaded' | 'wireframe' | string;
  overlay?: 'none' | 'overhang' | 'thin_walls' | 'issues' | string;
  compare?: { from?: string; to?: string; mode?: string } | null;
}

export interface PartVersion {
  id: string;
  parent?: string | null;
  op: string;
  params?: Record<string, unknown>;
  created: number;
  filename?: string;
  format?: string;
  bytes?: number;
  metrics?: PartMetrics;
  objective?: number | null;
  verdict?: PartVerdict;
  issues?: PartIssue[];
  kept?: boolean;
  note?: string;
  origin?: string;
}

export interface PartSummary {
  id: string;
  name: string;
  description?: string;
  tags?: string[];
  created?: number;
  updated?: number;
  source?: { filename?: string; format?: string; bytes?: number; sha256?: string };
  current_version?: string;
  versions?: number;
  faces?: number;
  vertices?: number;
  objective?: number | null;
  verdict?: PartVerdict;
  op?: string;
  kept?: boolean;
  bytes?: number;
  format?: string;
  view?: PartView;
}

export interface PartDetail extends Omit<PartSummary, 'versions'> {
  versions: PartVersion[];
  current?: PartVersion | null;
  report?: Record<string, unknown>;
  issues?: PartIssue[];
  view?: PartView;
}

export interface PartGeometry {
  positions: number[];
  indices: number[];
  position_count: number;
  face_count: number;
}

export interface PartJob {
  id: string;
  part_id: string;
  version: string;
  base_version?: string;
  current_version?: string;
  pass: string;
  params?: Record<string, unknown>;
  max_steps: number;
  step: number;
  status: 'queued' | 'running' | 'cancelling' | 'done' | 'cancelled' | 'error' | string;
  log?: string[];
  versions?: string[];
  error?: string | null;
  note?: string;
  origin?: string;
  created?: number;
  updated?: number;
  finished?: number;
}

export interface BlenderBounds {
  x: number; y: number; width: number; height: number;
}

export interface BlenderStatus {
  available?: boolean;
  executable?: string | null;
  live?: { live?: boolean; part_id?: string; version?: string;
           pid?: number; port?: number; info?: Record<string, unknown> };
  worker?: { alive?: boolean; info?: Record<string, unknown> };
  root?: string;
}

export interface BlenderOperator {
  name: string;
  group?: string;
  op?: string;
  label?: string;
  description?: string;
  params?: { name: string; type?: string; label?: string;
             description?: string; default?: unknown; enum?: string[] }[];
}

export interface BlenderCatalog {
  ok?: boolean; total?: number; operators?: BlenderOperator[];
  groups?: Record<string, number>; truncated?: boolean; note?: string;
  error?: string;
}

export interface BlenderRender {
  ok?: boolean; error?: string; path?: string; filename?: string;
  bytes?: number; image_base64?: string; engine?: string;
}

export interface PartPassInfo {
  name: string;
  description: string;
  params?: Record<string, unknown>;
  modifies?: boolean;
}

export interface PartCapabilities {
  backends?: Record<string, { available?: boolean; version?: string | null; error?: string | null }>;
  natively_readable?: string[];
  limits?: { max_bytes?: number; max_faces?: number; max_vertices?: number };
  notes?: string[];
}

export interface TrackerApp {
  id: string;
  name: string;
  kind: string;
  description?: string;
  schedule: string;
  next_refresh: number | null;
  refreshed_at: number | null;
  count: number;
  added: number;
  removed: number;
  error: string | null;
}

export interface TrackerAppDetail extends TrackerApp {
  spec: Record<string, unknown> & {
    kind?: string;
    description?: string;
    refresh_prompt?: string;
    surface?: A2uiSurface;
    state?: Record<string, unknown>;
  };
  surface?: A2uiSurface | null;
  state?: Record<string, unknown> | null;
  last_event?: { action?: string; input?: Record<string, string> } | null;
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

/** `?sid=` / `&sid=` for the code routes a studio session owns.
 *
 *  Absent sid means "the shared workspace" — which is every caller that exists
 *  today, so the URL the Code page sends is byte-identical to before. It is
 *  passed explicitly rather than read from a module-level "current session",
 *  because the console keeps every section mounted: a global here would make a
 *  hidden Code page's fetches carry the studio's session, and the symptom of
 *  that bug is a file written into the wrong project, not an error. */
const sidQs = (sid?: string, hasQuery = false): string =>
  (sid ? `${hasQuery ? '&' : '?'}sid=${encodeURIComponent(sid)}` : '');

export const api = {
  health: (fresh = false) => j<{ agent: string; gateway_up: boolean }>(
    `/api/health${fresh ? '?fresh=1' : ''}`, { cache: 'no-store' }),
  /** Is the local Model Lab playground (port 7861) running? The page embeds it
      in an iframe and shows a helpful placeholder when it is down. */
  modelLabStatus: () => j<{ up: boolean; url: string }>(
    '/api/model-lab/status', { cache: 'no-store' }),
  /* ── computer-use (gated desktop control) ──────────────────────────────
     The gates live server-side: a sensitive action returns a PENDING
     approval and waits for a human. This view is where that human is -
     the agent server had the endpoints, but nothing in the console could
     approve anything, so a pending action was a dead end. */
  computerStatus: () => j<{
    approvals: ComputerApproval[];
    capabilities: { binary_present?: boolean; daemon_running?: boolean;
                    ax_ok?: boolean; screenshot_ok?: boolean;
                    elevated?: boolean; platform?: string;
                    apps?: { name: string; running: boolean }[]; note?: string };
    mode: string;
    /** False when the gates are off entirely (COMPUTER_USE_ENABLED). */
    enabled: boolean;
  }>('/api/computer/approvals', { cache: 'no-store' }),
  /** Approve or reject a pending action. `approve:false` is a rejection -
   *  the action never runs, which is the point of the gate. */
  computerDecide: (id: string, approve: boolean) =>
    pj<{ status: string; action?: string; approve?: boolean }>(
      `/api/computer/approvals/${encodeURIComponent(id)}`,
      { approve }),
  /** The redacted audit trail: every gated request and its outcome.
   *  `limit` keeps only the newest lines - the log is uncapped and the
   *  approval the user came to read is at the end. */
  computerAudit: (redact = true, limit = 400) =>
    j<{ lines: string[]; count: number; total?: number }>(
      `/api/audit?redact=${redact ? 'true' : 'false'}&limit=${limit}`, { ttlMs: LIST_TTL_MS }),
  /** Recorded trajectories (start/stop recording, replay). */
  computerRuns: () => j<{ runs: { id: string; turns?: number; path?: string }[] }>(
    '/api/computer/runs', { ttlMs: LIST_TTL_MS }),
  computerRun: (id: string) =>
    j<{ id: string; turns?: { turn: string; calls: unknown[] }[] }>(
      `/api/computer/runs/${encodeURIComponent(id)}`, { cache: 'no-store' }),
  computerReplay: (id: string) =>
    pj<{ status: string; result?: unknown }>(
      `/api/computer/replay/${encodeURIComponent(id)}`, {}),
  /** Dispatch a computer action (drive_app, open_app, click, type, etc.) */
  computerAction: (action: string, params: Record<string, unknown>) =>
    pj<{ status: string; output?: unknown; error?: string }>(
      '/api/computer/action',
      { action, params }),
  /* ── document authoring (PDF / PPTX / DOCX / XLSX) ──
     The gateway owns the generators; these two routes proxy them so the
     console never holds the gateway token. `docgen` returns the BLOB (it
     is a file download, not JSON), so it cannot use the `j` helper. */
  docgenSchema: () => j<{ spec: Record<string, unknown>; limits: Record<string, number> }>(
    '/api/docgen/formats'),
  docgen: async (format: string, spec: unknown): Promise<{ blob: Blob; filename: string }> => {
    const r = await fetch('/api/docgen', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...headers() },
      body: JSON.stringify({ format, spec }),
    });
    if (!r.ok) {
      let msg = `${r.status}`;
      try {
        const b = await r.json();
        msg = String(b?.error || msg);
      } catch { /* not JSON: keep the status */ }
      throw new Error(msg);
    }
    const cd = r.headers.get('content-disposition') || '';
    const m = /filename="?([^";]+)"?/i.exec(cd);
    return { blob: await r.blob(), filename: m ? m[1] : `document.${format}` };
  },
  /* `mode` scopes the list to one engine ("light" | "dag") so the
     Lightweight/DAG toggle does not show the same conversations in both. */
  chatThreads: (limit = 50, offset = 0, mode?: 'light' | 'dag') =>
    j<{ threads: ChatThread[]; total: number; has_more: boolean }>(
      `/api/chat/threads?limit=${limit}&offset=${offset}${mode ? `&mode=${mode}` : ''}`,
    ),
  chatThread: (cid: string) =>
    j<{ conversation_id: string; title: string; active?: boolean;
        messages: { role: string; content: string; kind?: string; title?: string;
                    pending?: boolean; started?: number; files?: StreamFile[] }[] }>(
      `/api/chat/threads/${encodeURIComponent(cid)}`,
    ),
  /* Persist a DAG turn into the sidebar store so a DAG conversation is listed
     under `dag` and can be reopened (the DAG engine keeps its own transcript;
     the sidebar store is otherwise lightweight-only). */
  chatThreadTurn: (cid: string, query: string, answer: string, mode: 'light' | 'dag') =>
    pj<{ status: string }>(`/api/chat/threads/${encodeURIComponent(cid)}/turn`,
      { query, answer, mode }),
  chatThreadDelete: (cid: string) =>
    j<{ status: string }>(`/api/chat/threads/${encodeURIComponent(cid)}`, { method: 'DELETE' }),
  chatSimple: (query: string, conversationId?: string, model?: string) =>
    pj<{ answer: string; conversation_id: string; error?: string; kind?: string; title?: string }>(`/api/chat/simple`, {
      query, conversation_id: conversationId || undefined,
      ...(model ? { model } : {}),
    }),
  sessions: (limit = 50, offset = 0) =>
    j<{ sessions: SessionSummary[]; total?: number; offset?: number; has_more?: boolean }>(
      `/api/sessions?limit=${limit}&offset=${offset}`, { ttlMs: LIST_TTL_MS }),
  runsSummary: (limit = 200, offset = 0) =>
    j<{ runs: RunRow[]; totals?: { runs?: number; nodes?: number; dollars?: number };
        offset?: number; has_more?: boolean }>(
      `/api/runs/summary?limit=${limit}&offset=${offset}`, { ttlMs: LIST_TTL_MS },
    ),
  /** The graph is always requested `light`: `light=false` makes the server
      serialise every NodeState (untrimmed, "some nodes are megabytes") into
      a `node_states` array this client never reads. Node detail is fetched
      on demand via `api.node()`, which *is* trimmed. */
  graph: (sid: string) =>
    j<GraphPayload>(
      `/api/sessions/${encodeURIComponent(sid)}/graph?light=true`,
      { cache: 'no-store' }),
  node: (sid: string, nid: string) =>
    j<NodeDetail>(`/api/sessions/${encodeURIComponent(sid)}/nodes/${encodeURIComponent(nid)}`),
  cost: (conversationId?: string) =>
    j<{ rows?: { calls?: number; dollars?: number }[] }>(
      `/api/cost${conversationId ? `?conversation_id=${encodeURIComponent(conversationId)}` : ''}`,
    ),
  bySkill: (conversationId?: string) =>
    j<{ rows?: SkillRow[]; turns?: TurnRow[]; totals?: { dollars?: number; calls?: number; skills?: number }; unknown_conversation?: boolean }>(
      `/api/cost/by_skill${conversationId ? `?conversation_id=${encodeURIComponent(conversationId)}` : ''}`,
    ),
  nodeHealth: (conversationId?: string) =>
    j<{
      sessions: number;
      /** Graph files that failed to parse; >0 means the table is partial. */
      unreadable?: number;
      rows: NodeHealthRow[];
      totals: { nodes: number; failed: number; fail_pct: number };
    }>(
      `/api/nodes/health?limit=200${conversationId ? `&conversation_id=${encodeURIComponent(conversationId)}` : ''}`,
    ),
  scheduleList: () => j<{ schedules?: ScheduleJob[] | Record<string, ScheduleJob> }>(
    '/api/schedule', { ttlMs: LIST_TTL_MS }),
  scheduleCreate: (query: string, when: string) =>
    pj<{ status: string; id?: string; message?: string }>('/api/schedule', { query, when }),
  scheduleCancel: (id: string) =>
    j<{ status: string }>(`/api/schedule/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  scheduleDelete: (id: string) =>
    j<{ status: string }>(`/api/schedule/${encodeURIComponent(id)}?hard=true`, { method: 'DELETE' }),
  /* Re-arm a cancelled or already-fired reminder instead of creating a
     second copy of it. Returns `{ok:false, hint}` when the time has passed,
     which the view shows rather than silently doing nothing. */
  scheduleToggle: (id: string, enabled: boolean) =>
    pj<{ ok: boolean; schedule_id?: string; enabled?: boolean; error?: string; hint?: string }>(
      `/api/schedule/${encodeURIComponent(id)}/enabled`, { enabled }),
  tools: () => j<{ tools: ToolSpec[] }>('/api/tools', { ttlMs: LIST_TTL_MS }),
  toolsGuard: () =>
    j<{ disabled: string[] | null; error?: string }>('/api/config/tools'),
  setTool: (tool: string, enabled: boolean) =>
    pj<{ disabled?: string[]; error?: string }>('/api/config/tools', { tool, enabled }),
  /** `live` opts out of the 30s read-through cache. The pane-hiding gate needs
   *  it: a flag you just switched off must not keep hiding panes for half a
   *  minute, which is the same liveness problem the gateway-down banner had. */
  flags: (live?: boolean) =>
    j<{ flags: FlagRow[]; prefab: { configured: boolean; live: boolean } }>(
      '/api/apps/flags', live ? { cache: 'no-store' } : undefined),
  setFlag: (name: string, value: unknown) =>
    pj<{ name?: string; value?: unknown; cleared?: boolean; flags?: FlagRow[]; error?: string }>(
      '/api/apps/flags', { name, value },
    ),
  trackerApps: () => j<{ apps: TrackerApp[] }>('/api/apps', { ttlMs: LIST_TTL_MS }),
  trackerApp: (id: string) =>
    j<TrackerAppDetail>(
      `/api/apps/${encodeURIComponent(id)}`, { cache: 'no-store' }),
  trackerCreate: (spec: Record<string, unknown>) =>
    pj<{ spec?: Record<string, unknown>; apps?: TrackerApp[]; error?: string }>('/api/apps', spec),
  appBuild: (description: string, name?: string) =>
    pj<{ ok?: boolean; app?: TrackerAppDetail; apps?: TrackerApp[]; error?: string }>(
      '/api/apps/build', { description, ...(name ? { name } : {}) }),
  appAction: (id: string, action: string, input: Record<string, string>) =>
    pj<{ ok?: boolean; app?: TrackerAppDetail; surface?: A2uiSurface; error?: string }>(
      `/api/apps/${encodeURIComponent(id)}/actions`, { action, input }),
  appPatch: (id: string, fields: { name?: string; schedule?: string; description?: string; refresh_prompt?: string }) =>
    pj<{ ok?: boolean; spec?: Record<string, unknown>; app?: TrackerAppDetail; apps?: TrackerApp[]; error?: string }>(
      `/api/apps/${encodeURIComponent(id)}`, fields, { method: 'PATCH' }),
  trackerRefresh: (id: string) =>
    j<{ count?: number; added?: number; removed?: number; error?: string | null; refreshed_at?: number }>(
      `/api/apps/${encodeURIComponent(id)}/refresh`,
      { method: 'POST' },
    ),
  trackerDelete: (id: string) =>
    j<{ deleted?: string; apps?: TrackerApp[] }>(`/api/apps/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }),
  /** One tracker app as a validated A2UI surface.
      A peer of the prefab render, not a replacement: prefab stays available and
      is still the offline-capable option. Built server-side and
      deterministically - a table of known rows has nothing for a model to
      resolve, so there is no generation step and nothing to hallucinate. */
  appSurface: (id: string) =>
    j<{ surface: A2uiSurface; notes?: string[] }>(
      `/api/apps/${encodeURIComponent(id)}/a2ui`,
    ),
  /** Refresh with live progress, as an AG-UI event stream.
      `POST /refresh` is one blocking call that fetches a third-party feed, so
      on a slow source the board showed nothing until it returned. This yields
      the typed frames instead, which is what lets the UI name the phase it is
      in and close it when the run ends.

      fetch + ReadableStream, never EventSource: EventSource cannot set the
      X-Aria-Token header, so every frame would 401. `sseFrames` is
      protocol-agnostic, so this is the same reader the chat streams use. */
  async *trackerRefreshStream(id: string, signal?: AbortSignal): AsyncGenerator<AguiEvent> {
    const r = await fetch(`/api/apps/${encodeURIComponent(id)}/refresh/stream`, {
      method: 'POST',
      headers: headers({ 'Content-Type': 'application/json' }),
      body: '{}',
      signal,
    });
    if (!r.ok) throw new Error(await reason(r));
    if (!r.body) throw new Error('empty response body');
    let sawTerminal = false;
    for await (const f of sseFrames(r, signal)) {
      if (f.type === 'RUN_FINISHED' || f.type === 'RUN_ERROR') sawTerminal = true;
      /* Frames arrive as `Record<string, unknown>`; the cast names the subset
         this client reads. Every field is optional because a server may add an
         event type we do not model yet - reading `type` off an unknown frame
         must not throw. */
      yield f as unknown as AguiEvent;
    }
    if (!sawTerminal && !signal?.aborted) {
      throw new Error('refresh stream ended before the run finished');
    }
  },
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
  /** Short-lived signed pass for the prefab iframe. The iframe is
      a browser navigation and cannot send the X-Aria-Token header,
      so the view 403'd inside its own frame. Fetch the pass through
      this authenticated call and mount the iframe with
      `?t=<sig>&exp=<exp>` — the server re-verifies the HMAC inside
      the auth middleware. An empty token means auth is disabled and
      the iframe loads bare. */
  prefabToken: (id: string) =>
    j<{ token: string; exp: number }>(
      `/api/apps/${encodeURIComponent(id)}/prefab-token`,
    ),
  /* Parts library. The three legacy scratch proxies still exist for
     compatibility, but the view uses the versioned library routes. */
  partsList: () =>
    j<{ parts: PartSummary[]; stats?: Record<string, number> }>('/api/parts'),
  partDetail: (id: string) =>
    j<{ part: PartDetail }>(`/api/parts/${encodeURIComponent(id)}`, { cache: 'no-store' }),
  partImport: (file: File, opts: { name?: string; description?: string;
    tags?: string[]; scale?: number; autoRepair?: boolean }) => {
    const fd = new FormData();
    fd.append('file', file, file.name);
    if (opts.name) fd.append('name', opts.name);
    if (opts.description) fd.append('description', opts.description);
    if (opts.tags?.length) fd.append('tags', JSON.stringify(opts.tags));
    if (opts.scale) fd.append('scale', String(opts.scale));
    fd.append('auto_repair', opts.autoRepair === false ? 'false' : 'true');
    return j<{ part?: PartDetail; error?: string; existing?: PartSummary }>(
      '/api/parts', { method: 'POST', body: fd });
  },
  partUpdate: (id: string, patch: { name?: string; description?: string; tags?: string[] }) =>
    pj<{ part?: PartDetail; error?: string }>(
      `/api/parts/${encodeURIComponent(id)}`, patch, { method: 'PATCH' }),
  partSetView: (id: string, view: PartView) =>
    pj<{ part?: PartDetail; error?: string }>(
      `/api/parts/${encodeURIComponent(id)}/view`, view, { method: 'PATCH' }),
  partDelete: (id: string) =>
    j<{ ok?: boolean; error?: string }>(`/api/parts/${encodeURIComponent(id)}`,
      { method: 'DELETE' }),
  partDuplicate: (id: string, name?: string) =>
    pj<{ part?: PartDetail; error?: string }>(
      `/api/parts/${encodeURIComponent(id)}/duplicate`, { name }),
  partAnalyse: (id: string, body: { version?: string; repair?: boolean; slicer?: boolean }) =>
    pj<{ part?: PartDetail; error?: string }>(
      `/api/parts/${encodeURIComponent(id)}/analyse`, body),
  partRepair: (id: string, body: { version?: string; watertight?: boolean;
    fix_normals?: boolean; weld?: boolean }) =>
    pj<{ part?: PartDetail; version?: PartVersion; changed?: boolean;
         result?: Record<string, unknown>; error?: string }>(
      `/api/parts/${encodeURIComponent(id)}/repair`, body),
  partOptimise: (id: string, body: { version?: string; pass: string;
    params?: Record<string, unknown> }) =>
    pj<{ part?: PartDetail; version?: PartVersion; changed?: boolean;
         improved?: boolean; result?: Record<string, unknown>; error?: string }>(
      `/api/parts/${encodeURIComponent(id)}/optimise`, body),
  partOptimiseLoop: (id: string, body: { version?: string; pass: string;
    params?: Record<string, unknown>; max_steps?: number }) =>
    pj<{ job?: PartJob; error?: string }>(
      `/api/parts/${encodeURIComponent(id)}/optimise-loop`, body),
  partJobs: (partId?: string) =>
    j<{ jobs: PartJob[] }>(`/api/parts/jobs${partId ? `?part_id=${encodeURIComponent(partId)}` : ''}`),
  partJob: (jobId: string) =>
    j<{ job?: PartJob; error?: string }>(`/api/parts/jobs/${encodeURIComponent(jobId)}`,
      { cache: 'no-store' }),
  partCancelJob: (jobId: string) =>
    j<{ job?: PartJob; error?: string }>(
      `/api/parts/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' }),
  partRestore: (id: string, version: string) =>
    j<{ part?: PartDetail; error?: string }>(
      `/api/parts/${encodeURIComponent(id)}/versions/${encodeURIComponent(version)}/restore`,
      { method: 'POST' }),
  partGeometry: (id: string, version: string, maxFaces = 140_000) =>
    j<{ geometry: PartGeometry; metrics?: PartMetrics; objective?: number;
         note?: string; error?: string }>(
      `/api/parts/${encodeURIComponent(id)}/versions/${encodeURIComponent(version)}/geometry?max_faces=${maxFaces}`),
  /** Authenticated blob download. An <a href> cannot carry X-Aria-Token. */
  partDownload: async (id: string, version?: string): Promise<Blob> => {
    const path = version
      ? `/api/parts/${encodeURIComponent(id)}/versions/${encodeURIComponent(version)}/stl`
      : `/api/parts/${encodeURIComponent(id)}/stl`;
    const r = await fetch(path, { headers: headers() });
    if (!r.ok) throw new Error(await reason(r));
    return await r.blob();
  },
  blenderStatus: () => j<BlenderStatus>('/api/blender/status', { cache: 'no-store' }),
  blenderCatalog: (query = '', limit = 30) =>
    j<BlenderCatalog>(`/api/blender/catalog?query=${encodeURIComponent(query)}&limit=${limit}`),
  blenderOpen: (id: string, version: string, bounds: BlenderBounds) =>
    pj<Record<string, unknown> & { error?: string }>(
      `/api/parts/${encodeURIComponent(id)}/blender/open`,
      { version, bounds }),
  blenderBounds: (id: string, bounds: BlenderBounds) =>
    pj<Record<string, unknown> & { error?: string }>(
      `/api/parts/${encodeURIComponent(id)}/blender/bounds`, { bounds }),
  blenderShow: (id: string, visible: boolean) =>
    pj<Record<string, unknown> & { error?: string }>(
      `/api/parts/${encodeURIComponent(id)}/blender/show`, { visible }),
  blenderSync: (id: string) =>
    pj<{ part?: PartDetail; version?: string; bytes?: number; error?: string }>(
      `/api/parts/${encodeURIComponent(id)}/blender/sync`, {}),
  blenderOp: (id: string, body: { version?: string; op: string;
    params?: Record<string, unknown>; active?: string; mode?: string }) =>
    pj<{ part?: PartDetail; result?: Record<string, unknown>;
         job?: string; error?: string }>(
      `/api/parts/${encodeURIComponent(id)}/blender/op`, body),
  blenderRender: (id: string, body: { version?: string; engine?: string;
    width?: number; height?: number; samples?: number; transparent?: boolean }) =>
    pj<BlenderRender>(`/api/parts/${encodeURIComponent(id)}/blender/render`, body),
  blenderClose: (id: string) =>
    pj<Record<string, unknown> & { error?: string }>(
      `/api/parts/${encodeURIComponent(id)}/blender/close`, {}),
  partCapabilities: () => j<PartCapabilities>('/api/parts/capabilities', { ttlMs: LIST_TTL_MS }),
  partPasses: () => j<{ passes: PartPassInfo[]; error?: string }>(
    '/api/parts/passes', { ttlMs: LIST_TTL_MS }),
  memory: (params: URLSearchParams, signal?: AbortSignal) =>
    j<{ items?: MemItem[]; has_more?: boolean; error?: string }>(
      `/api/memory?${params}`, { signal }),
  memoryStats: (signal?: AbortSignal) =>
    j<MemStats>('/api/memory/stats', { signal }),
  remember: (body: Record<string, unknown>) =>
    pj<{ status: string; id?: string }>('/api/memory/remember', body),
  /* The behavioural profile. Separate from `memory` because the evidence is
     the point: each row carries the user's own words, so a wrong observation
     is visibly a guess and can be deleted knowing what it rested on. */
  profile: (limit = 200, signal?: AbortSignal) =>
    j<{
      enabled: boolean;
      note?: string;
      observations?: {
        id: string;
        observation: string;
        evidence: string;
        confidence?: number;
        /** How many separate conversations this has turned up in. One is an
            anecdote, not a tendency. */
        seen_count?: number;
        created_at?: string;
      }[];
    }>(`/api/profile?limit=${Math.min(Math.max(limit, 1), 200)}`, { signal }),
  /* Write the whole profile - observations AND standing preferences - into the
     Documents section as markdown. */
  saveProfileDocument: () =>
    pj<{ ok: boolean; document_id?: string; filename?: string; error?: string }>(
      '/api/profile/document', {}),
  /* `confirm=wipe` is required server-side: an unscoped DELETE /api/memory
     deletes every drawer, so the intent has to be explicit. */
  wipeMemory: (sessionId: string) =>
    j<{ status?: string; result?: Record<string, number> }>(
      `/api/memory?session_id=${encodeURIComponent(sessionId)}&confirm=wipe`,
      { method: 'DELETE' }),
  /* Delete exactly one memory row. Distinct from wipeMemory, which clears every
     drawer for a session — that is far too destructive for fixing one
     mis-captured preference. */
  /* Per-conversation document access. Defaults to on; a conversation that
     turns it off gets no document chunks in its prompt.
     `doc_ids` binds the thread to specific documents — "discuss this
     document" — and is stored with the thread, so the binding survives a
     reload instead of being re-sent by whichever page happens to be open. */
  chatThreadPrefs: (id: string) =>
    j<{ prefs?: { use_documents: boolean; folder_ids?: string[]; doc_ids?: string[] } }>(
      `/api/chat/threads/${encodeURIComponent(id)}/prefs`),
  setChatThreadPrefs: (id: string, useDocuments: boolean,
                       folderIds?: string[], docIds?: string[]) =>
    pj<{ prefs?: { use_documents: boolean; folder_ids?: string[]; doc_ids?: string[] } }>(
      `/api/chat/threads/${encodeURIComponent(id)}/prefs`,
      { use_documents: useDocuments,
        ...(folderIds ? { folder_ids: folderIds } : {}),
        ...(docIds ? { doc_ids: docIds } : {}) }),
  deleteMemory: (id: string) =>
    j<Record<string, unknown>>(`/api/memory/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  events: (limit = 200) => j<{ events?: FeedEvent[]; count?: number }>(
    `/api/events?limit=${limit}`, { ttlMs: LIST_TTL_MS }),
  config: () => j<{ raw?: string; error?: string }>('/api/config'),
  templates: () => j<{ templates?: unknown[] }>('/api/templates'),
  /* Documents. Upload returns as soon as the bytes are stored; indexing
     continues in the background, so the caller polls the list. `fresh` opts
     the poll out of the read-through cache: statuses change every few seconds
     while a file is being indexed, but a section switch can still reuse the
     list the user just saw. */
  documents: (signal?: AbortSignal, fresh = false) =>
    j<{ documents?: DocItem[]; error?: string }>(
      '/api/documents',
      fresh
        ? { cache: 'no-store', ...(signal ? { signal } : {}) }
        : { ttlMs: LIST_TTL_MS, ...(signal ? { signal } : {}) }),
  documentDetail: (id: string) =>
    j<{ document?: DocItem; error?: string }>(
      `/api/documents/${encodeURIComponent(id)}`),
  /** Raw upload bytes for the inline preview. Not `j`: the body is
      the file itself, not JSON, and the browser's native viewer
      needs the blob. */
  documentContent: async (id: string): Promise<Blob> => {
    const r = await fetch(
      `/api/documents/${encodeURIComponent(id)}/content`,
      { headers: headers() });
    if (!r.ok) throw new Error(await reason(r));
    return r.blob();
  },
  uploadDocument: (file: File, folderId = '') => {
    const fd = new FormData();
    fd.append('file', file, file.name);
    if (folderId) fd.append('folder_id', folderId);
    // NOT pj: it forces Content-Type: application/json, which would override
    // the multipart boundary the browser generates and the server could not
    // parse the file at all.
    return j<{ document: DocItem; warnings?: string[]; error?: string }>(
      '/api/documents', { method: 'POST', body: fd });
  },
  documentFolders: () => j<{ folders: DocFolder[] }>('/api/documents/folders'),
  createDocumentFolder: (name: string, parentId = '') =>
    pj<{ folder?: DocFolder; error?: string }>('/api/documents/folders',
      { name, parent_id: parentId }),
  renameDocumentFolder: (id: string, name: string) =>
    pj<{ folder?: DocFolder; error?: string }>(
      `/api/documents/folders/${encodeURIComponent(id)}`, { name },
      { method: 'PATCH' }),
  deleteDocumentFolder: (id: string) =>
    pj<{ status?: string; error?: string }>(
      `/api/documents/folders/${encodeURIComponent(id)}`, {},
      { method: 'DELETE' }),
  moveDocument: (id: string, folderId: string) =>
    pj<{ document?: DocItem; error?: string }>(
      `/api/documents/${encodeURIComponent(id)}/folder`,
      { folder_id: folderId }),
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

  /* Code workspace. The editor starts on an empty scratch folder; Open Folder
     or New Playground re-roots it, path-confined, at a local directory. The
     console pays a human confirm for the one write path (save), and the
     coding agent's writes pause for approval in the panel (see views/Code.tsx). */
  codeRoots: (sid?: string) =>
    j<{ root?: string; roots?: { name: string; exists: boolean }[]; max_bytes?: number }>(
      `/api/code/roots${sidQs(sid)}`),
  /** The workspace the explorer is browsing right now. */
  codeWorkspace: (sid?: string) =>
    j<CodeWorkspace>(`/api/code/workspace${sidQs(sid)}`),
  /** Open a folder on this machine as the Code workspace. Single-operator,
   *  loopback, token-gated: the same trust level as the user's own shell.
   *  Re-roots only `/api/code/*`; the agent's tool sandbox is separate.
   *
   *  With a `sid` the server adds the folder to THAT session instead of
   *  rewriting the process globals — which is the difference between opening a
   *  project here and silently re-pointing another tab's editor, or re-rooting
   *  an agent that is mid-run. */
  openCodeWorkspace: (path: string, sid?: string) =>
    pj<CodeWorkspace>(`/api/code/workspace${sidQs(sid)}`, { path }),
  /** Create an empty scratch workspace and switch to it. */
  newCodePlayground: (sid?: string) =>
    pj<CodeWorkspace>(`/api/code/workspace${sidQs(sid)}`, { playground: true }),
  /** Return to the shipped default: the empty scratch workspace. */
  resetCodeWorkspace: (sid?: string) =>
    pj<CodeWorkspace>(`/api/code/workspace/reset${sidQs(sid)}`, {}),
  codeTree: (path: string, signal?: AbortSignal, sid?: string) =>
    j<{ path?: string; parent?: string; entries?: CodeEntry[]; error?: string }>(
      `/api/code/tree?path=${encodeURIComponent(path)}${sidQs(sid, true)}`,
      signal ? { signal } : undefined),
  codeFiles: (signal?: AbortSignal, sid?: string) =>
    j<{ files?: string[]; count?: number; truncated?: boolean }>(
      `/api/code/files${sidQs(sid)}`, signal ? { signal } : undefined),
  /** Syntax-check a DRAFT buffer. Takes text, never a path — the server judges
      what is on screen and cannot be turned into a second file reader. */
  codeCheck: (text: string, language: string, signal?: AbortSignal) =>
    pj<CodeCheck>('/api/code/check', { text, language },
      signal ? { signal } : undefined),
  codeSearch: (q: string, limit = 200, signal?: AbortSignal, sid?: string) =>
    j<{ matches?: CodeMatch[]; count?: number; truncated?: boolean }>(
      `/api/code/search?q=${encodeURIComponent(q)}&limit=${limit}${sidQs(sid, true)}`,
      signal ? { signal } : undefined),
  /* NOT wrapped in safe(). Every failure came back as null, so a 413 "file too
     large", a 415 "not UTF-8 text", a 403 "outside the allowed roots" and a
     dead gateway were all the same dead end to the user: "cannot open
     <path>". The server already sends a readable reason; let it through. */
  codeFile: (path: string, signal?: AbortSignal, sid?: string) =>
    j<CodeFile>(`/api/code/file?path=${encodeURIComponent(path)}${sidQs(sid, true)}`,
      signal ? { signal } : undefined),
  /** Run the project's own lint / typecheck / tests.
   *
   *  Distinct from codeCheck, which judges the BUFFER with a parser: this one
   *  runs pytest / tsc / npm test / ruff / cargo / go vet for whichever the
   *  project actually has. Slow by nature - that is the point. It is what makes
   *  the panel a feedback loop rather than a spell-checker. */
  codeChecks: (path?: string, signal?: AbortSignal, sid?: string) =>
    pj<CodeChecks>(`/api/code/checks${sidQs(sid)}`, { path: path || '' },
      signal ? { signal } : undefined),

  /* ── Studio workspaces (slice 1 surface) ───────────────────────────────
     A session holds folders; the process holds one. These are the only
     routes that read or write that list, and `validate_root` on the server is
     the rule — which refuses the state directory and anything holding it, so
     a mis-clicked "Open Folder" cannot publish sessions/ or agent.token. */
  studioWorkspace: (sid: string, signal?: AbortSignal) =>
    j<StudioWorkspace>(`/api/studio/roots?sid=${encodeURIComponent(sid)}`,
      signal ? { signal } : undefined),
  studioRootAdd: (sid: string, path: string) =>
    pj<StudioWorkspace>(`/api/studio/roots${sidQs(sid)}`, { path }),
  studioRootRemove: (sid: string, path: string) =>
    pj<StudioWorkspace>(`/api/studio/roots/remove${sidQs(sid)}`, { path }),
  /** The picker's directory read: folders only, drives at the top. */
  studioFsList: (path: string, signal?: AbortSignal) =>
    j<StudioFsListResult>(`/api/studio/fs/list?path=${encodeURIComponent(path)}`,
      signal ? { signal } : undefined),
  /** Open the OS folder dialog on the machine running the agent. Returns
   *  {path} when the user picked one, {cancelled: true} when they dismissed
   *  it, or an error when the platform has no dialog — the console falls
   *  back to the in-app picker then. The call blocks until the dialog is
   *  closed, up to five minutes. */
  studioPickFolder: () =>
    pj<{ path?: string; cancelled?: boolean; error?: string }>(
      '/api/studio/pick-folder', {}),
  studioPlayground: (sid: string) =>
    pj<StudioWorkspace>(`/api/studio/playground${sidQs(sid)}`, {}),
  studioReset: (sid: string) =>
    pj<StudioWorkspace>(`/api/studio/workspace/reset${sidQs(sid)}`, {}),
  /** The sessions the left column lists. This is a list of AGENTS rather than
   *  a list of files, which is the framing the whole surface is built on. */
  studioSessions: (sid: string, signal?: AbortSignal) =>
    j<StudioSessionsResult>(`/api/studio/sessions?sid=${encodeURIComponent(sid)}`,
      signal ? { signal } : undefined),
  /** Name a session. The first task typed becomes its title. */
  studioSessionRename: (sid: string, label: string) =>
    pj<{ ok?: boolean; sid?: string; label?: string; error?: string }>(
      `/api/studio/session/rename${sidQs(sid)}`, { sid, label }),
  studioSessionArchive: (sid: string, archived: boolean) =>
    pj<{ ok?: boolean; sid?: string; archived?: boolean; error?: string }>(
      `/api/studio/session/archive${sidQs(sid)}`, { sid, archived }),
  /** Slice 4 — the review model, per session.
   *
   *  `path` additionally returns that file's baseline text. Without it the
   *  list is deliberately metadata-only: shipping every snapshot in the
   *  session to paint a sidebar would be the same write-amplification mistake
   *  the memory store already made, in the read direction. */
  studioChanges: (sid: string, path?: string, signal?: AbortSignal) =>
    j<StudioChangesResult>(
      `/api/studio/changes?sid=${encodeURIComponent(sid)}`
      + (path ? `&path=${encodeURIComponent(path)}` : ''),
      // Liveness: this is the unreviewed-changes truth per turn; a 30s cache
      // keyed on the identical URL serves pre-turn values at turn end.
      { cache: 'no-store', ...(signal ? { signal } : {}) }),
  /** Slice 5 — the runs this session has. A run is an object now: an id you
   *  can stop from another tab, a transcript a reload can replay, a cost. */
  studioRuns: (sid: string, signal?: AbortSignal) =>
    j<StudioRunsResult>(`/api/studio/runs?sid=${encodeURIComponent(sid)}`,
      // Same reason as changes/checkpoints: liveness cannot be cached.
      { cache: 'no-store', ...(signal ? { signal } : {}) }),
  /** The watcher stream: this session's run statuses, pushed as they change.
   *
   *  The server closes it when nothing is live, so a tab with no agents
   *  running costs one short request. The caller reopens it when it starts a
   *  run — which is also why this is a generator rather than a hook: the
   *  re-open has to happen next to the code that starts the run. */
  async *studioLive(sid: string,
                    signal?: AbortSignal): AsyncGenerator<StudioRunsResult> {
    const r = await fetch(`/api/studio/live${sidQs(sid)}`,
      { headers: headers(), signal });
    if (!r.ok) throw new Error(await reason(r));
    if (!r.body) throw new Error('empty response body');
    for await (const f of sseFrames(r, signal)) {
      if (f.type === 'status') yield f as unknown as StudioRunsResult;
    }
  },
  /** Replay one run: the frames, in the order they were sent. `after` is a
   *  line offset, so a long transcript can be followed rather than refetched. */
  studioRunFrames: (sid: string, runId: string, after = 0,
                    signal?: AbortSignal) =>
    j<StudioRunFrames>(
      `/api/studio/runs/${encodeURIComponent(runId)}`
      + `?sid=${encodeURIComponent(sid)}&after=${after}`,
      { cache: 'no-store', ...(signal ? { signal } : {}) }),
  /** Stop a run AT ITS NEXT HOP. A different verb from aborting the fetch,
   *  which only closes this view and leaves the loop buying model calls. */
  studioRunStop: (sid: string, id: string) =>
    pj<{ ok?: boolean; id?: string; error?: string; note?: string }>(
      '/api/studio/run/stop', { sid, id }),
  /** What `@<fragment>` completes to, across every folder the session holds.
   *  Server-side on purpose: the alternative is shipping a whole repository
   *  tree to the browser to filter it on every keystroke. */
  studioMentions: (sid: string, q: string, signal?: AbortSignal) =>
    j<StudioMentionsResult>(
      `/api/studio/mentions?sid=${encodeURIComponent(sid)}`
      + `&q=${encodeURIComponent(q)}`, signal ? { signal } : undefined),
  /** The slash commands, served rather than hardcoded: a popup that invents a
   *  verb is a button that does nothing. */
  studioCommands: (signal?: AbortSignal) =>
    j<{ commands?: StudioCommand[] }>(
      '/api/studio/commands', signal ? { signal } : undefined),
  /** A run's real spend, from the gateway ledger. `unknown: true` means the
   *  ledger was not reachable — the panel must show a gap, not $0.00. */
  studioRunCost: (sid: string, runId: string, signal?: AbortSignal) =>
    j<StudioRunCost>(
      `/api/studio/runs/${encodeURIComponent(runId)}/cost`
      + `?sid=${encodeURIComponent(sid)}`, signal ? { signal } : undefined),
  /** Draft a commit message from the staged (or working) diff. One bounded
   *  call, not an agent run — and it reports which hunks it did not see. */
  studioCommitMsg: (sid: string, staged = true, model?: string) =>
    pj<StudioCommitMsgResult>('/api/studio/commit-msg', {
      sid, staged, ...(model ? { model } : {}),
    }),
  /** Slice 8 — file operations, batched so one user intent is one undo. */
  studioFiles: (sid: string, ops: StudioFileOp[]) =>
    pj<StudioFilesResult>(`/api/studio/files${sidQs(sid)}`, { sid, ops }),
  /** Find in files, with the expression switch the IDE needs. */
  studioSearch: (sid: string, q: string, opts?: {
    regex?: boolean; case?: boolean; glob?: string; limit?: number;
  }, signal?: AbortSignal) => {
    const p = new URLSearchParams({ sid, q });
    p.set('regex', String(opts?.regex !== false));
    p.set('case', String(opts?.case !== false));
    if (opts?.glob) p.set('glob', opts.glob);
    if (opts?.limit) p.set('limit', String(opts.limit));
    return j<StudioSearchResult>(`/api/studio/search?${p}`,
      signal ? { signal } : undefined);
  },
  /** The dry run: returns the token the apply step must carry. */
  studioReplacePreview: (sid: string, body: {
    hits: StudioSearchMatch[]; pattern: string; replacement: string;
    regex?: boolean; case?: boolean;
  }) => pj<StudioReplacePreview>(`/api/studio/replace${sidQs(sid)}`,
    { sid, ...body }),
  /** The apply. Without the preview token it is refused — that is the point. */
  studioReplaceApply: (sid: string, body: {
    hits: StudioSearchMatch[]; pattern: string; replacement: string; token: string;
    regex?: boolean; case?: boolean;
  }) => pjBody<StudioReplaceResult>(`/api/studio/replace${sidQs(sid)}`,
    { sid, apply: true, ...body }, [400, 409]),
  /** One git verb. `confirm` is what turns a 428 into an action. */
  studioGit: (sid: string, action: string, args?: Record<string, unknown>,
              confirm = false) =>
    pjBody<StudioGitResult>(`/api/studio/git${sidQs(sid)}`,
      { sid, action, args: args || {}, confirm }),
  studioJournal: (sid: string, limit = 200, signal?: AbortSignal) =>
    j<{ entries?: StudioJournalEntry[]; count?: number }>(
      `/api/studio/journal?sid=${encodeURIComponent(sid)}&limit=${limit}`,
      signal ? { signal } : undefined),
  /* ── Studio browser pane (slice 10) ─────────────────────────────── */
  studioBrowserStatus: (sid: string, signal?: AbortSignal) =>
    j<StudioBrowserStatus>(`/api/studio/browser/status?sid=${encodeURIComponent(sid)}`,
      signal ? { signal } : undefined),
  studioBrowserEvents: (sid: string, kind: 'console' | 'network' = 'console',
                        all = false, signal?: AbortSignal) =>
    j<StudioBrowserEvents>(
      `/api/studio/browser/events?sid=${encodeURIComponent(sid)}`
      + `&kind=${kind}&all=${all}`, signal ? { signal } : undefined),
  studioBrowserShots: (sid: string, limit = 24, signal?: AbortSignal) =>
    j<StudioBrowserShots>(
      `/api/studio/browser/shots?sid=${encodeURIComponent(sid)}&limit=${limit}`,
      signal ? { signal } : undefined),
  studioBrowserPage: (sid: string, kind: 'snapshot' | 'read' = 'snapshot',
                      signal?: AbortSignal) =>
    j<Record<string, unknown>>(
      `/api/studio/browser/page?sid=${encodeURIComponent(sid)}&kind=${kind}`,
      signal ? { signal } : undefined),
  studioBrowserNavigate: (sid: string, url: string) =>
    pjBody<StudioBrowserNav>(`/api/studio/browser/navigate${sidQs(sid)}`,
      { sid, url }, [400, 403, 500]),
  studioBrowserTab: (sid: string, action: string, index?: number, url?: string) =>
    pjBody<Record<string, unknown>>(`/api/studio/browser/tab${sidQs(sid)}`,
      { sid, action, index, url }, [400, 403, 500]),
  studioBrowserInput: (sid: string, ev: { kind: string; x?: number; y?: number; dx?: number; dy?: number; text?: string; key?: string }) =>
    pjBody<Record<string, unknown>>(`/api/studio/browser/input${sidQs(sid)}`,
      { sid, ...ev }, [400, 403, 500]),
  studioBrowserClose: (sid: string) =>
    pj<{ ok?: boolean; closed?: boolean; disabled?: boolean }>(
      `/api/studio/browser/close${sidQs(sid)}`, { sid }),
  /** The live view. Yields each frame as it arrives and returns when the
   *  server ends the window, so the caller reopens it — the same shape as
   *  `studioLive`, and for the same reason: no long-lived idle socket. */
  async *studioBrowserView(sid: string, everyMs = 50,
                           signal?: AbortSignal): AsyncGenerator<StudioBrowserFrame> {
    const r = await fetch(
      `/api/studio/browser/view?sid=${encodeURIComponent(sid)}&every_ms=${everyMs}`,
      { headers: headers(), signal });
    if (!r.ok) throw new Error(await reason(r));
    if (!r.body) throw new Error('empty response body');
    for await (const f of sseFrames(r, signal)) {
      yield f as unknown as StudioBrowserFrame;
    }
  },
  /** Forget a session's runs and checkpoints. Never touches a workspace file:
   *  deleting an agent's memory is not the same act as deleting a project. */
  studioSessionForget: (sid: string) =>
    pj<{ ok?: boolean; sid?: string; error?: string }>(
      '/api/studio/session/forget', { sid }),
  /** Decide ONE hunk. `keep` folds it into the baseline and never touches the
   *  file; `revert` reverses it in the file. A hunk whose content has moved is
   *  a 409 that says so — the panel must show that rather than retry blindly. */
  studioHunkApply: (sid: string, path: string, hunkId: string,
                    action: 'keep' | 'revert') =>
    pj<StudioApplyResult>(`/api/studio/changes/apply${sidQs(sid)}`,
      { path, hunk_id: hunkId, action }),
  /** Named "before task N" states, newest first. Persisted, so these survive
   *  both a reload and an agent restart. */
  studioCheckpoints: (sid: string, signal?: AbortSignal) =>
    j<StudioCheckpointList>(
      `/api/studio/checkpoints?sid=${encodeURIComponent(sid)}`,
      { cache: 'no-store', ...(signal ? { signal } : {}) }),
  /** Put one turn's files back. The server captures what it replaces as a
   *  checkpoint of its own first, so this is always reversible from the same
   *  panel — `undo.id` is that checkpoint. */
  studioCheckpointRestore: (sid: string, id: string) =>
    pj<StudioCheckpointRestoreResult>(`/api/studio/checkpoint/restore${sidQs(sid)}`,
      { id }),
  /** Code intelligence for the agent panel.
   *
   *  One endpoint over the gateway's `code_intel`: symbols, references, blast
   *  radius, preview and apply. `preview` writes nothing, so the panel can call
   *  it freely to show a diff; only `apply` touches disk. */
  codeIntel: (
    op: 'symbols' | 'references' | 'blast' | 'preview' | 'apply'
      | 'definition' | 'hover' | 'rename',
    args: { path?: string; name?: string; text?: string; mode?: string;
            confirm?: boolean; line?: number; col?: number;
            new_name?: string } = {},
    signal?: AbortSignal,
  ) => pj<CodeIntelResult>(`/api/code/intel`, { op, ...args },
      signal ? { signal } : undefined),
  /** Roster for the Code panel's model picker, proxied from the gateway.
   *
   *  Goes through the agent rather than the gateway directly: the gateway has
   *  no CORS middleware and the page never receives the gateway token, so a
   *  direct fetch would be blocked twice over.
   *
   *  `all` asks for the whole catalogue rather than only the free tier - the
   *  picker groups free first but must still offer everything, or half the
   *  models Kilo serves would be unreachable from the UI.
   *
   *  `refresh` bypasses both caches (the gateway TTL and the 30s GET cache)
   *  so the user can pick up a newly added model without a restart. */
  models: (refresh = false, all = false, signal?: AbortSignal) => {
    const q = [refresh && 'refresh=true', all && 'all=true'].filter(Boolean);
    return j<ModelRoster>(`/api/models${q.length ? `?${q.join('&')}` : ''}`, { signal });
  },

  /** Ask the agent about the code in the editor.
   *
   *  Read-only by design: it answers, it does not edit. A question typed into a
   *  panel must not be able to rewrite files behind the user's back. Streams,
   *  because a grounded answer over a real file takes longer than a spinner
   *  should. fetch + ReadableStream, not EventSource: EventSource cannot send
   *  X-Aria-Token, so every frame would 401. */
  async *codeAskStream(question: string, path: string, text: string,
                        history?: { role: 'user' | 'assistant'; content: string }[],
                        signal?: AbortSignal,
                        model?: string): AsyncGenerator<CodeAskFrame> {
    const r = await fetch('/api/code/ask', {
      method: 'POST',
      headers: headers({ 'Content-Type': 'application/json' }),
      // `history` makes this a conversation rather than a series of unrelated
      // questions. The server keeps a bounded tail of the recent turns, so
      // "and what calls it?" still refers to the previous answer.
      // `model` is the picker's choice, sent as a bare `vendor/name` id: the
      // gateway resolves it to a provider itself, so the panel never has to
      // know or hardcode which provider serves it. Omitted when unset.
      body: JSON.stringify({ question, path, text, history: history ?? [], ...(model ? { model } : {}) }),
      signal,
    });
    if (!r.ok) throw new Error(await reason(r));
    if (!r.body) throw new Error('empty response body');
    for await (const f of sseFrames(r, signal)) {
      yield f as unknown as CodeAskFrame;
    }
  },
  /** The agentic counterpart to `codeAskStream`: the coding agent that can
   *  actually USE tools.
   *
   *  `/api/code/ask` is one model call over a pre-baked context slice — the
   *  model never gets to choose what to look at, so when it wants a tool it has
   *  nowhere to go and simply stops. This endpoint runs the tool loop: the
   *  model calls read_file / search_files / find_symbol …, each call and its
   *  result streams back as `tool_call` / `tool_result` frames, and the answer
   *  streams token by token as `delta`.
   *
   *  `model` is sent as a bare `vendor/name` id (the picker's choice); the
   *  gateway resolves it, and a Kilo id is what routes the request to Kilo
   *  rather than the general ladder. Omitting it lets the server default. */
  async *codeAgentStream(question: string, path: string, text: string,
                         history?: { role: 'user' | 'assistant'; content: string }[],
                         signal?: AbortSignal,
                         model?: string,
                         maxHops?: number,
                         paths?: string[],
                         autoApprove = false,
                         readOnly = false,
                         context?: {
                           tabs?: string[];
                           diagnostics?: Array<{
                             path?: string; line?: number; col?: number;
                             message?: string;
                           }>;
                           /** `@browser` — the page this session's browser is
                            *  on. Sent as an object rather than as pasted text
                            *  so the server can bound and label the excerpt the
                            *  same way it bounds a file. */
                           browser_page?: {
                             url: string; title?: string; text?: string;
                             errors?: string[];
                           };
                         },
                         /* Commands and checks are a separate opt-in from file
                            writes: running the project's checks executes
                            repository code, so "Auto" on writes has never
                            implied it. Trailing so no positional caller shifts. */
                         autoApproveExec = false,
                         /* Scopes "always allow this command for this session".
                            Without an owner a standing approval in one session
                            could authorise another session's command. */
                         session?: string,
                         /* The studio session this run belongs to — which
                            FOLDERS it may touch. Sent in the query as well as
                            the body because the middleware that binds a
                            session reads the query, and a run that binds
                            nothing does not fail: it silently works in the
                            process-wide default workspace instead of the
                            project the user opened. That is the one failure
                            this parameter exists to prevent. */
                         sid?: string): AsyncGenerator<CodeAgentFrame> {
    const r = await fetch(`/api/code/agent${sidQs(sid)}`, {
      method: 'POST',
      headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({
        question, path, text, history: history ?? [],
        ...(sid ? { sid } : {}),
        ...(model ? { model } : {}),
        ...(maxHops ? { max_hops: maxHops } : {}),
        ...(paths && paths.length ? { paths } : {}),
        ...(autoApprove ? { auto_approve: true } : {}),
        ...(readOnly ? { read_only: true } : {}),
        ...(autoApproveExec ? { auto_approve_exec: true } : {}),
        ...(session ? { session } : {}),
        ...(context?.tabs?.length ? { tabs: context.tabs } : {}),
        ...(context?.diagnostics?.length
          ? { diagnostics: context.diagnostics } : {}),
      }),
      signal,
    });
    if (!r.ok) throw new Error(await reason(r));
    if (!r.body) throw new Error('empty response body');
    for await (const f of sseFrames(r, signal)) {
      yield f as unknown as CodeAgentFrame;
    }
  },
  /** Accept or refuse one proposed agent write.
   *
   *  The coding agent CAN edit, but never unattended: each write parks the run
   *  and emits a `permission` frame carrying the exact diff. This call is what
   *  releases it. A refusal is a real outcome - the model is told the change was
   *  declined and continues - so the panel can always say "Decline" and let the
   *  agent adapt rather than hang. */
  codePermission: (id: string, approve: boolean, remember = false) =>
    pj<{ status?: string; id?: string; approve?: boolean; error?: string }>(
      `/api/code/permission/${encodeURIComponent(id)}`,
      { approve, ...(remember ? { remember: true } : {}) }),
  /** Files the agent changed whose change is still unreviewed.
   *
   *  With auto-approve the writes land immediately and the user keeps or
   *  reverts each file afterwards; this lists what is still pending so a page
   *  reload does not lose the review. `created` marks a file the agent brought
   *  into existence (Revert deletes it). */
  codeChanges: (signal?: AbortSignal, sid?: string) =>
    j<{ changes?: CodeAgentChange[] }>(`/api/code/agent/changes${sidQs(sid)}`,
      signal ? { signal } : undefined),
  /** Accept the agent's change to a path: it stays on disk, marks come off. */
  codeKeep: (path: string, sid?: string) =>
    pj<{ ok?: boolean; path?: string; error?: string }>(
      `/api/code/agent/keep${sidQs(sid)}`, { path }),
  /** Undo ONE hunk of an unreviewed change, leaving the rest of the file's
   *  edits in place. 409 when that hunk is no longer in the file — the server
   *  refuses rather than guessing at a merge. */
  codeRevertHunk: (path: string, hunk: number, sid?: string) =>
    pj<{ ok?: boolean; path?: string; hunks_remaining?: number;
         error?: string }>(
      `/api/code/agent/revert-hunk${sidQs(sid)}`, { path, hunk }),
  /** Put a path back the way it was before the agent touched it. A file the
   *  agent created is removed. */
  codeRevert: (path: string, sid?: string) =>
    pj<{ ok?: boolean; path?: string; restored?: boolean; deleted?: boolean;
         error?: string }>(`/api/code/agent/revert${sidQs(sid)}`, { path }),
  /** Save a buffer to disk. The only human write route, and gated: the payload
   *  must carry confirm:true (the user pressed save) plus the mtime the caller
   *  read, so a stale tab cannot silently clobber newer work. */
  codeWrite: (path: string, text: string, opts: { confirm: boolean; mtime?: number; sid?: string }) => {
    const { sid, ...rest } = opts;
    return pj<{ ok?: boolean; path?: string; written?: boolean; unchanged?: boolean;
         backup?: string; bytes?: number; error?: string }>(
      `/api/code/write${sidQs(sid)}`, { path, text, ...rest });
  },
  /** Create one empty file in the workspace, for the explorer's "New file".
   *
   *  `/api/code/write` only saves a buffer that was already open, so nothing in
   *  the console could bring a file into existence. This can only add an empty
   *  file (never truncates), and needs confirm:true like every write here. */
  codeNewFile: (name: string, dir?: string, sid?: string) =>
    pj<{ ok?: boolean; path?: string; created?: boolean; bytes?: number; error?: string }>(
      `/api/code/newfile${sidQs(sid)}`, { name, dir: dir || '', confirm: true }),
  /** Review what is on disk: status, diff, or recent log.
   *
   *  Read-only by construction — the server offers no write verb, so this
   *  cannot stage, commit or revert. It is the review step for anything that
   *  edited a file: browser drafts are invisible here, disk changes are not. */
  codeGit: (target: 'status' | 'diff' | 'log' = 'status',
            signal?: AbortSignal, sid?: string) =>
    j<{ ok?: boolean; target?: string; exit_code?: number | null;
         output?: string; truncated?: boolean; error?: string }>(
      `/api/code/git?target=${target}${sidQs(sid, true)}`,
      signal ? { signal } : undefined),
  adopt: (sessionId: string) =>
    pj<{ conversation_id?: string; error?: string }>('/api/conversations/adopt', {
      session_id: sessionId,
    }),
  /** Node ids are per-run counters (n:1, n:2 …), so a node_id-only
      lookup returns the latest vote cast on that index in ANY run —
      a thumbs-down on yesterday's n:3 would color today's n:3.
      Always pass the session id to scope the read. */
  feedbackGet: (nodeId: string, sessionId?: string, msgId?: string) =>
    j<{ vote: number; note?: string; feedback_id?: string; t?: number }>(
      `/api/feedback?node_id=${encodeURIComponent(nodeId)}` +
      (msgId ? `&msg_id=${encodeURIComponent(msgId)}` : '') +
      `${sessionId ? `&session_id=${encodeURIComponent(sessionId)}` : ''}`,
    ),
  /** `skill` is sent so votes can be rolled up per skill. Without it the
      server can only group by node id, and a rollup over pruned runs loses
      the attribution entirely. `msgId` scopes chat-turn votes (assistant chat
      messages have no DAG node id). `note` is free-text explanation, capped
      server-side to 280 chars. */
  feedbackPost: (sessionId: string, nodeId: string, vote: 1 | -1 | 0, skill?: string, msgId?: string, note?: string) =>
    pj<{ status: string; feedback_id?: string; learning?: 'queued' | 'no_note' | 'disabled' | 'error' }>(
      '/api/feedback', { session_id: sessionId, node_id: nodeId, vote, skill, msg_id: msgId, note }),
  /** Progress of the feedback → memory learner: what is waiting, what was
      applied, and whether automatic learning/autosave is switched on. */
  feedbackLearning: () => j<FeedbackLearningStatus>('/api/feedback/learning'),
  /** Gateway voice channel readiness (Kokoro TTS + Whisper STT). */
  voiceStatus: () => j<VoiceStatus>('/api/voice/status'),
  ttsVoices: () => j<{ voices: VoiceOption[] }>('/api/tts/voices'),
  /** Synthesize text to a playable WAV Blob through the gateway. */
  tts: async (text: string, voice = 'af_heart', speed = 1.0): Promise<Blob> => {
    const r = await fetch('/api/tts', {
      method: 'POST',
      headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ text, voice, speed }),
    });
    if (!r.ok) throw new Error(await reason(r));
    return await r.blob();
  },
  /** Transcribe a recorded clip through the gateway's Whisper service. */
  /** Sentence-chunked TTS. Events arrive as the gateway synthesizes them;
      the caller plays chunk N while N+1 is still being made. */
  ttsStream: async function* (
    text: string, voice = 'af_heart', speed = 1.0, signal?: AbortSignal,
  ): AsyncGenerator<TtsStreamEvent> {
    const r = await fetch('/api/tts/stream', {
      method: 'POST',
      headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ text, voice, speed }),
      signal,
    });
    if (!r.ok) throw new Error(await reason(r));
    if (!r.body) throw new Error('empty voice stream');
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let nl = buf.indexOf('\n');
        while (nl >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (line) {
            let ev: TtsStreamEvent;
            try { ev = JSON.parse(line) as TtsStreamEvent; }
            catch { ev = { type: 'error', error: 'malformed voice chunk' }; }
            yield ev;
            if (ev.type === 'error' || ev.type === 'done') return;
          }
          nl = buf.indexOf('\n');
        }
      }
    } finally {
      try { reader.releaseLock(); } catch { /* already closed */ }
    }
  },
  stt: async (audio: Blob, filename = 'clip.webm'): Promise<{ text: string }> => {
    const form = new FormData();
    form.append('file', audio, filename);
    const r = await fetch('/api/stt', {
      method: 'POST',
      headers: headers(),
      body: form,
    });
    let data: { text?: string; error?: string } | null = null;
    try { data = await r.json(); } catch { /* non-JSON error body */ }
    if (!r.ok || data?.error) {
      throw new Error(data?.error || await reason(r));
    }
    return { text: String(data?.text || '') };
  },
  /** Aggregated votes: the only honest way to tell whether a prompt edit
      helped. `min_votes` marks low-sample rows rather than presenting them
      as a rate. */
  feedbackRollup: (days = 30, minVotes = 1) =>
    j<FeedbackRollup>(`/api/feedback/rollup?days=${days}&min_votes=${minVotes}`),
  /** Persona is Aria's AI-written, evolving portrait of the user — not a
      render of the memory drawers. `dry_run` defaults to true on the server
      for the separate feedback→memory cross-check. */
  profilePersona: () => j<{
    markdown: string; generated?: boolean; meta?: PersonaMeta;
  }>('/api/profile/persona'),
  profilePersonaEvolution: () => j<{ markdown: string }>(
    '/api/profile/persona/evolution'),
  profilePersonaRegenerate: () => pj<{
    ok: boolean; version?: number; markdown?: string; error?: string;
    meta?: PersonaMeta;
    documents?: { ok?: boolean; error?: string };
  }>('/api/profile/persona/regenerate', {}),
  profilePersonaSync: (dryRun = true) =>
    pj<FeedbackSyncResult>(
      `/api/profile/persona/sync?dry_run=${dryRun}`, { dry_run: dryRun }),
  profilePersonaDocument: () =>
    pj<{ ok: boolean; current?: string; evolution?: string; error?: string }>(
      '/api/profile/persona/document', {}),
  /** Runtime counters the UI cannot otherwise see: fetch-cache hit rate (the
      thing behind research latency), how many tool outcomes reached memory,
      and whether the MCP tool subprocess breaker is open. */
  gatewayUser: () => j<GatewayUserStatus>('/api/gateway/user'),
  gatewayUserSet: (token: string) =>
    pj<GatewayUserStatus>('/api/gateway/user', { token }),
  gatewayUserClear: () =>
    j<GatewayUserStatus>('/api/gateway/user', { method: 'DELETE' }),
  mcpStats: () => j<McpStats>('/api/mcp/stats'),
  /** Ask the agent to actually STOP a live run (server-side; the run ends
      at its next node boundary and remaining nodes are saved as `skipped`).
      `found:false` simply means nothing was running for that id. */
  cancelRun: (ids: { conversation_id?: string; session_id?: string }) =>
    pj<{ status: 'cancelled' | 'pending' | 'missing'; found: boolean;
         cancelled: number; lightweight_cancelled?: number;
         session_id?: string }>('/api/chat/cancel', ids),
  /** Structured contents of a rendered document: slide cards for a deck,
   *  page cards for a PDF, a grid for a workbook, paragraphs for a .docx.
   *  Read from the artifact that was ACTUALLY produced - the run's own
   *  `sections`/`slides` metadata is model-written and has shipped decks
   *  whose slides did not exist in the file. */
  artifactPreview: (artifactId: string) =>
    j<DocPreview>(`/api/artifact/${encodeURIComponent(artifactId)}/preview`),
  /** The same honest preview for a Documents-library row: the slide outline of
   *  a deck, the page text of a PDF, the sheets of a workbook — read from the
   *  stored file, never from a run's model-written metadata. */
  documentPreview: (docId: string) =>
    j<DocPreview>(`/api/documents/${encodeURIComponent(docId)}/preview`),
  /** Slide images for a deck (rasterised server-side by PowerPoint). `count` is
   *  0 when the deck could not be rasterised — the caller falls back to the
   *  text outline. */
  artifactSlides: (artifactId: string) =>
    j<{ count: number; rasterised: boolean }>(
      `/api/artifact/${encodeURIComponent(artifactId)}/slides`),
  documentSlides: (docId: string) =>
    j<{ count: number; rasterised: boolean }>(
      `/api/documents/${encodeURIComponent(docId)}/slides`),
  /** One slide as a PNG blob (1-based). */
  artifactSlideBlob: async (artifactId: string, n: number): Promise<Blob> => {
    const r = await fetch(
      `/api/artifact/${encodeURIComponent(artifactId)}/slide/${n}`,
      { headers: headers() });
    if (!r.ok) throw new Error(await reason(r));
    return await r.blob();
  },
  documentSlideBlob: async (docId: string, n: number): Promise<Blob> => {
    const r = await fetch(
      `/api/documents/${encodeURIComponent(docId)}/slide/${n}`,
      { headers: headers() });
    if (!r.ok) throw new Error(await reason(r));
    return await r.blob();
  },
  /** The artifact's own bytes, for a viewer that renders them itself.
   *  `download=0` makes the server answer inline with the real content type,
   *  which is what lets the browser's built-in PDF viewer open it — an
   *  `<a href>` cannot carry the launch token, so the bytes come through here
   *  and the iframe gets a blob URL. */
  artifactBlob: async (artifactId: string): Promise<Blob> => {
    const r = await fetch(`/api/artifact/${encodeURIComponent(artifactId)}?download=0`,
      { headers: headers() });
    if (!r.ok) throw new Error(await reason(r));
    return await r.blob();
  },
  /** Index a stored artifact into the Documents library if it is not there
      yet, and report the document id it lives under. `pj` swallows a non-2xx
      into `{error}`, which is what the caller wants to show. */
  indexArtifact: (artifactId: string) =>
    pj<{ document_id?: string; folder?: string; origin?: string;
         already?: boolean; error?: string }>(
      `/api/artifact/${encodeURIComponent(artifactId)}/documents`, {}),
  /** What a run has produced so far. This is how the Authoring view
   *  reattaches to a run the user walked away from: the run keeps executing
   *  server-side after the stream closes, so on return we poll this instead
   *  of having lost the deliverable entirely. */
  runProduced: (sid: string) =>
    j<{
      session_id: string;
      files: { artifact: string; filename: string; format: string; skill: string;
               bytes?: number; document_id?: string }[];
      answer: string;
      live: boolean;
      /** The run lost a branch: the answer is not complete. Persisted, so it
          survives reopening - previously this lived only in the live SSE
          frame and a reloaded run looked finished and whole. */
      degraded?: boolean;
      /** Which parts went unanswered, for the user rather than the log. */
      incomplete_detail?: string[];
      /** Stopped by the operator before finishing. */
      stopped?: boolean;
      nodes: { id: string; skill: string; status: string;
               duration?: number | null }[];
      /** The sources the researchers actually opened, for the receipt. */
      sources?: { url: string; title?: string }[];
    }>(`/api/sessions/${encodeURIComponent(sid)}/produced`, { cache: 'no-store' }),
  safe,
  /** POST /api/chat as an async generator of SSE events.
      Pass an AbortSignal to cancel mid-run (the generator closes the
      reader and stops — callers must also stop their graph polling). */
async *chat(query: string, conversationId?: string, signal?: AbortSignal,
               opts?: { research?: boolean; idempotencyKey?: string;
                        docSetup?: Record<string, unknown>;
                        depth?: 'quick' | 'standard' | 'deep';
                        folderIds?: string[];
                        docIds?: string[];
                        partId?: string;
                        versionId?: string;
                        runKind?: 'research' | 'authoring' | 'chat' | 'parts' | 'app' }): AsyncGenerator<ChatEvent> {
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
        /* What kind of run this is, declared rather than inferred later from
           the skills it emits. Persisted at run start, so a new research
           topic is listed in the sidebar immediately. */
        run_kind: opts?.runKind,
        /* Section runs are scoped to one library object. The server loads the
           brief from its own store, so the client cannot forge part metrics. */
        part_id: opts?.partId,
        version_id: opts?.versionId,
        /* Planner-only. `query` is the TOPIC itself; depth never rides inside
           it, or every worker reads it as its own instruction. */
        depth: opts?.depth,
        /* Folder subtree scope for this run's document retrieval. The body
           value wins over the chat-thread preference on the server. */
        folder_ids: opts?.folderIds?.length ? opts.folderIds : undefined,
        /* Bind this turn to specific documents. Same override rule as
           folder_ids, and what lets a document be discussed without the
           rest of the enabled corpus answering for it. */
        doc_ids: opts?.docIds?.length ? opts.docIds : undefined,
        /* The setup panel is applied to the render server-side, not merely
           described in the prompt - see _clean_doc_setup. */
        doc_setup: opts?.docSetup,
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
  async *chatSimpleStream(query: string, conversationId?: string, signal?: AbortSignal,
                          model?: string,
                          opts?: { docIds?: string[]; cite?: boolean }): AsyncGenerator<SimpleStreamEvent> {
    const r = await fetch('/api/chat/simple/stream', {
      method: 'POST',
      headers: headers({ 'Content-Type': 'application/json' }),
      // Conditional spread: an empty pick must leave the request identical
      // to the pre-picker shape so the gateway default still applies.
      body: JSON.stringify({ query, conversation_id: conversationId || undefined,
                              ...(model ? { model } : {}),
                             /* Bind this turn to the document on screen. A body
                                value is authoritative for its turn server-side,
                                so the panel that owns the thread sends it every
                                turn instead of writing preferences once. */
                             ...(opts?.docIds?.length ? { doc_ids: opts.docIds } : {}),
                             /* Ask for page-and-words citations. Only the
                                document pane sends it: a marker is a feature
                                when a viewer can light it up and noise
                                everywhere else. */
                             ...(opts?.cite ? { cite_markers: true } : {}) }),
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
  /* Guard the magnitude too. `if (!ts)` let a stored `when` of
     9999999999999999999 through: `new Date(NaN)` printed "Invalid Date" and
     `Math.round(NaN/1440)` rendered "in 115740740740720030d". A fire time
     further than a year out is corrupt data, not a schedule. */
  if (!ts || !Number.isFinite(ts) || Math.abs(ts) > 4e9) return '-';
  const d = new Date(ts * 1000);
  if (Number.isNaN(d.getTime())) return '-';
  const diff = ts * 1000 - Date.now();
  const when = d.toLocaleString();
  if (diff <= 0) return `${when} (due)`;
  /* Sub-minute granularity is accepted by the scheduler, so `in 20s` used to
     floor to the nonsense "in 0m". Show seconds below a minute. */
  if (diff < 60000) return `${when} (in ${Math.max(1, Math.round(diff / 1000))}s)`;
  const m = Math.round(diff / 60000);
  const rel = m < 60 ? `in ${m}m` : m < 1440 ? `in ${Math.round(m / 60)}h` : `in ${Math.round(m / 1440)}d`;
  return `${when} (${rel})`;
}
