/* Shared, XSS-hardened mini-markdown + run-status classifier.
   Used by Chat, Inspector previews, and the DAG views so rendering and
   status keywords behave identically everywhere.

   Security model, unchanged from the original and worth preserving:
   ALL text is escaped first (including quotes, so a URL can never break
   out of an href attribute), and the only HTML that ever reaches the
   output is the fixed tag set emitted by this function. Links are limited
   to http(s) by SAFE_URL. Nothing here interpolates unescaped input into
   a tag, an attribute name, or a URL. */

export function esc(s: string): string {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

const SAFE_URL = /^https?:\/\/[^\s"'<>]+$/i;

/** Inline spans: bold, inline code, links, strikethrough. Operates on
    already-escaped text, so `<b>` etc. are the only tags it introduces. */
function inline(s: string): string {
  return esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/(^|[\s(])_([^_]+)_(?=$|[\s.,;:)!?])/g, '$1<i>$2</i>')
    .replace(/~~([^~]+)~~/g, '<del>$1</del>')
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_m, text: string, url: string) =>
      SAFE_URL.test(url)
        ? `<a href="${url}" target="_blank" rel="noopener noreferrer">${text}</a>`
        : text);
}

/** Split a table row on unescaped pipes, dropping the outer delimiters. */
function cells(row: string): string[] {
  return row.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
}

const TABLE_SEP = /^\|?[\s:|-]+\|[\s:|-]*$/;

/** Markdown subset → HTML: ATX headings (offset one level so model output
    can never outrank the page's own heading structure), fenced code
    blocks, pipe tables, nested + ordered lists, blockquotes, rules, and
    paragraphs.

    Fences and tables are the two things research reports actually contain
    constantly, and without them every code block rendered as a stack of
    separate paragraphs with visible backticks — while the container styles
    in the views styled a `pre` the renderer never emitted. */
