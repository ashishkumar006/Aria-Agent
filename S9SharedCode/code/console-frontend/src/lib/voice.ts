import { api, type VoiceOption } from '../api';

const MODE_KEY = 'aria.voice.mode';
const VOICE_KEY = 'aria.voice.id';

export interface VoicePrefs {
  mode: boolean;
  voice: string;
}

export const DEFAULT_VOICES: VoiceOption[] = [
  { id: 'af_heart', label: 'Heart (female, warm)' },
  { id: 'af_bella', label: 'Bella (female, bright)' },
  { id: 'af_nicole', label: 'Nicole (female, soft)' },
  { id: 'am_michael', label: 'Michael (male)' },
  { id: 'am_adam', label: 'Adam (male, deep)' },
  { id: 'bf_emma', label: 'Emma (British female)' },
  { id: 'bm_george', label: 'George (British male)' },
];

export function loadVoicePrefs(): VoicePrefs {
  try {
    return {
      mode: localStorage.getItem(MODE_KEY) === '1',
      voice: localStorage.getItem(VOICE_KEY) || 'af_heart',
    };
  } catch {
    return { mode: false, voice: 'af_heart' };
  }
}

export function saveVoicePrefs(prefs: Partial<VoicePrefs>): void {
  try {
    if (prefs.mode !== undefined) localStorage.setItem(MODE_KEY, prefs.mode ? '1' : '0');
    if (prefs.voice) localStorage.setItem(VOICE_KEY, prefs.voice);
  } catch { /* private mode; prefs are best-effort */ }
}

export function mediaRecordingSupported(): boolean {
  return typeof window !== 'undefined'
    && !!navigator.mediaDevices?.getUserMedia
    && typeof MediaRecorder !== 'undefined';
}

const MIME_TYPES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/ogg;codecs=opus',
  'audio/mp4',
];

function pickMimeType(): string {
  for (const type of MIME_TYPES) {
    try {
      if (MediaRecorder.isTypeSupported(type)) return type;
    } catch { /* older browsers */ }
  }
  return '';
}

export class VoiceRecorder {
  private rec: MediaRecorder | null = null;
  private stream: MediaStream | null = null;
  private chunks: BlobPart[] = [];
  private stopping: Promise<Blob> | null = null;

  async start(): Promise<void> {
    if (this.rec) return;
    if (!mediaRecordingSupported()) {
      throw new Error('This browser cannot record audio.');
    }
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (e) {
      const name = (e as DOMException)?.name || '';
      throw new Error(name === 'NotAllowedError' || name === 'SecurityError'
        ? 'Microphone permission was denied. Allow it in the browser and try again.'
        : `Could not open the microphone (${name || 'unknown error'}).`);
    }
    const mime = pickMimeType();
    const rec = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
    this.stream = stream;
    this.rec = rec;
    this.chunks = [];
    this.stopping = null;
    rec.ondataavailable = (ev) => {
      if (ev.data && ev.data.size > 0) this.chunks.push(ev.data);
    };
    rec.start(250);
  }

  get active(): boolean {
    return this.rec !== null && this.rec.state !== 'inactive';
  }

  /** The audio captured so far, without stopping. Used for live transcripts
   *  while the user is still speaking. */
  snapshot(): Blob | null {
    if (!this.rec || this.chunks.length === 0) return null;
    return new Blob(this.chunks, { type: this.rec.mimeType || 'audio/webm' });
  }

  async stop(): Promise<Blob> {
    if (!this.rec) throw new Error('Not recording.');
    if (this.stopping) return this.stopping;
    const rec = this.rec;
    this.stopping = new Promise<Blob>((resolve, reject) => {
      rec.onstop = () => {
        const type = rec.mimeType || 'audio/webm';
        const blob = new Blob(this.chunks, { type });
        this.cleanup();
        if (!blob.size) reject(new Error('The recording was empty.'));
        else resolve(blob);
      };
      rec.onerror = () => {
        this.cleanup();
        reject(new Error('Recording failed.'));
      };
      try {
        rec.stop();
      } catch (e) {
        this.cleanup();
        reject(e as Error);
      }
    });
    return this.stopping;
  }

