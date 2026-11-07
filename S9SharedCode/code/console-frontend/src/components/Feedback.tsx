import { MessageSquare, Send, ThumbsDown, ThumbsUp } from 'lucide-react';
import { useEffect, useState } from 'react';
import { api } from '../api';

export type VoteVal = 1 | -1 | 0;

/** Inline thumbs + optional free-text note for one assistant response.

Attached to chat and research result turns so feedback lands on every response,
not just run-DAG nodes. Chat messages have no DAG node id, so they are keyed by
`msgId` (the turn's stable signature) and the POST goes through `/api/feedback`
with `msg_id`. The note is the free-text explanation the persona idle-task
reads to decide whether to extract a preference.
*/
export function Feedback({ msgId, sessionId, nodeId = '', skill = '' }: {
  msgId: string;
  sessionId: string;
  nodeId?: string;
  skill?: string;
}) {
  const [vote, setVote] = useState<VoteVal>(0);
  const [note, setNote] = useState('');
  const [noteOpen, setNoteOpen] = useState(false);
  const [sending, setSending] = useState(false);
  const [status, setStatus] = useState('');

  /* The note is stored server-side, but this row is remounted after a refresh
     or a thread reload. Without restoring it here the textarea came back
     empty even though feedback.jsonl still held the note — so the user's
     correction looked lost. */
  useEffect(() => {
    let live = true;
    /* React reuses this row when the transcript switches threads. Without
       clearing first, the note from the previous message stayed in state and
       appeared on every later message/thread. */
    setVote(0);
    setNote('');
    setNoteOpen(false);
    setStatus('');
    if (!msgId && !nodeId) return;
    api.feedbackGet(nodeId, sessionId, msgId).then((v) => {
      if (!live) return;
      if (v.vote === 1 || v.vote === -1) setVote(v.vote);
      if (v.note && v.note.trim()) {
        /* Keep the text, but do not reopen the editor on every thread load:
           a wall of expanded textareas is not a transcript. The "note saved"
           control is the collapsed indicator; clicking it reveals the text. */
        setNote(v.note);
        setNoteOpen(false);
        setStatus('');
      }
    }).catch(() => { /* feedback is best-effort */ });
    return () => { live = false; };
  }, [msgId, nodeId, sessionId]);

  const send = async (v: VoteVal, text = '') => {
    setSending(true);
    setStatus('');
    try {
      const res = await api.feedbackPost(
        sessionId, nodeId, v, skill || undefined, msgId, text);
      setVote(v);
      if (text) {
        setNote(text);
        setNoteOpen(false);
      }
      setStatus(v === 0
        ? 'vote cleared'
        : res.learning === 'queued'
          ? 'noted — Aria is learning from this in the background'
          : res.learning === 'disabled'
            ? 'saved — automatic learning is switched off'
            : res.learning === 'error'
              ? 'saved — the learning worker could not start'
              : 'vote recorded');
    } catch { /* retryable; the banner still flips if it worked */ }
    finally { setSending(false); }
  };

  const onThumb = (v: VoteVal) => {
    // Toggling the same thumb off is a real state change, so persist it as a
    // 0 tombstone: the local pressed state and the next reload must agree.
    if (vote === v) {
      setVote(0);
      void send(0, '');
      return;
    }
    setVote(v);
    void send(v, note);
  };

  const submitNote = () => {
    const trimmed = note.trim();
    if (!trimmed || vote === 0) {
      // A note with no vote still needs a vote to be recorded: pair it with a
      // neutral-ish upvote so the message carries feedback weight without
      // polluting the downvote totals.
      void send(1, trimmed);
      return;
    }
    void send(vote, trimmed);
  };

  const saved = !!note.trim();
  return (
    <div className="flex min-w-0 flex-1 flex-wrap items-center gap-1 text-[12px] text-zinc-400">
      <button
        type="button"
        onClick={() => onThumb(1)}
        aria-pressed={vote === 1}
        className={`flex h-6 w-6 items-center justify-center rounded ${vote === 1 ? 'text-violet-300' : 'hover:text-violet-300'}`}
        title="Thumbs up">
        <ThumbsUp size={12} />
      </button>
      <button
        type="button"
        onClick={() => onThumb(-1)}
        aria-pressed={vote === -1}
        className={`flex h-6 w-6 items-center justify-center rounded ${vote === -1 ? 'text-red-400' : 'hover:text-red-400'}`}
        title="Thumbs down">
        <ThumbsDown size={12} />
      </button>
      <button
        type="button"
        onClick={() => setNoteOpen((open) => !open)}
        aria-expanded={noteOpen}
        title={saved ? 'Show saved note' : 'Add a note'}
        className={`ml-0.5 flex h-6 items-center gap-1 rounded px-1.5 hover:bg-white/5 ${
          saved ? 'text-emerald-300/90' : 'text-zinc-400 hover:text-zinc-200'
        }`}>
        <MessageSquare size={11} />
        {saved ? 'note saved' : 'add note'}
      </button>
      {status && (
        <span role="status" className="text-[11px] text-emerald-300/80">
          {status}
        </span>
      )}
      {noteOpen && (
        <div className="mt-1 flex w-full items-start gap-1.5">
          <textarea
            value={note}
            onChange={(e) => setNote(e.target.value.slice(0, 280))}
            placeholder="What did you want instead?"
            rows={2}
            className="min-h-[42px] flex-1 resize-y rounded-md border border-white/10 bg-black/40 p-1.5 text-[12px] text-zinc-200 outline-none focus:border-violet-400"
          />
          <button
            type="button"
            disabled={sending || !note.trim()}
            onClick={submitNote}
            className="flex h-7 w-7 flex-none items-center justify-center rounded text-violet-300 disabled:opacity-40"
            title="Send note"
            aria-label="Send note">
            <Send size={12} />
          </button>
        </div>
      )}
    </div>
  );
}