export function renderMarkdown(src: string, headingOffset = 0): string {
  const lines = String(src || '').split('\n');
  let html = '';
  // Open list stack, so nesting depth is preserved instead of flattened.
  const stack: string[] = [];
  // Depths with an <li> still open. Open <li>s always form a root-to-leaf
  // path, so closing everything >= the incoming depth keeps every sibling,
  // parent, and sublist correctly nested — including a type change (ul→ol)
  // at the same depth, which closes and reopens the list.
  const liDepth: number[] = [];
  const closeLis = (toDepth: number) => {
    while (liDepth.length && liDepth[liDepth.length - 1] >= toDepth) {
      html += '</li>';
      liDepth.pop();
    }
  };
  const closeLists = (toDepth = 0) => {
    closeLis(toDepth + 1);
    while (stack.length > toDepth) html += `</${stack.pop()}>`;
  };

  for (let i = 0; i < lines.length; i++) {
    const t = lines[i].replace(/\s+$/, '');

    // ── fenced code block ──────────────────────────────────────────────
    const fence = t.match(/^\s*(```|~~~)\s*([A-Za-z0-9+#._-]*)\s*$/);
    if (fence) {
      closeLists();
      const marker = fence[1];
      const lang = fence[2];
      const buf: string[] = [];
      i++;
      while (i < lines.length && !new RegExp(`^\\s*${marker}\\s*$`).test(lines[i])) {
        buf.push(lines[i]);
        i++;
      }
      // The closing fence may be missing mid-stream; that is expected while
      // text is still arriving, so emit what we have rather than dropping it.
      const cls = lang ? ` class="language-${esc(lang)}"` : '';
      html += `<pre><code${cls}>${esc(buf.join('\n'))}</code></pre>`;
      continue;
    }

    // ── table ──────────────────────────────────────────────────────────
    if (t.includes('|') && TABLE_SEP.test(lines[i + 1] || '')) {
      closeLists();
      const head = cells(t);
      i += 2; // skip the |---|---| separator
      const body: string[][] = [];
      for (; i < lines.length && lines[i].includes('|') && lines[i].trim(); i++) {
        body.push(cells(lines[i]));
      }
      i--;
      const th = head.map((c) => `<th>${inline(c)}</th>`).join('');
      const rows = body
        .map((r) => `<tr>${head.map((_, n) => `<td>${inline(r[n] ?? '')}</td>`).join('')}</tr>`)
        .join('');
      html += `<div class="table-scroll"><table><thead><tr>${th}</tr></thead><tbody>${rows}</tbody></table></div>`;
      continue;
    }

    if (!t.trim()) { closeLists(); continue; }

    // ── thematic rule ──────────────────────────────────────────────────
    if (/^\s*([-*_])\s*(\1\s*){2,}$/.test(t)) {
      closeLists();
      html += '<hr />';
      continue;
    }

    // ── heading ────────────────────────────────────────────────────────
    const h = t.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      closeLists();
      const lv = Math.min(6, h[1].length + headingOffset);
      html += `<h${lv}>${inline(h[2])}</h${lv}>`;
      continue;
    }

    // ── blockquote ─────────────────────────────────────────────────────
    const bq = t.match(/^>\s?(.*)$/);
    if (bq) {
      closeLists();
      html += `<blockquote>${inline(bq[1])}</blockquote>`;
      continue;
    }

    // ── list item (indentation decides depth) ──────────────────────────
    const li = t.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
    if (li) {
      const depth = Math.min(4, Math.floor(li[1].replace(/\t/g, '  ').length / 2) + 1);
      const ordered = /\d/.test(li[2]);
      const kind = ordered ? 'ol' : 'ul';
      closeLis(depth); // siblings at this depth, then anything deeper
      if (stack.length > depth) closeLists(depth);
      else if (stack.length === depth && depth > 0 && stack[depth - 1] !== kind) {
        html += `</${stack.pop()}>`;
      }
      while (stack.length < depth) {
        const kk = stack.length === depth - 1 ? kind : 'ul';
        html += `<${kk}>`;
        stack.push(kk);
      }
      // The <li> stays OPEN on purpose: a deeper item that follows opens
      // its sublist inside this element (`<li>one<ul>…`), which is what
      // makes the nesting real instead of sibling lists.
      html += `<li>${inline(li[3])}`;
      liDepth.push(depth);
      continue;
    }

    closeLists();
    html += `<p>${inline(t.trim())}</p>`;
  }
  closeLists();
  return html;
}

export type RunStatus = 'ok' | 'err' | 'warn' | 'info' | 'muted';

/** One terminal-state predicate for the whole console. Three pages each
    had their own regex and they disagreed: Runs stopped polling on
    /done|complete|ok|success|error|fail|skip|cancel/, Research's catch-up
    only recognised /complete|fail|error|skip|cancel/, and past-report
    lookup used yet another set — so a `done` node could be "terminal" on
    one page and "still running" on another. */
export function isTerminal(status?: string): boolean {
  /* `interrupted` is the server's label for a run whose process died before
     it could mark a node terminal (stale-graph reconciliation). It must read
     as terminal, or the Runs page keeps polling a dead run forever. */
  return /done|complete|ok|success|pass|error|fail|skip|cancel|interrupt/i.test(status || '');
}

/** One label + tone per lifecycle state, for the status pills. Pages
    disagreed: live was blue on Research/Runs but green on Apps/Skills,
    off was grey on Scheduler but yellow on Skills (which used two tones
    for one label), and Runs mapped two tones to the identical word
    "queued". */
export function pillForStatus(status?: string): { label: string; tone: 'ok' | 'err' | 'warn' | 'info' | 'muted' } {
  const s = (status || '').toLowerCase();
  /* Checked before "live": a dead run's node is reconciled to `interrupted`
     server-side, and warning-behaviour reads truer than either colour. */
  if (/interrupt|orphan|stale/.test(s)) return { label: 'interrupted', tone: 'warn' };
  if (/(fail|error|dead|denied)/.test(s)) return { label: 'failed', tone: 'err' };
  if (/(cancel|skip)/.test(s)) return { label: 'stopped', tone: 'warn' };
  if (/(running|live|active|working|started)/.test(s)) return { label: 'live', tone: 'info' };
  if (/(pend|wait|queued|idle|paused)/.test(s)) return { label: 'queued', tone: 'muted' };
  if (/(done|complete|ok|success|pass)/.test(s)) return { label: 'done', tone: 'ok' };
  return { label: status || 'unknown', tone: 'muted' };
}

/** Single keyword classifier for node/run statuses (done/complete/ok → ok,
    fail/error → err, running/live/active → info, pending/wait → warn). */
export function classifyStatus(status?: string): RunStatus {
  const s = (status || '').toLowerCase();
  if (/interrupt|orphan|stale/.test(s)) return 'warn';
  if (/(fail|error|dead|denied)/.test(s)) return 'err';
  if (/(running|live|active|working|started)/.test(s)) return 'info';
  if (/(pend|wait|queued|idle|paused)/.test(s)) return 'warn';
  if (/(done|complete|ok|success|pass)/.test(s)) return 'ok';
  return 'muted';
}
