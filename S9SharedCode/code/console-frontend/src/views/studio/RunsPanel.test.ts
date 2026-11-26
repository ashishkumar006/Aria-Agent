import { describe, expect, it } from 'vitest';
import { buildEvents } from './RunsPanel';
import type { StudioRunFrame } from '../../api';

/* The RUNS panel is the only surface that renders the raw transcript file;
   its fold is where the streamed deltas live on reload. These frames mirror
   what `state/code-studio/runs/<sid>/<run>.jsonl` actually carries: raw model
   events (thinking/text deltas on `delta`, not the SSE `text`), tool calls
   with object arguments, results matched back to calls by id. */

const frames = (list: StudioRunFrame[]) => list;

describe('buildEvents', () => {
  it('folds a raw transcript into an ordered timeline', () => {
    const got = buildEvents(frames([
      { t: 'started', run: 'r-1', mode: 'agent', at: 100.0 },
      { t: 'thinking', delta: 'he', at: 100.1 },
      { t: 'thinking', delta: 'llo', at: 100.2 },
      { t: 'tool_call', name: 'read_file', arguments: { path: 'a.py' }, id: 'c1', at: 100.3 },
      { t: 'tool_result', name: 'read_file', id: 'c1', ok: true, result: 'contents', at: 100.4 },
      { t: 'tool_call', name: 'search_files', arguments: { pattern: 'foo' }, id: 'c2', at: 100.5 },
      { t: 'tool_result', name: 'search_files', id: 'c2', ok: false, error: 'boom', at: 100.6 },
      { t: 'delta', text: 'answer ', at: 100.7 },
      { t: 'delta', text: 'text', at: 100.8 },
      { t: 'done', answer: 'answer text', input_tokens: 10, output_tokens: 5, tool_calls: 2, at: 100.9 },
    ]));
    expect(got.map((e) => e.kind)).toEqual(
      ['started', 'think', 'tool', 'tool', 'answer', 'done']);
    const think = got.find((e) => e.kind === 'think');
    expect(think?.text).toBe('hello');
    const tools = got.filter((e) => e.kind === 'tool');
    expect(tools[0].name).toBe('read_file');
    expect(tools[0].ok).toBe(true);
    expect(tools[0].result).toBe('contents');
    expect(tools[1].ok).toBe(false);
    expect(tools[1].result).toBe('boom');
    expect(got.find((e) => e.kind === 'answer')?.text).toBe('answer text');
    /* offsets are seconds since the first frame, monotonically non-decreasing */
    const offsets = got.map((e) => e.at);
    expect(offsets[0]).toBe(0);
    expect(offsets).toEqual([...offsets].sort((a, b) => a - b));
  });

  it('carries SSE-style thinking frames too, not just raw deltas', () => {
    const got = buildEvents(frames([
      { t: 'thinking', text: 'alpha ', at: 5 },
      { t: 'thinking', text: 'beta', at: 6 },
    ]));
    expect(got[0].kind).toBe('think');
    expect(got[0].text).toBe('alpha beta');
  });

  it('records a result with no matching call instead of dropping it', () => {
    const got = buildEvents(frames([
      { t: 'tool_result', name: 'git_action', id: 'gone', ok: true, result: 'ok', at: 2 },
    ]));
    expect(got).toHaveLength(1);
    expect(got[0].kind).toBe('tool');
    expect(got[0].ok).toBe(true);
  });

  it('keeps unknown frame kinds visible instead of swallowing them', () => {
    const got = buildEvents(frames([
      { t: 'mystery', some: 'thing', at: 1 },
    ]));
    expect(got[0].kind).toBe('unknown');
    expect(got[0].k).toBe('mystery');
  });

  it('folds nothing into an empty transcript', () => {
    expect(buildEvents([])).toEqual([]);
  });
});