  cancel(): void {
    try { this.rec?.stop(); } catch { /* already stopped */ }
    this.cleanup();
  }

  private cleanup(): void {
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    this.rec = null;
    this.chunks = [];
    this.stopping = null;
  }
}

export function filenameFor(blob: Blob): string {
  const type = String(blob.type || '').toLowerCase();
  if (type.includes('ogg')) return 'clip.ogg';
  if (type.includes('mp4')) return 'clip.mp4';
  if (type.includes('wav')) return 'clip.wav';
  return 'clip.webm';
}

/* Emoji, flags, dingbats, variation selectors and ZWJ sequences. Kokoro
   reads these as names or noises; speech should carry words only. */
const EMOJI_RE = /[\u{1F1E6}-\u{1F1FF}\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{FE0F}\u{200D}\u{20E3}]/gu;

/** Markdown -> something Kokoro can read without reciting symbols or emojis. */
export function plainTextForSpeech(markdown: string, maxChars = 6000): string {
  let text = String(markdown || '')
    .replace(/```[\s\S]*?```/g, ' code block. ')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/^#{1,6}\s*/gm, '')
    .replace(/^\s*[-*+]\s+/gm, '')
    .replace(/^\s*\d+\.\s+/gm, '')
    .replace(/[*_~>|#]/g, ' ')
    .replace(EMOJI_RE, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  if (text.length > maxChars) text = text.slice(0, maxChars).trimEnd() + '…';
  return text;
}

export type SpeechState = 'idle' | 'loading' | 'speaking' | 'ended' | 'error';

interface QueuedClip {
  audio: HTMLAudioElement;
  url: string;
}

interface SpeechSession {
  owner: string;
  stopped: boolean;
  done: boolean;
  playing: boolean;
  queue: QueuedClip[];
  current: HTMLAudioElement | null;
  currentUrl: string | null;
  waiters: (() => void)[];
  resolve: () => void;
  reject: (e: unknown) => void;
}

let currentSession: SpeechSession | null = null;
let currentOwner: string | null = null;
const speechListeners = new Set<(owner: string | null, state: SpeechState) => void>();

/** Subscribe to playback changes. The owner lets one message's button reset
 *  when another message starts speaking (the audio element is a singleton). */
export function subscribeSpeech(
  fn: (owner: string | null, state: SpeechState) => void,
): () => void {
  speechListeners.add(fn);
  return () => { speechListeners.delete(fn); };
}

function emitTo(owner: string | null, state: SpeechState): void {
  for (const fn of speechListeners) {
    try { fn(owner, state); } catch { /* listener is best-effort */ }
  }
}

function emitSpeech(state: SpeechState): void {
  emitTo(currentOwner, state);
}

function revoke(item: QueuedClip): void {
  try { URL.revokeObjectURL(item.url); } catch { /* ignore */ }
}

function settleSession(session: SpeechSession, state: SpeechState): void {
  if (session.stopped) return;
  session.stopped = true;
  if (currentSession === session) {
    currentSession = null;
    currentOwner = null;
  }
  for (const item of session.queue.splice(0)) revoke(item);
  try { session.current?.pause(); } catch { /* ignore */ }
  if (session.currentUrl) {
    try { URL.revokeObjectURL(session.currentUrl); } catch { /* ignore */ }
    session.currentUrl = null;
  }
  session.current = null;
  for (const wake of session.waiters.splice(0)) wake();
  emitTo(session.owner, state);
  session.resolve();
}

/** Stop whatever is speaking or being prepared. Clears the prefetch queue,
 *  aborts the HTTP stream, and resolves the in-flight speakText promise. */
export function stopSpeaking(): void {
  if (!currentSession) return;
  settleSession(currentSession, 'idle');
}

function base64ToBlob(value: string, type: string): Blob {
  const bin = atob(value);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type });
}

/** Speak text through the gateway in sentence chunks. The first chunk starts
 *  as soon as it is ready; later chunks are synthesized while earlier ones
 *  play, so a long answer has the same time-to-first-word as a short one. */
export async function speakText(
  text: string, voice: string, onState?: (state: SpeechState) => void,
  owner = 'default',
): Promise<void> {
  // Large cap, not the old 6,000: chunked synthesis makes length a playback
  // duration question, not a latency one.
  const plain = plainTextForSpeech(text, 120_000);
  if (!plain) return;
  stopSpeaking();
  currentOwner = owner;
  let resolve!: () => void;
  let reject!: (e: unknown) => void;
  const finished = new Promise<void>((res, rej) => { resolve = res; reject = rej; });
  const session: SpeechSession = {
    owner, stopped: false, done: false, playing: false, queue: [],
    current: null, currentUrl: null, waiters: [], resolve, reject,
  };
  currentSession = session;
  emitSpeech('loading');
  onState?.('loading');

  const playQueue = async (): Promise<void> => {
    if (session.playing || session.stopped) return;
    session.playing = true;
    while (!session.stopped) {
      const item = session.queue.shift();
      if (!item) break;
      for (const wake of session.waiters.splice(0)) wake();
      session.current = item.audio;
      session.currentUrl = item.url;
      emitSpeech('speaking');
      onState?.('speaking');
      try {
        await new Promise<void>((done, fail) => {
          item.audio.onended = () => { item.audio.onended = null; done(); };
          item.audio.onerror = () => {
            item.audio.onerror = null;
            fail(new Error('Audio playback failed.'));
          };
          item.audio.play().catch(fail);
        });
      } catch (e) {
        revoke(item);
        session.current = null;
        session.currentUrl = null;
        session.playing = false;
        settleWithError(e);
        return;
      }
      revoke(item);
      session.current = null;
      session.currentUrl = null;
    }
    session.playing = false;
    if (session.stopped) return;
    if (session.queue.length) { void playQueue(); return; }
    if (session.done) settleSession(session, 'ended');
  };

  const settleWithError = (e: unknown): void => {
    if (currentSession === session) {
      currentSession = null;
      currentOwner = null;
    }
    session.stopped = true;
    for (const item of session.queue.splice(0)) revoke(item);
    try { session.current?.pause(); } catch { /* ignore */ }
    session.current = null;
    session.currentUrl = null;
    for (const wake of session.waiters.splice(0)) wake();
    emitTo(session.owner, 'error');
    onState?.('error');
    session.reject(e);
  };

  // Consume the stream in the background: chunk 1 is handed to the player
  // immediately, and the loop keeps reading while it plays. The queue cap
  // turns that into HTTP backpressure rather than unbounded memory if
  // synthesis is much faster than real time.
  void (async () => {
    let first = true;
    try {
      for await (const ev of api.ttsStream(plain, voice)) {
        if (session.stopped) return;
        if (ev.type === 'error') {
          throw new Error(ev.error || 'voice synthesis failed');
        }
        if (ev.type === 'chunk' && ev.audio) {
          const item = {
            audio: new Audio(URL.createObjectURL(base64ToBlob(ev.audio, 'audio/wav'))),
            url: '',
          };
          item.url = item.audio.src;
          session.queue.push(item);
          first = false;
          if (!session.playing) void playQueue();
          // Keep at most four clips queued; the next read waits for one to
          // be consumed, which pauses the generator and its synthesis.
          while (session.queue.length >= 4 && !session.stopped) {
            await new Promise<void>((wake) => session.waiters.push(wake));
          }
        }
        if (ev.type === 'done') break;
      }
      if (first) throw new Error('voice stream returned no audio');
      session.done = true;
      if (!first && !session.playing && !session.queue.length) {
        settleSession(session, 'ended');
      }
    } catch (e) {
      if (session.stopped) return;
      // A gateway without the streaming endpoint (or a mid-stream failure)
      // falls back to the old whole-text path when nothing has played yet.
      if (first) {
        try {
          const blob = await api.tts(plainTextForSpeech(text, 6000), voice);
          const item = { audio: new Audio(URL.createObjectURL(blob)), url: '' };
          item.url = item.audio.src;
          session.queue.push(item);
          session.done = true;
          void playQueue();
          return;
        } catch (fallbackError) {
          settleWithError(fallbackError);
          return;
        }
      }
      settleWithError(e);
    }
  })();

  return finished;
}
