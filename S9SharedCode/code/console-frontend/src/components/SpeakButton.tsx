import { Loader2, Square, Volume2 } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';
import {
  speakText, stopSpeaking, subscribeSpeech, type SpeechState,
} from '../lib/voice';

/** Speak one assistant answer through the gateway's Kokoro voice. */
export function SpeakButton({ text, voice }: { text: string; voice: string }) {
  const owner = useId();
  const [state, setState] = useState<SpeechState>('idle');
  const [error, setError] = useState('');
  const playingRef = useRef(false);

  useEffect(() => subscribeSpeech((who, next) => {
    if (who === owner) setState(next);
  }), [owner]);

  useEffect(() => () => {
    if (playingRef.current) stopSpeaking();
  }, []);

  if (!String(text || '').trim()) return null;

  const busy = state === 'loading' || state === 'speaking';
  const label = state === 'loading'
    ? 'Preparing voice…'
    : state === 'speaking'
      ? 'Stop speaking'
      : error || 'Speak this answer';

  const click = async () => {
    if (busy) {
      playingRef.current = false;
      stopSpeaking();
      return;
    }
    setError('');
    playingRef.current = true;
    try {
      await speakText(text, voice, undefined, owner);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      playingRef.current = false;
      setState('idle');
    }
  };

  return (
    <button
      type="button"
      onClick={() => void click()}
      aria-label={label}
      title={label}
      aria-pressed={state === 'speaking'}
      className={`rounded p-1 transition-colors ${
        busy ? 'text-violet-300' : error ? 'text-red-300' : 'text-zinc-400 hover:text-violet-300'
      }`}
    >
      {state === 'loading'
        ? <Loader2 size={12} className="animate-spin" />
        : state === 'speaking'
          ? <Square size={12} />
          : <Volume2 size={12} />}
    </button>
  );
}
